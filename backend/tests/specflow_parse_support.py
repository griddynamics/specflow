"""Shared helpers of the parse tests (no tests here).

- fakes for tier 1 (session factory, repository functions),
- the history SCRUB used when recording replay fixtures from tier-2 runs, and
  the leak scan that also guards the committed fixtures (AC-8.1, AC-8.4),
- ``python -m specflow_parse_support``: a worker process whose parse step
  sleeps first, so a tier-2 test can SIGKILL it mid-parse (AC-8.6).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from sqlalchemy.ext.asyncio import create_async_engine
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from db.repositories import PARSE_ERROR_CODES
from db.session import build_session_factory
from file_parsers.pdf.to_markdown import pdf_file_to_markdown
from settings import DatabaseSettings, StorageSettings
from storage.gcs import GcsObjectStore
from temporal.documents import activities as document_activities
from temporal.documents.activities import ParseActivities
from temporal.documents.workflows import ParseDocumentWorkflow

FIXTURE_DIR: Final = Path(__file__).parent / "fixtures" / "temporal"
PDF_FIXTURE: Final = Path(__file__).parent / "data" / "specs" / "extraction.pdf"

FIXED_WORKFLOW_ID: Final = "doc-parse-00000000-0000-4000-8000-0000000000d1"
FIXED_QUEUE: Final = "specflow-parse"
FIXED_IDENTITY: Final = "1@worker"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_IDENTITY = re.compile(r"^\d+@.+")
LEAK_PATTERNS: Final = (
    re.compile(r"/[U]sers/|/home/|/private/|/var/folders/|[A-Za-z]:\\\\"),
    re.compile(r"\.pdf\b", re.IGNORECASE),
    re.compile(r"(?i)(password|secret|api[_-]?key|token)\s*[\"':=]"),
    re.compile(r"specflow-parse-"),  # the temp-dir prefix
)


# ---------------------------------------------------------------- tier-1 fakes
class FakeSession:
    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> FakeSession:
        return self


class FakeSessions:
    def __call__(self) -> FakeSession:
        return FakeSession()


class FakeRepository:
    """Stand-ins for the ``repositories`` functions the activities call."""

    def __init__(self, *, exists: bool = True) -> None:
        self.exists = exists
        self.vanishes = False  # the row is there when read, gone at the write
        self.parsed: list[tuple[str, str]] = []
        self.failed: list[str] = []
        self.fail_with: Exception | None = None

    async def get_document(self, _session: Any, _project_id: UUID, _document_id: UUID) -> object | None:
        return object() if self.exists else None

    async def mark_parsed(self, _session, _project_id, _document_id, markdown, parser) -> bool:
        if self.fail_with is not None:
            raise self.fail_with
        self.parsed.append((markdown, parser))
        return self.exists and not self.vanishes

    async def mark_failed(self, _session, _project_id, _document_id, code) -> bool:
        if self.fail_with is not None:
            raise self.fail_with
        if code not in PARSE_ERROR_CODES:
            raise ValueError("unknown error code")
        self.failed.append(code)
        return self.exists


def install_fake_repository(monkeypatch: Any, repository: FakeRepository) -> None:
    for name in ("get_document", "mark_parsed", "mark_failed"):
        monkeypatch.setattr(document_activities.repositories, name, getattr(repository, name))


# ----------------------------------------------------------------------- scrub
def _scrub_text(value: str, uuids: dict[str, str], known_ids: dict[str, str]) -> str:
    for real, fixed in known_ids.items():
        value = value.replace(real, fixed)

    def replace(match: re.Match[str]) -> str:
        found = match.group(0).lower()
        return uuids.setdefault(found, f"00000000-0000-4000-8000-{len(uuids) + 1:012d}")

    return _UUID.sub(replace, value)


def _scrub_payload(payload: dict[str, Any], uuids: dict[str, str], known: dict[str, str]) -> None:
    raw = base64.b64decode(payload["data"]).decode()
    payload["data"] = base64.b64encode(_scrub_text(raw, uuids, known).encode()).decode()


def scrub_history(
    history_json: str, *, workflow_id: str, task_queues: list[str], run_ids: list[str] = ()
) -> dict[str, Any]:
    """A deterministic, data-free copy of a recorded history.

    Replaced: the workflow id and every uuid (documents, projects, run ids) by
    fixed values, task-queue names, worker identities (``pid@host``), and stack
    traces (they hold source paths). Payloads (base64 JSON) are rewritten the
    same way. Timestamps and event structure stay, so replay is unaffected.
    """

    uuids: dict[str, str] = {}
    known = {workflow_id: FIXED_WORKFLOW_ID, **dict.fromkeys(task_queues, FIXED_QUEUE)}
    known.update(dict.fromkeys(run_ids, "00000000-0000-4000-8000-0000000000f1"))

    def scrub_string(node: str, key: str | None) -> str:
        if key == "stackTrace":
            return ""
        if key == "identity" or _IDENTITY.match(node):
            return FIXED_IDENTITY
        return _scrub_text(node, uuids, known)

    def walk(node: Any, key: str | None = None) -> Any:
        result = node
        if isinstance(node, dict):
            if "data" in node and "metadata" in node:
                _scrub_payload(node, uuids, known)
            else:
                result = {k: walk(v, k) for k, v in node.items()}
        elif isinstance(node, list):
            result = [walk(item, key) for item in node]
        elif isinstance(node, str):
            result = scrub_string(node, key)
        return result

    return walk(json.loads(history_json))


def find_leaks(text: str, *, extra: tuple[str, ...] = (), patterns: bool = True) -> list[str]:
    """Descriptions (never the matched text) of anything a fixture must not hold.

    Payload bytes are decoded first, so a leak inside a payload is found too.
    """

    decoded = [text]
    for blob in re.findall(r'"data":\s*"([A-Za-z0-9+/=]+)"', text):
        try:
            decoded.append(base64.b64decode(blob).decode())
        except ValueError:
            continue
    found = []
    for chunk in decoded:
        found.extend(
            f"pattern {pattern.pattern[:24]}"
            for pattern in (LEAK_PATTERNS if patterns else ())
            if pattern.search(chunk)
        )
        found.extend(f"extra value #{i}" for i, value in enumerate(extra) if value and value in chunk)
    return found


# ------------------------------------------------- worker process killed mid-parse
async def _run_slow_worker(sleep_seconds: float) -> None:
    database = DatabaseSettings.from_environment().model_copy(update={"name": os.environ["PARSE_TEST_DATABASE"]})
    storage = StorageSettings(
        STORAGE_BUCKET=os.environ["STORAGE_BUCKET"],
        STORAGE_PREFIX=os.environ["PARSE_TEST_PREFIX"],
        GOOGLE_CLOUD_PROJECT=os.environ["GOOGLE_CLOUD_PROJECT"],
    )

    def slow_parse(path: Path):
        Path(os.environ["PARSE_TEST_MARKER"]).write_text("parsing")
        threading.Event().wait(sleep_seconds)
        return pdf_file_to_markdown(path)

    client = await Client.connect(
        os.environ["TEMPORAL_ADDRESS"],
        namespace=os.environ["TEMPORAL_NAMESPACE"],
        data_converter=pydantic_data_converter,
    )
    engine = create_async_engine(database.url())
    activities = ParseActivities(
        build_session_factory(engine),
        GcsObjectStore(storage),
        ThreadPoolExecutor(max_workers=1),
        parse=slow_parse,
    )
    worker = Worker(
        client,
        task_queue=os.environ["PARSE_TEST_QUEUE"],
        workflows=[ParseDocumentWorkflow],
        activities=[activities.parse_document, activities.mark_document_failed],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(_run_slow_worker(float(sys.argv[1])))
