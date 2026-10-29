"""Tier 1: the sources-ux read and batch API (INT-SRC-0001, INT-SRC-0002, INT-SRC-0004).

Requirements: INT-SRC-0001..0004, NFR-SEC-0002, NFR-MAIN-0002, FR-SRC-0019 (404
semantics). The database is the in-memory ``FakeBatches`` (pure rules are the real
ones); the SQL is covered by ``test_db_*_live.py`` and ``test_specflow_api_sources_live.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import errors, schemas
from db.batches import BatchEndedError, LockTimeoutError
from logger_harness import events, fields
from specflow_api_support import PDF, bearer
from specflow_sources_support import new_project

CONTRACT = json.loads((Path(__file__).parents[1] / "src/api/fixtures/sources-contract.json").read_text())
PDF_HEADERS = {"Content-Type": "application/pdf"}
ALICE, BOB = "alice@example.com", "bob@example.com"


def pdf(n: int) -> bytes:
    return PDF + str(n).encode()


async def project(harness, email: str = ALICE) -> str:
    response = await harness.client.post("/api/v1/projects", json={"name": "p"}, headers=bearer(email))
    return response.json()["id"]


def code(response) -> str:
    return response.json()["error"]["code"]


def url(pid: str, tail: str = "") -> str:
    return f"/api/v1/projects/{pid}/sources{tail}"


async def start(harness, pid, expected=3, email=ALICE):
    return await harness.client.post(url(pid, "/uploads"), json={"expected_files": expected}, headers=bearer(email))


async def add(harness, pid, sid, content=None, name="a.pdf", *, email=ALICE, headers=None):
    return await harness.client.post(
        url(pid, f"/{sid}/files?filename={name}"),
        content=pdf(1) if content is None else content,
        headers={**bearer(email), **(headers or PDF_HEADERS)},
    )


async def end(harness, pid, sid, email=ALICE):
    return await harness.client.post(url(pid, f"/{sid}/end"), headers=bearer(email))


async def files(harness, pid, sid, query="", email=ALICE):
    return await harness.client.get(url(pid, f"/{sid}/files{query}"), headers=bearer(email))


def parse_calls(harness):
    return [c for c in harness.temporal.calls if c["workflow"] == "ParseDocumentWorkflow"]


# ---- the contract ---------------------------------------------------------------


def test_the_pydantic_models_match_the_shared_contract_file():
    """INT-SRC-0001/0002: the JSON keys are the contract's (the web app checks its copy too)."""

    assert list(schemas.SourceSummaryOut.model_fields) == CONTRACT["source_fields"]
    assert list(schemas.FileOut.model_fields) == CONTRACT["file_fields"]
    assert list(schemas.HolderOut.model_fields) == CONTRACT["holder_fields"]
    assert list(schemas.CountsOut.model_fields) == CONTRACT["file_states"]
    assert list(schemas.FilePageOut.model_fields) == ["source", "items", "next_cursor"]
    assert list(schemas.SourcesStatusV2Out.model_fields) == [
        "progress",
        "processed_documents",
        "removed_files",
        "sources",
    ]
    assert list(schemas.BatchFileOut.model_fields) == ["outcome", "file", "holder"]

    def literals(annotation):
        return set(annotation.__args__)

    assert literals(schemas.FileStateName) == set(CONTRACT["file_states"])
    assert literals(schemas.BatchOutcomeName) == set(CONTRACT["outcomes"])
    assert CONTRACT["page_limit"] == 200


def test_the_error_codes_of_the_contract_exist_in_the_api():

    assert errors.source_not_found().code == "source_not_found"
    assert (errors.source_not_found().status, errors.batch_ended().status) == (404, 409)
    assert errors.removal_unavailable().status == 503
    assert {"source_not_found", "batch_ended", "removal_unavailable"} <= set(CONTRACT["error_codes"])


# ---- start ------------------------------------------------------------------------


async def test_start_opens_a_batch_and_answers_the_summary(harness, caplog):
    pid = await project(harness)
    with caplog.at_level("INFO"):
        response = await start(harness, pid, 8)
    assert response.status_code == 201
    source = response.json()["source"]
    assert list(source) == CONTRACT["source_fields"]
    assert (source["kind"], source["name"], source["name_suffix"]) == ("upload", "Local files", 1)
    assert (source["open"], source["listing_finished"], source["total"]) == (True, True, 8)
    assert source["counts"]["waiting"] == 8 and source["folder_id"] is None and source["link"] is None
    assert source["added_by"]["display_name"] == "Alice"
    [record] = events(caplog, "api.batch.created")
    logged = fields(record)
    assert logged["source_id"] == source["id"] and logged["file_count"] == 8
    assert (logged["kind"], logged["outcome"]) == ("upload", "created")


async def test_two_batches_in_one_minute_get_the_next_suffix(harness):
    pid = await project(harness)
    first = (await start(harness, pid)).json()["source"]
    second = (await start(harness, pid)).json()["source"]
    assert (first["name_suffix"], second["name_suffix"]) == (1, 2)


@pytest.mark.parametrize(
    "body",
    [
        {"expected_files": 0},
        {"expected_files": 10001},
        {"expected_files": "5"},
        {"expected_files": 1.5},
        {},
        {"expected_files": 1, "extra": 1},
    ],
)
async def test_a_bad_expected_files_is_422_validation_failed(harness, body):
    pid = await project(harness)
    response = await harness.client.post(url(pid, "/uploads"), json=body, headers=bearer())
    assert response.status_code == 422 and code(response) == "validation_failed"
    assert not harness.batches.sources


async def test_the_bounds_of_expected_files_are_accepted(harness):
    pid = await project(harness)
    assert (await start(harness, pid, 1)).status_code == 201
    assert (await start(harness, pid, 10000)).status_code == 201


async def test_start_retries_once_on_a_lock_conflict_then_gives_up(harness):
    pid = await project(harness)
    harness.batches.conflicts = 1
    assert (await start(harness, pid)).status_code == 201
    harness.batches.conflicts = 2
    response = await start(harness, pid)
    assert response.status_code == 503 and code(response) == "database_unavailable"


# ---- add --------------------------------------------------------------------------


async def test_add_streams_a_pdf_into_the_batch_and_starts_the_parse(harness, caplog):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    with caplog.at_level("INFO"):
        response = await add(harness, pid, sid, name="Secret Name.pdf")
    assert response.status_code == 201
    body = response.json()
    assert list(body) == ["outcome", "file", "holder"] and body["outcome"] == "added"
    assert list(body["file"]) == CONTRACT["file_fields"] and body["holder"] is None
    assert (body["file"]["name"], body["file"]["state"], body["file"]["category"]) == (
        "Secret Name.pdf",
        "processing",
        "pdf",
    )
    [doc] = harness.repo.documents.values()
    assert str(doc.source_id) == sid and doc.status == "processing"
    assert harness.store.objects[doc.original_key] == pdf(1)
    [call] = parse_calls(harness)
    assert call["id"] == f"doc-parse-{doc.id}"
    [uploaded] = events(caplog, "api.document.uploaded")
    logged = fields(uploaded)
    assert (logged["source_id"], logged["outcome"]) == (sid, "added")
    assert logged["document_id"] == str(doc.id) and logged["project_id"] == pid
    assert "Secret Name" not in caplog.text


async def test_the_open_batch_counts_the_files_still_to_come(harness):
    pid = await project(harness)
    sid = (await start(harness, pid, 3)).json()["source"]["id"]
    await add(harness, pid, sid, pdf(1))
    source = (await files(harness, pid, sid)).json()["source"]
    assert source["open"] is True and source["total"] == 3
    assert (source["counts"]["processing"], source["counts"]["waiting"]) == (1, 2)


async def test_the_same_content_twice_in_a_batch_is_a_duplicate_with_no_file(harness):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    await add(harness, pid, sid)
    again = await add(harness, pid, sid, name="again.pdf")
    assert again.status_code == 200
    assert again.json() == {"outcome": "duplicate", "file": None, "holder": None}
    assert len(harness.repo.documents) == 1 and len(harness.store.objects) == 1
    assert len(parse_calls(harness)) == 1


async def test_content_held_by_another_source_is_already_in_project_with_its_holder(harness):
    pid = await project(harness)
    first = (await start(harness, pid)).json()["source"]["id"]
    await add(harness, pid, first)
    [doc] = harness.repo.documents.values()
    doc.status = "parsed"
    second = (await start(harness, pid)).json()["source"]
    response = await add(harness, pid, second["id"], name="same.pdf")
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "already_in_project"
    assert list(body["holder"]) == CONTRACT["holder_fields"]
    assert body["holder"]["id"] == first and body["holder"]["kind"] == "upload"
    assert body["file"]["state"] == "already_in_project"
    assert body["file"]["reason"] == "already_in_project"
    assert body["file"]["holder"] == body["holder"]
    assert list(harness.store.objects) == [doc.original_key]  # the new object is deleted
    assert len(parse_calls(harness)) == 1


async def test_a_failed_file_of_an_older_batch_is_read_again_here(harness):
    pid = await project(harness)
    first = (await start(harness, pid, 1)).json()["source"]["id"]
    await add(harness, pid, first)
    [doc] = harness.repo.documents.values()
    doc.status, doc.error = "failed", "pdf_parse_failed"
    await end(harness, pid, first)
    harness.temporal.calls.clear()
    second = (await start(harness, pid)).json()["source"]["id"]
    response = await add(harness, pid, second, name="again.pdf")
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "read_again" and body["holder"] is None
    assert body["file"]["state"] == "processing"
    assert (str(doc.source_id), doc.status) == (second, "processing")
    assert [c["id"] for c in parse_calls(harness)] == [f"doc-parse-{doc.id}"]
    assert list(harness.store.objects) == [doc.original_key]  # never the document's own object
    assert uuid.UUID(first) not in harness.batches.sources  # the emptied old batch is gone


async def test_a_parse_start_that_fails_marks_the_file_failed_and_stays_201(harness):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    harness.temporal.fail = RuntimeError("temporal down")
    response = await add(harness, pid, sid)
    assert response.status_code == 201
    assert response.json()["file"]["state"] == "failed"
    assert response.json()["file"]["reason"] == "unknown"
    [doc] = harness.repo.documents.values()
    assert (doc.status, doc.error) == ("failed", "workflow_start_failed")
    assert doc.original_key in harness.store.objects


async def test_adding_to_an_ended_batch_is_409_batch_ended_and_leaves_nothing(harness):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    await add(harness, pid, sid, pdf(1))
    await end(harness, pid, sid)
    response = await add(harness, pid, sid, pdf(2))
    assert response.status_code == 409 and code(response) == "batch_ended"
    assert len(harness.repo.documents) == 1 and len(harness.store.objects) == 1


async def test_a_batch_closed_by_the_fifteen_minute_rule_is_409_too(harness):
    pid = await project(harness)
    source = harness.batches.add_source(uuid.UUID(pid), ended=False, expected=2, age=timedelta(minutes=16))
    old = harness.batches.add_item(source, "a.pdf")  # a batch with a file is still shown
    old.created_at -= timedelta(minutes=16)
    response = await add(harness, pid, source.id)
    assert response.status_code == 409 and code(response) == "batch_ended"


async def test_an_empty_batch_past_the_fifteen_minute_rule_is_hidden_so_404(harness):
    pid = await project(harness)
    source = harness.batches.add_source(uuid.UUID(pid), ended=False, expected=2, age=timedelta(minutes=16))
    response = await add(harness, pid, source.id)
    assert response.status_code == 404 and code(response) == "source_not_found"


async def test_adding_to_a_drive_source_is_409_and_to_an_unknown_one_404(harness):
    pid = await project(harness)
    drive = harness.batches.add_source(uuid.UUID(pid), kind="gdrive")
    response = await add(harness, pid, drive.id)
    assert response.status_code == 409 and code(response) == "batch_ended"
    response = await add(harness, pid, uuid.uuid4())
    assert response.status_code == 404 and code(response) == "source_not_found"
    assert not harness.store.objects and not harness.repo.documents


async def test_add_checks_the_upload_like_the_legacy_route(harness):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    wrong = await add(harness, pid, sid, headers={"Content-Type": "text/plain"})
    assert (wrong.status_code, code(wrong)) == (415, "unsupported_format")
    magic = await add(harness, pid, sid, b"GIF89a")
    assert (magic.status_code, code(magic)) == (415, "unsupported_format")
    empty = await add(harness, pid, sid, b"")
    assert (empty.status_code, code(empty)) == (400, "empty_file")
    nameless = await harness.client.post(url(pid, f"/{sid}/files"), content=PDF, headers={**bearer(), **PDF_HEADERS})
    assert (nameless.status_code, code(nameless)) == (400, "missing_filename")
    assert not harness.store.objects and not harness.repo.documents


async def test_add_retries_once_on_a_lock_conflict_then_503(harness):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    harness.batches.conflicts = 1
    ok = await add(harness, pid, sid, pdf(1))
    assert ok.status_code == 201 and len(harness.repo.documents) == 1
    assert harness.batches.calls.count("add_batch_file") == 2
    harness.batches.conflicts = 2
    failed = await add(harness, pid, sid, pdf(2))
    assert failed.status_code == 503 and code(failed) == "database_unavailable"
    assert len(harness.repo.documents) == 1 and len(harness.store.objects) == 1
    assert harness.batches.calls.count("add_batch_file") == 4


async def test_a_lock_timeout_on_add_is_503_try_again_and_never_source_not_found(harness):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    harness.batches.raises["add_batch_file"] = [LockTimeoutError("slow")]
    response = await add(harness, pid, sid)
    assert response.status_code == 503 and code(response) == "database_unavailable"
    assert not harness.store.objects  # the streamed object is deleted


async def test_an_unexpected_failure_after_the_stream_deletes_the_object(harness):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    harness.batches.raises["add_batch_file"] = [RuntimeError("db down")]
    response = await add(harness, pid, sid)
    assert response.status_code == 500 and not harness.store.objects


async def test_a_failing_object_delete_after_the_commit_does_not_change_the_answer(harness, monkeypatch):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    await add(harness, pid, sid)

    async def boom(key):
        raise OSError("gcs down")

    monkeypatch.setattr(harness.store, "delete", boom)
    again = await add(harness, pid, sid, name="again.pdf")
    assert again.status_code == 200 and again.json()["outcome"] == "duplicate"


# ---- end --------------------------------------------------------------------------


async def test_end_closes_the_batch_and_is_idempotent(harness, caplog):
    pid = await project(harness)
    sid = (await start(harness, pid, 5)).json()["source"]["id"]
    await add(harness, pid, sid, pdf(1))
    await add(harness, pid, sid, pdf(2))
    with caplog.at_level("INFO"):
        response = await end(harness, pid, sid)
    assert response.status_code == 200
    source = response.json()["source"]
    assert list(source) == CONTRACT["source_fields"]
    assert (source["open"], source["total"]) == (False, 2)  # the card counts the accepted files
    assert source["counts"]["processing"] == 2
    assert (await end(harness, pid, sid)).json()["source"]["total"] == 2
    ended = fields(events(caplog, "api.batch.ended")[0])
    assert (ended["source_id"], ended["file_count"], ended["outcome"]) == (sid, 2, "ended")


async def test_the_end_of_an_empty_batch_is_204_and_the_batch_is_gone(harness, caplog):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    with caplog.at_level("INFO"):
        response = await end(harness, pid, sid)
    assert response.status_code == 204 and response.content == b""
    assert fields(events(caplog, "api.batch.ended")[0])["outcome"] == "deleted_empty"
    assert (await files(harness, pid, sid)).status_code == 404
    listed = await harness.client.get(url(pid, "?batches=1"), headers=bearer())
    assert listed.json()["sources"] == []


async def test_end_of_an_unknown_or_drive_source_is_404_source_not_found(harness):
    pid = await project(harness)
    drive = harness.batches.add_source(uuid.UUID(pid), kind="gdrive")
    for sid in (uuid.uuid4(), drive.id):
        response = await end(harness, pid, sid)
        assert response.status_code == 404 and code(response) == "source_not_found"


async def test_end_retries_once_on_a_lock_conflict(harness):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    await add(harness, pid, sid)
    harness.batches.conflicts = 1
    assert (await end(harness, pid, sid)).status_code == 200
    harness.batches.conflicts = 2
    assert (await end(harness, pid, sid)).status_code == 503


# ---- files ------------------------------------------------------------------------


async def test_the_file_list_pages_by_200_without_repeats(harness):
    pid = await project(harness)
    source = harness.batches.add_source(uuid.UUID(pid))
    for n in range(450):
        harness.batches.add_item(source, f"f{n:03d}.pdf", state="done")
    seen, cursor, sizes = [], None, []
    while True:
        response = await files(harness, pid, source.id, f"?cursor={cursor}" if cursor else "")
        assert response.status_code == 200
        body = response.json()
        assert list(body) == ["source", "items", "next_cursor"]
        assert body["source"]["total"] == 450
        sizes.append(len(body["items"]))
        seen += [item["id"] for item in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert sizes == [200, 200, 50] and len(set(seen)) == 450


async def test_the_file_list_orders_problems_then_processing_waiting_done(harness):
    pid = await project(harness)
    source = harness.batches.add_source(uuid.UUID(pid), kind="gdrive")
    for name, state, reason in [
        ("d.pdf", "done", None),
        ("w.pdf", "waiting", None),
        ("p.pdf", "processing", None),
        ("f.pdf", "failed", "read_failed"),
        ("n.pdf", "not_read", "not_readable"),
    ]:
        harness.batches.add_item(source, name, state=state, reason=reason)
    items = (await files(harness, pid, source.id)).json()["items"]
    assert [i["name"] for i in items] == ["f.pdf", "n.pdf", "p.pdf", "w.pdf", "d.pdf"]
    assert list(items[0]) == CONTRACT["file_fields"]
    assert (items[0]["state"], items[0]["reason"]) == ("failed", "read_failed")


@pytest.mark.parametrize("query", ["?limit=0", "?limit=201", "?limit=x", "?cursor=!!!", "?cursor=e30"])
async def test_a_bad_limit_or_cursor_is_422_validation_failed(harness, query):
    pid = await project(harness)
    source = harness.batches.add_source(uuid.UUID(pid))
    harness.batches.add_item(source, "a.pdf", state="done")
    response = await files(harness, pid, source.id, query)
    assert response.status_code == 422 and code(response) == "validation_failed"


async def test_the_limit_bounds_are_accepted(harness):
    pid = await project(harness)
    source = harness.batches.add_source(uuid.UUID(pid))
    harness.batches.add_item(source, "a.pdf", state="done")
    for limit in (1, 200):
        assert (await files(harness, pid, source.id, f"?limit={limit}")).status_code == 200


async def test_a_hidden_empty_batch_and_an_unknown_id_are_source_not_found(harness):
    pid = await project(harness)
    hidden = harness.batches.add_source(uuid.UUID(pid))  # ended, no file
    for sid in (hidden.id, uuid.uuid4()):
        response = await files(harness, pid, sid)
        assert response.status_code == 404 and code(response) == "source_not_found"
    assert (await files(harness, pid, "not-a-uuid")).status_code == 422


async def test_an_already_in_project_row_names_its_holder(harness):
    pid = await project(harness)
    first = (await start(harness, pid)).json()["source"]["id"]
    await add(harness, pid, first)
    [doc] = harness.repo.documents.values()
    doc.status = "parsed"
    second = (await start(harness, pid)).json()["source"]["id"]
    await add(harness, pid, second, name="same.pdf")
    [item] = (await files(harness, pid, second)).json()["items"]
    assert item["state"] == "already_in_project"
    assert item["holder"]["id"] == first and list(item["holder"]) == CONTRACT["holder_fields"]


# ---- status v2 and legacy -----------------------------------------------------------


async def test_status_with_the_flag_answers_the_unified_cards(harness):
    pid = await project(harness)
    drive = harness.batches.add_source(uuid.UUID(pid), kind="gdrive", age=timedelta(hours=1))
    for n, state in enumerate(["done", "waiting", "failed"]):
        harness.batches.add_item(drive, f"d{n}.pdf", state=state, reason="read_failed" if state == "failed" else None)
    sid = (await start(harness, pid, 4)).json()["source"]["id"]
    await add(harness, pid, sid)
    harness.batches.removed[uuid.UUID(pid)] = 7
    response = await harness.client.get(url(pid, "?batches=1"), headers=bearer())
    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["progress", "processed_documents", "removed_files", "sources"]
    assert body["removed_files"] == 7 and body["processed_documents"] == 0
    assert [s["kind"] for s in body["sources"]] == ["gdrive", "upload"]  # added order
    assert [s["total"] for s in body["sources"]] == [3, 4]
    # finished = total - waiting - processing: 7 - (1 drive waiting + 1 + 3 of the batch)
    assert body["progress"] == {"listing_finished": True, "total": 7, "finished": 2}


async def test_status_waits_for_a_listing_source(harness):
    pid = await project(harness)
    harness.batches.add_source(uuid.UUID(pid), kind="gdrive", listing=True)
    body = (await harness.client.get(url(pid, "?batches=1"), headers=bearer())).json()
    assert body["progress"]["listing_finished"] is False and body["sources"][0]["listing_finished"] is False


@pytest.mark.parametrize("query", ["", "?batches=0", "?batches=2", "?batches="])
async def test_status_without_the_flag_keeps_the_legacy_shape(sources, query):
    pid = await new_project(sources)
    response = await sources.client.get(url(pid, query), headers=bearer())
    assert response.status_code == 200
    assert set(response.json()) == {"progress", "sources", "uploads", "processed_documents"}


# ---- legacy upload: the per-UTC-day batch -------------------------------------------


async def legacy(harness, pid, content, name="a.pdf", email=ALICE):
    return await harness.client.post(
        f"/api/v1/projects/{pid}/documents?filename={name}",
        content=content,
        headers={**bearer(email), **PDF_HEADERS},
    )


async def test_legacy_uploads_of_one_day_land_in_one_ended_day_batch(harness):

    pid = await project(harness)
    one = (await legacy(harness, pid, pdf(1))).json()["document"]
    two = (await legacy(harness, pid, pdf(2), "b.pdf")).json()["document"]
    [batch] = harness.batches.sources.values()
    assert batch.external_id == f"legacy-{datetime.now(UTC):%Y%m%d}"
    assert (batch.kind, batch.ended_at is not None) == ("upload", True)
    for did in (one["id"], two["id"]):
        assert harness.repo.documents[uuid.UUID(did)].source_id == batch.id
    assert len(harness.batches.items_of(batch.id)) == 2  # no document without a holder
    # the new status read shows it as a closed card with both files
    [card] = (await harness.client.get(url(pid, "?batches=1"), headers=bearer())).json()["sources"]
    assert (card["open"], card["total"]) == (False, 2)


async def test_legacy_upload_outcomes_map_as_before(harness):
    pid = await project(harness)
    created = await legacy(harness, pid, pdf(1))
    assert created.status_code == 201 and "outcome" not in created.json()
    dup = await legacy(harness, pid, pdf(1), "again.pdf")
    assert (dup.status_code, dup.json()["outcome"]) == (200, "duplicate")
    # held by an open batch of the new flow: already_in_project -> duplicate
    sid = (await start(harness, pid)).json()["source"]["id"]
    await add(harness, pid, sid, pdf(9))
    [new_doc] = [
        d
        for d in harness.repo.documents.values()
        if d.content_hash != created.json()["document"]["id"] and str(d.source_id) == sid
    ]
    other = await legacy(harness, pid, pdf(9), "x.pdf")
    assert (other.status_code, other.json()["outcome"]) == (200, "duplicate")
    assert other.json()["document"]["id"] == str(new_doc.id)
    # a failed file of a new-flow batch is read again by the legacy route: retried
    new_doc.status, new_doc.error = "failed", "pdf_parse_failed"
    await end(harness, pid, sid)
    retried = await legacy(harness, pid, pdf(9), "y.pdf")
    assert (retried.status_code, retried.json()["outcome"]) == (200, "retried")
    assert retried.json()["document"]["status"] == "processing"


async def test_the_typed_batch_errors_have_answers_on_the_legacy_route_too(harness):
    pid = await project(harness)
    harness.batches.raises["add_batch_file"] = [BatchEndedError()]
    response = await legacy(harness, pid, pdf(1))
    assert response.status_code == 409 and code(response) == "batch_ended"
    assert not harness.store.objects
    harness.batches.conflicts = 2
    response = await legacy(harness, pid, pdf(1))
    assert response.status_code == 503 and not harness.store.objects


# ---- membership and cross-project ids (NFR-SEC-0002) --------------------------------


async def test_a_non_member_gets_project_not_found_on_every_route(harness):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    await add(harness, pid, sid)
    await project(harness, BOB)  # bob exists, but is not a member of alice's project
    calls = [
        harness.client.get(url(pid, "?batches=1"), headers=bearer(BOB)),
        harness.client.get(url(pid, f"/{sid}/files"), headers=bearer(BOB)),
        harness.client.delete(url(pid, f"/{sid}"), headers=bearer(BOB)),
        harness.client.post(url(pid, "/uploads"), json={"expected_files": 1}, headers=bearer(BOB)),
        add(harness, pid, sid, pdf(5), email=BOB),
        end(harness, pid, sid, BOB),
    ]
    for pending in calls:
        response = await pending
        assert response.status_code == 404 and code(response) == "project_not_found"
    assert len(harness.repo.documents) == 1 and len(harness.batches.sources) == 1
    assert len(harness.store.objects) == 1


async def test_another_projects_source_id_is_source_not_found_and_nothing_changes(harness):
    mine, theirs = await project(harness), await project(harness)
    sid = (await start(harness, theirs)).json()["source"]["id"]
    await add(harness, theirs, sid)
    before = (len(harness.repo.documents), len(harness.store.objects), set(harness.batches.sources))
    responses = [
        await files(harness, mine, sid),
        await harness.client.delete(url(mine, f"/{sid}"), headers=bearer()),
        await add(harness, mine, sid, pdf(5)),
        await end(harness, mine, sid),
    ]
    for response in responses:
        assert response.status_code == 404 and code(response) == "source_not_found"
    after = (len(harness.repo.documents), len(harness.store.objects), set(harness.batches.sources))
    assert before == after
    assert all(e == 0 for e in harness.batches.removed.values())


async def test_no_log_event_of_the_batch_flow_carries_a_name_or_email(harness, caplog):
    pid = await project(harness, "carol.secret@example.com")
    with caplog.at_level("DEBUG"):
        sid = (await start(harness, pid, 2, "carol.secret@example.com")).json()["source"]["id"]
        await add(harness, pid, sid, pdf(1), "Confidential Plan.pdf", email="carol.secret@example.com")
        await end(harness, pid, sid, "carol.secret@example.com")
    assert "Confidential" not in caplog.text and "carol.secret" not in caplog.text
    for name in ("api.batch.created", "api.document.uploaded", "api.batch.ended"):
        assert events(caplog, name)


async def test_the_legacy_status_lists_drive_rows_only_even_with_batches_present(sources):
    """SPECS 4.1/9: batches have no link; the old ``SourceOut`` would fail on them."""

    pid = await new_project(sources)
    drive = (await sources.save("https://drive.google.com/drive/folders/fake-synthetic-30")).json()["source"]
    sources.repo.rows[uuid.uuid4()] = row = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.UUID(pid),
        kind="upload",
        external_id="legacy-20260101",
        link=None,
        name="Local files",
        state="done",
        failure_reason=None,
        connected_by=uuid.uuid4(),
        last_synced_at=None,
        created_at=sources.repo.rows[uuid.UUID(drive["id"])].created_at,
    )
    body = (await sources.status()).json()
    assert [s["id"] for s in body["sources"]] == [drive["id"]] and row.link is None


async def test_the_legacy_upload_log_gains_the_source_id_and_the_batch_outcome(harness, caplog):
    pid = await project(harness)
    with caplog.at_level("INFO"):
        await legacy(harness, pid, pdf(1))
        await legacy(harness, pid, pdf(1), "again.pdf")
    [batch] = harness.batches.sources.values()
    logged = [fields(r) for r in events(caplog, "api.document.uploaded")]
    assert [(f["outcome"], f["source_id"]) for f in logged] == [
        ("added", str(batch.id)),
        ("duplicate", str(batch.id)),
    ]
