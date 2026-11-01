"""Tier 1: the Drive import of the parser formats, without Temporal or Postgres.

Scenario (one file of each type, through the fake Drive and the real parse activity):

    Drive listing --categorize--> item row (category, class)
    activity ``drive_import_item`` --_kind--> "pdf" | "document" | "google" | "stored"
    fake Drive --bytes--> object store ``original.<ext>`` (canonical media type) --> document row
    parse activity (real ``conversion.convert`` + Docling) <-- the stored key and media type of that row

Sequence:  worker -> Drive: iter_download(id) -> store: original.<ext> -> db: insert(media_type)
           worker -> parse: parse_document -> store: download -> Docling -> db: mark_parsed

The hostile files of G12 are in ``test_drive_import_hostile.py``; the time limits are at the end of this file.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from temporalio.testing import ActivityEnvironment

from db import repositories
from drive.categories import categorize
from drive_import_offline_support import (
    CID,
    PID,
    Repo,
    Served,
    build,
    data,
    kind_of,
    mixed_drive,
    row_of,
    run_activity,
)
from file_parsers.engines.docling import engine_id
from specflow_parse_support import FakeRepository, FakeSessions, install_fake_repository
from storage.keys import document_original, document_original_bin
from storage.memory import InMemoryObjectStore
from temporal import ids
from temporal.documents import workflows as parse_workflows
from temporal.documents.activities import ParseActivities
from temporal.documents.models import ParseDocumentInput
from temporal.sources import activities as acts
from temporal.sources import workflows as wf
from temporal.sources.models import WaitDocumentInput

OFFICE = "application/vnd.openxmlformats-officedocument."
DOCX = OFFICE + "wordprocessingml.document"
PPTX = OFFICE + "presentationml.presentation"
XLSX = OFFICE + "spreadsheetml.sheet"
G = "application/vnd.google-apps."

#: (Drive MIME type, file name on Drive, key extension): the six types that become documents.
NEW_TYPES = [
    (DOCX, "Spec.docx", ".docx"),
    (PPTX, "Deck.pptx", ".pptx"),
    (XLSX, "Budget.xlsx", ".xlsx"),
    ("text/html", "Page.html", ".html"),
    ("text/markdown", "Notes.md", ".md"),
    ("text/csv", "Data.csv", ".csv"),
]
#: Listed too, and never read: a legacy .doc, a .md that Drive calls text/plain, a zip.
STORED = [
    ("application/msword", "Old.doc"),
    ("application/vnd.ms-powerpoint", "Old.ppt"),
    ("application/vnd.ms-excel", "Old.xls"),
    ("text/plain", "Readme.md"),
    ("application/zip", "Bundle.zip"),
]


@pytest.fixture
def pool():
    executor = ThreadPoolExecutor(2)
    yield executor
    executor.shutdown(wait=False)


@pytest.fixture
def store():
    return InMemoryObjectStore(app_env="local")


# --- the routing decision ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mime", "kind"),
    [
        ("application/pdf", "pdf"),
        *[(mime, "document") for mime, _, _ in NEW_TYPES],
        *[(mime, "stored") for mime, _ in STORED],
        (G + "document", "google"),
        (G + "presentation", "google"),
        (G + "spreadsheet", "google"),
    ],
)
def test_the_kind_follows_the_class_and_the_mime_type(mime, kind):
    category, klass = categorize(mime)
    row = SimpleNamespace(category=category.value, processing_class=klass.value, mime_type=mime)
    assert kind_of(row) == kind


@pytest.mark.parametrize(
    "row",
    [
        SimpleNamespace(category="other_files", processing_class="processed", mime_type="application/zip"),
        SimpleNamespace(category="word", processing_class="processed", mime_type="application/msword"),
        SimpleNamespace(category="other_google", processing_class="forwarded", mime_type=G + "form"),
    ],
)
def test_a_row_without_a_reader_has_no_kind(row):
    assert kind_of(row) is None


# --- each new type: Drive -> store -> document row -> real parse -----------------------------------------------
@pytest.mark.parametrize(("mime", "name", "extension"), NEW_TYPES, ids=[t[2] for t in NEW_TYPES])
async def test_a_file_of_each_new_type_becomes_a_parsed_document(
    *, mime, name, extension, tmp_path, monkeypatch, store, pool
):
    drive = mixed_drive(tmp_path, [mime])
    activities, _ = build(store, pool, drive)
    item = row_of(0, mime)
    repo = Repo(monkeypatch, item)
    kind = kind_of(item)
    assert kind == "document"

    result = await run_activity(activities, activities.drive_import_item, data(item, kind))

    document_id = ids.drive_document_id(CID, item.external_id)
    assert (result.action, result.document_id) == ("parse", document_id)
    key = document_original(str(PID), str(document_id), mime)
    assert key.endswith(f"original{extension}")
    assert store.content_types[key] == mime  # the canonical type, never the generic "octet-stream"
    [inserted] = repo.inserted
    assert (inserted["media_type"], inserted["original_key"], inserted["status"]) == (mime, key, "processing")
    assert repo.finished == []  # the workflow finishes the file after the child parse

    # The parse workflow's activity reads that row and that object.
    parse_repo = FakeRepository()
    parse_repo.media_type, parse_repo.original_key = inserted["media_type"], inserted["original_key"]
    install_fake_repository(monkeypatch, parse_repo)
    parse = ParseActivities(FakeSessions(), store, pool)
    arg = ParseDocumentInput(project_id=PID, document_id=document_id)
    await ActivityEnvironment().run(parse.parse_document, arg)

    ((markdown, parser),) = parse_repo.parsed
    assert parser == engine_id()
    assert item.external_id in markdown  # each fake file names its Drive id


@pytest.mark.parametrize(("mime", "name"), STORED, ids=[s[1] for s in STORED])
async def test_a_legacy_or_unknown_file_is_stored_only_without_a_document(
    *, mime, name, tmp_path, monkeypatch, store, pool
):
    drive = mixed_drive(tmp_path, [mime])
    activities, _ = build(store, pool, drive)
    item = row_of(0, mime)
    repo = Repo(monkeypatch, item)
    kind = kind_of(item)
    assert kind == "stored"

    result = await run_activity(activities, activities.drive_import_item, data(item, kind))

    assert result.action == "done"
    document_id = ids.drive_document_id(CID, item.external_id)
    assert list(store.objects) == [document_original_bin(str(PID), str(document_id))]
    assert repo.inserted == []
    assert [f["state"] for f in repo.finished] == ["stored"]


async def test_a_google_doc_keeps_drive_convert(tmp_path, monkeypatch, store, pool):
    drive = mixed_drive(tmp_path, [G + "document"])
    drive.payload = {"placeholder": "payload"}
    activities, _ = build(store, pool, drive, max_markdown_bytes=1024)
    monkeypatch.setattr(acts, "_convert", lambda _mime, _payload, _cap: "# Converted\n")
    item = row_of(0, G + "document")
    repo = Repo(monkeypatch, item)
    assert kind_of(item) == "google"

    await run_activity(activities, activities.drive_import_item, data(item, "google"))

    [inserted] = repo.inserted
    assert (inserted["parser"], inserted["media_type"], inserted["status"]) == (
        acts.CONVERT_PARSER,
        "application/json",
        "parsed",
    )
    assert acts.CONVERT_PARSER == "drive-convert"


async def test_a_processed_row_with_no_parser_type_ends_as_a_fixed_error_not_a_crash(
    tmp_path, monkeypatch, store, pool
):
    drive = mixed_drive(tmp_path, ["application/zip"])
    activities, _ = build(store, pool, drive)
    item = row_of(0, "application/zip")
    repo = Repo(monkeypatch, item)

    result = await run_activity(activities, activities.drive_import_item, data(item, "document"))

    assert result.action == "done" and store.objects == {} and repo.inserted == []
    assert [(f["state"], f["error_reason"], f["error_step"]) for f in repo.finished] == [
        ("error", "unknown_error", "download")
    ]


# --- the same file in two folders ---------------------------------------------------------------------------
class DedupRepo(Repo):
    """Like the database: one document per content hash in the project."""

    def __init__(self, monkeypatch, item):
        super().__init__(monkeypatch, item)
        self.by_hash: dict[str, SimpleNamespace] = {}

    async def insert(self, _session, *, file, **kw):
        known = self.by_hash.get(file.content_hash)
        if known is not None:
            return known, False
        row = SimpleNamespace(
            id=file.document_id, status=kw["status"], source_id=kw["source_id"], media_type=kw["media_type"]
        )
        self.by_hash[file.content_hash] = row
        return row, True


@pytest.mark.parametrize(
    ("first_status", "action", "state"), [("processing", "wait", None), ("parsed", "done", "processed")]
)
async def test_the_same_docx_in_two_folders_is_one_document_and_one_object(
    *, first_status, action, state, tmp_path, monkeypatch, store, pool
):
    drive = mixed_drive(tmp_path, [DOCX, DOCX], Served)
    drive.content = b"the same bytes in both folders"
    activities, _ = build(store, pool, drive)
    first, second = row_of(0, DOCX), row_of(1, DOCX)
    repo = DedupRepo(monkeypatch, first)

    one = await run_activity(activities, activities.drive_import_item, data(first, "document"))
    assert one.action == "parse"
    repo.by_hash[next(iter(repo.by_hash))].status = first_status
    repo.item = second
    two = await run_activity(activities, activities.drive_import_item, data(second, "document"))

    first_id = ids.drive_document_id(CID, first.external_id)
    assert (two.action, two.document_id if action == "wait" else None) == (
        action,
        first_id if action == "wait" else None,
    )
    assert len(repo.by_hash) == 1
    assert list(store.objects) == [document_original(str(PID), str(first_id), DOCX)]  # no second copy
    assert [f["state"] for f in repo.finished] == ([state] if state else [])


# --- limits (DESIGN 'Long documents and time limits') -------------------------------------------------------------
def test_the_drive_wait_is_not_below_the_parse_execution_limit_and_the_wait_activity_is_above_the_wait():
    execution = parse_workflows.EXECUTION_TIMEOUT
    assert timedelta(seconds=acts.WAIT_LIMIT_SECONDS) >= execution
    assert timedelta(seconds=acts.WAIT_LIMIT_SECONDS) < wf.WAIT_START_TO_CLOSE
    assert execution == wf.PARSE_EXECUTION_TIMEOUT  # the child parse the import starts has that limit


def test_the_limits_are_the_designed_values():
    assert acts.WAIT_LIMIT_SECONDS == 250 * 60
    assert timedelta(minutes=255) == wf.WAIT_START_TO_CLOSE


async def test_the_wait_gives_up_exactly_at_the_wait_limit(monkeypatch, store, pool):
    """A parse still ``processing`` after 250 min (fake clock) is a ``timeout``; at 249 min 59 s it goes on waiting."""

    activities, _ = build(store, pool)
    activities._wait_interval = 0
    clock = iter([0.0, acts.WAIT_LIMIT_SECONDS - 1, acts.WAIT_LIMIT_SECONDS])
    reads = []

    async def state(_session, _project_id, _document_id):
        reads.append(1)
        return SimpleNamespace(status="processing")

    monkeypatch.setattr(repositories, "get_document_state", state)
    monkeypatch.setattr(acts, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    arg = WaitDocumentInput(project_id=PID, document_id=uuid.uuid4())

    done = await run_activity(activities, activities.drive_wait_document, arg)

    assert done.status == "timeout"
    assert len(reads) == 2  # read at start, read again at limit - 1 s, then the clock reached the limit
