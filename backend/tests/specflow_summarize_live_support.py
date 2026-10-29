"""Tier-2 harness for the summarize workflow (no tests here).

``SummarizeStack`` runs the REAL agents-queue registration (scratch Postgres
migrated to head, the session test agent role with RLS) in an in-process worker
on the test's unique task queue of the live ``specflow-local`` namespace. The
model is either a recording ``TestModel`` or the LLM gateway. The queue uses no Neo4j.
"""

from __future__ import annotations

import base64
import getpass
import gzip
import hashlib
import json
import os
import re
import socket
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

from agents import Model, ModelProvider
from sqlalchemy import select
from temporalio.client import Client, WorkflowHandle
from temporalio.openai_agents.testing import ResponseBuilders, TestModel
from temporalio.worker import Worker

from agent_runtime import plugin as plugin_module
from db.agent_scope import build_agent_session_factory
from db.models import Document, Project, ProjectMember, User
from db.session import build_engine, build_session_factory
from settings import DatabaseSettings, RoleSettings
from temporal import ids
from temporal.agents.models import SummarizeDocumentInput
from temporal.agents.registration import build_registration
from temporal.agents.workflows import (
    EXECUTION_TIMEOUT,
    SummarizeDocumentWorkflow,
)

FIXTURES: Final = Path(__file__).resolve().parent / "fixtures" / "temporal"
RECORD_ENV: Final = "SPECFLOW_RECORD_FIXTURES"
# The pystatic `[file-size]` ceiling (backend/pystatic.toml `max-bytes`): a recorded history
# larger than this is stored gzip-compressed as `summarize_<name>.json.gz`.
FIXTURE_COMPRESS_ABOVE_BYTES: Final = 1024 * 1024
COMPRESSED_SUFFIX: Final = ".gz"
SYNTHETIC_PARAGRAPH: Final = (
    "The Acme widget pipeline moves synthetic widgets through five stages: intake, "
    "sorting, painting, packing and shipping. Each stage keeps a counter. "
)


def synthetic_markdown(label: str, chars: int = 20_000) -> str:
    """Non-sensitive synthetic text (P11) with a unique ``label`` line."""

    lines = [f"# Synthetic report {label}", f"CANARY-{label}"]
    index = 0
    while sum(len(line) + 1 for line in lines) < chars:
        lines.append(f"[section {index}] {SYNTHETIC_PARAGRAPH}")
        index += 1
    return "\n".join(lines)


class RecordingModel(Model):
    """Records every model input (system instructions + conversation)."""

    def __init__(self, inner: Model) -> None:
        self.inner = inner
        self.inputs: list[str] = []

    async def get_response(self, system_instructions, *args: Any, **kwargs: Any):
        conversation = kwargs["input"] if "input" in kwargs else args[0]
        self.inputs.append(json.dumps({"system": system_instructions, "input": conversation}, default=str))
        return await self.inner.get_response(system_instructions, *args, **kwargs)

    def stream_response(self, *args: Any, **kwargs: Any):
        raise NotImplementedError


class RecordingProvider(ModelProvider):
    def __init__(self, model: Model) -> None:
        self.model = RecordingModel(model)

    def get_model(self, _model_name: str | None) -> Model:
        return self.model


def scripted(*calls: tuple[str, dict[str, Any]] | str) -> RecordingProvider:
    """A recording TestModel: ``(tool, args)`` tool calls, or a plain text message."""

    responses = []
    for index, call in enumerate(calls):
        if isinstance(call, str):
            responses.append(ResponseBuilders.output_message(call))
        else:
            name, args = call
            responses.append(ResponseBuilders.tool_call(json.dumps(args), name, call_id=f"c{index}"))
    return RecordingProvider(TestModel.returning_responses(responses))


def happy_calls(
    title: str = "Synthetic widget pipeline", summary: str = "Five stages."
) -> list[tuple[str, dict[str, Any]]]:
    return [
        ("read_document", {"offset": 0}),
        ("read_document", {"offset": 16000}),
        ("submit_summary", {"title": title, "summary": summary}),
    ]


def happy_script(title: str = "Synthetic widget pipeline", summary: str = "Five stages.") -> RecordingProvider:
    return scripted(*happy_calls(title, summary))


@dataclass(frozen=True)
class SeededDocument:
    project_id: uuid.UUID
    document_id: uuid.UUID
    user_id: uuid.UUID
    markdown: str


@dataclass
class SummarizeStack:
    client: Client
    task_queue: str
    id_prefix: str
    database: DatabaseSettings
    app_sessions: Any
    track: Callable[[WorkflowHandle], WorkflowHandle]

    async def seed(
        self,
        *,
        markdown: str,
        filename: str = "synthetic.pdf",
        summary: str | None = None,
    ) -> SeededDocument:
        """A user, a project (owner) and a parsed document whose summary is ``running``."""

        async with self.app_sessions() as session, session.begin():
            tag = uuid.uuid4().hex[:10]
            user = User(
                actor_id=f"human:pytest-{tag}",
                display_name="Pytest",
                email="pytest@example.com",
            )
            session.add(user)
            await session.flush()
            project = Project(name=f"pytest-{tag}", created_by=user.id)
            session.add(project)
            await session.flush()
            session.add(ProjectMember(project_id=project.id, user_id=user.id))
            document = Document(
                project_id=project.id,
                content_hash=hashlib.sha256(tag.encode()).hexdigest(),
                original_filename=filename,
                media_type="application/pdf",
                size_bytes=len(markdown),
                origin="upload",
                original_key=f"projects/{project.id}/documents/x/original.pdf",
                markdown=markdown,
                status="parsed",
                summarization=summary,
                summarization_status="running",
            )
            session.add(document)
            await session.flush()
        return SeededDocument(project.id, document.id, user.id, markdown)

    async def document_row(self, seeded: SeededDocument) -> Document:
        async with self.app_sessions() as session:
            return await session.scalar(select(Document).where(Document.id == seeded.document_id))

    async def start(
        self,
        seeded: SeededDocument,
        *,
        model: str = "test/model",
        trace_sensitive_data: bool = True,
        workflow: Any = SummarizeDocumentWorkflow.run,
        name: str | None = None,
        execution_timeout: timedelta = EXECUTION_TIMEOUT,
    ) -> WorkflowHandle:
        workflow_id = (
            f"{self.id_prefix}{name}" if name else ids.summary_workflow_id(seeded.document_id, prefix=self.id_prefix)
        )
        handle = await self.client.start_workflow(
            workflow,
            SummarizeDocumentInput(
                project_id=seeded.project_id,
                document_id=seeded.document_id,
                requested_by=seeded.user_id,
                model=model,
                trace_sensitive_data=trace_sensitive_data,
            ),
            id=workflow_id,
            task_queue=self.task_queue,
            execution_timeout=execution_timeout,
        )
        return self.track(handle)


@asynccontextmanager
async def summarize_stack(
    *,
    settings: RoleSettings,
    temporal_live: Any,
    database: DatabaseSettings,
    agent_role: Any,
    model_provider: ModelProvider | None,
    workflows: Sequence[type] = (SummarizeDocumentWorkflow,),
    overrides: dict[str, Callable[..., Any]] | None = None,
    otel: bool = False,
) -> AsyncIterator[SummarizeStack]:
    """The real registration in an in-process worker; everything is cleaned up."""

    app_engine = build_engine(database)
    registration = build_registration(app_engine, build_agent_session_factory(database, agent_role.settings()))
    plugin = plugin_module.build_plugin(settings, otel=otel, model_provider=model_provider)
    client = await Client.connect(
        settings.temporal.address,
        namespace=settings.temporal.namespace,
        plugins=[plugin],
    )
    activities = list(registration.activities)
    for name, replacement in (overrides or {}).items():
        activities = [replacement if fn.__temporal_activity_definition.name == name else fn for fn in activities]
    stack = SummarizeStack(
        client=client,
        task_queue=temporal_live.task_queue,
        id_prefix=temporal_live.id_prefix,
        database=database,
        app_sessions=build_session_factory(app_engine),
        track=temporal_live.track,
    )
    try:
        async with Worker(
            client,
            task_queue=temporal_live.task_queue,
            workflows=list(workflows),
            activities=activities,
            graceful_shutdown_timeout=timedelta(seconds=1),
        ):
            yield stack
            await temporal_live.cleanup()
    finally:
        await registration.close()


# ---------------------------------------------------------------- history
async def history_events(handle: WorkflowHandle) -> list[Any]:
    return [event async for event in handle.fetch_history_events()]


def _decode(payload: Any) -> str:
    try:
        return payload.data.decode()
    except UnicodeDecodeError:
        return ""


def scheduled_activities(events: list[Any]) -> list[tuple[str, str]]:
    """``(activity type, decoded input)`` for every scheduled activity, in order."""

    found = []
    for event in events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            attributes = event.activity_task_scheduled_event_attributes
            data = "".join(_decode(p) for p in attributes.input.payloads)
            found.append((attributes.activity_type.name, data))
    return found


def count_events(events: list[Any], field_name: str) -> int:
    return sum(1 for event in events if event.HasField(field_name))


def secret_values() -> list[str]:
    """Env values that must never appear in history or fixtures (never printed)."""

    names = (
        "DATABASE_PASSWORD",
        "DATABASE_AGENT_PASSWORD",
        "NEO4J_PASSWORD",
        "LLM_GATEWAY_API_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "TEMPORAL_API_KEY",
    )
    return [os.environ[n] for n in names if len(os.environ.get(n, "")) >= 6]


EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])(?!redacted@)[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
WORKER_IDENTITY = re.compile(r"(?<![\w.%+-])\d+@[A-Za-z][\w-]*")  # pid@host, any host
FIXED_WORKER_IDENTITY = "1@worker"
MIN_NAME_CHARS = 3
URL_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/@\"'\\]+@")


def scrub_history(history_json: str) -> str:
    """Scrub a history JSON for committing (also usable on an existing file).

    - ``identity`` (``pid@host``) -> ``pytest-worker``; a ``pid@host[-hex]``
      worker identity inside any string (sticky task-queue names, payloads)
      -> ``1@worker`` (the shape the parse fixtures use);
    - ``stackTrace`` (absolute source paths) -> empty;
    - the local host name and user name inside any string (sticky task-queue
      names) -> ``pytest-host`` / ``pytest-user``.
    - e-mail addresses and the user info of URLs (``https://user:pw@host``),
      also inside the base64 payloads (they are decoded, masked, re-encoded).
    Replay does not depend on any of these fields.
    """

    host = socket.gethostname().split(".")[0]
    identity = re.compile(
        rf"(?<![\w.%+-])\d+@(?:{re.escape(host)}|pytest-host)(?:-[0-9a-f]{{8,}})*",
        re.IGNORECASE,
    )
    names = ((host, "pytest-host"), (getpass.getuser(), "pytest-user"))
    return json.dumps(_walk_history(json.loads(history_json), identity, names), indent=1, sort_keys=True)


def _scrub_history_text(value: str, identity: re.Pattern[str], names: tuple[tuple[str, str], ...]) -> str:
    value = identity.sub(FIXED_WORKER_IDENTITY, value)
    value = URL_USERINFO.sub(r"\1redacted@", value)
    value = EMAIL.sub("redacted@example.invalid", value)
    for needle, replacement in names:
        if needle and len(needle) >= MIN_NAME_CHARS:
            value = re.sub(re.escape(needle), replacement, value, flags=re.IGNORECASE)
    return value


def _scrub_history_payload(
    node: dict[str, Any], identity: re.Pattern[str], names: tuple[tuple[str, str], ...]
) -> dict[str, Any] | None:
    data = node.get("data")
    if not isinstance(data, str) or "metadata" not in node:
        return None
    try:
        decoded = base64.b64decode(data, validate=True).decode()
    except ValueError:  # binary or not base64: left as it is
        return None
    scrubbed = _scrub_history_text(decoded, identity, names)
    if scrubbed == decoded:
        return None
    return {**node, "data": base64.b64encode(scrubbed.encode()).decode()}


def _walk_history(node: Any, identity: re.Pattern[str], names: tuple[tuple[str, str], ...]) -> Any:
    if isinstance(node, dict):
        replaced = _scrub_history_payload(node, identity, names)
        if replaced is not None:
            return replaced
        out = {}
        for key, value in node.items():
            if key == "identity" and isinstance(value, str):
                out[key] = "pytest-worker"
            elif key == "stackTrace" and isinstance(value, str):
                out[key] = ""
            else:
                out[key] = _walk_history(value, identity, names)
        return out
    if isinstance(node, list):
        return [_walk_history(item, identity, names) for item in node]
    if isinstance(node, str):
        return _scrub_history_text(node, identity, names)
    return node


async def record_fixture(handle: WorkflowHandle, name: str, extra_secrets: Sequence[str] = ()) -> None:
    """Write ``summarize_<name>.json`` (``.json.gz`` above the size ceiling) when
    ``SPECFLOW_RECORD_FIXTURES=1`` (scrubbed)."""

    if os.environ.get(RECORD_ENV) != "1":
        return
    history = await handle.fetch_history()
    text = scrub_history(history.to_json())
    for value in [*secret_values(), *extra_secrets]:
        assert value not in text, "a secret value would be written to a fixture"
    FIXTURES.mkdir(parents=True, exist_ok=True)
    plain = FIXTURES / f"summarize_{name}.json"
    compressed = plain.with_name(plain.name + COMPRESSED_SUFFIX)
    encoded = (text + "\n").encode()
    if len(encoded) > FIXTURE_COMPRESS_ABOVE_BYTES:
        compressed.write_bytes(gzip.compress(encoded, mtime=0))
        plain.unlink(missing_ok=True)
    else:
        plain.write_bytes(encoded)
        compressed.unlink(missing_ok=True)


def summarize_fixture_path(name: str) -> Path:
    """The recorded ``summarize_<name>`` history: ``.json.gz`` when compressed, else ``.json``."""

    plain = FIXTURES / f"summarize_{name}.json"
    compressed = plain.with_name(plain.name + COMPRESSED_SUFFIX)
    return compressed if compressed.exists() else plain


def fixture_name(path: Path) -> str:
    """``summarize_<name>.json[.gz]`` -> ``<name>``."""

    return path.name.removesuffix(COMPRESSED_SUFFIX).removesuffix(".json").removeprefix("summarize_")


def read_fixture_text(path: Path) -> str:
    """A recorded history as JSON text, decompressing a ``.json.gz`` fixture."""

    if path.suffix == COMPRESSED_SUFFIX:
        return gzip.decompress(path.read_bytes()).decode()
    return path.read_text()
