"""Tier 1: raw-body PDF streaming upload (AC-7.5 to 7.7, N1)."""

from __future__ import annotations

import hashlib
from uuid import UUID

import pytest

from db import repositories as repo
from logger_harness import events, fields
from specflow_api_support import PDF, bearer
from storage.base import CHUNK_SIZE

PDF_HEADERS = {"Content-Type": "application/pdf"}


async def new_project(harness, email="alice@example.com") -> str:
    response = await harness.client.post("/api/v1/projects", json={"name": "p"}, headers=bearer(email))
    return response.json()["id"]


async def upload(
    harness,
    pid,
    content,
    *,
    email="alice@example.com",
    filename="spec.pdf",
    headers=None,
):
    url = f"/api/v1/projects/{pid}/documents" + (f"?filename={filename}" if filename else "")
    return await harness.client.post(url, content=content, headers={**bearer(email), **(headers or PDF_HEADERS)})


async def pieces(data: bytes, size: int):
    for start in range(0, len(data), size):
        yield data[start : start + size]


async def test_a_chunked_upload_stores_the_object_and_matches_hash_and_size(harness):
    pid = await new_project(harness)
    data = PDF * 5000  # ~700 KB in 1000-byte pieces
    response = await upload(harness, pid, pieces(data, 1000))
    assert response.status_code == 201
    document = response.json()["document"]
    assert document["status"] == "processing" and document["original_filename"] == "spec.pdf"
    assert document["size_bytes"] == len(data)
    key = f"projects/{pid}/documents/{document['id']}/original.pdf"
    assert harness.store.objects[key] == data
    assert harness.store.content_types[key] == "application/pdf"
    row = harness.repo.documents[UUID(document["id"])]
    assert row.content_hash == hashlib.sha256(data).hexdigest()
    assert row.original_key == key


async def test_the_writer_receives_chunks_of_at_most_a_small_multiple_of_4_mib(harness, monkeypatch):
    pid = await new_project(harness)
    sizes: list[int] = []
    original = harness.store.open_writer

    async def spy(key, *, content_type):
        writer = await original(key, content_type=content_type)
        inner = writer.write

        async def write(chunk):
            sizes.append(len(chunk))
            await inner(chunk)

        writer.write = write
        return writer

    monkeypatch.setattr(harness.store, "open_writer", spy)
    data = PDF + b"x" * (CHUNK_SIZE * 2 + 123)
    response = await upload(harness, pid, pieces(data, 64 * 1024))
    assert response.status_code == 201
    assert sum(sizes) == len(data) and len(sizes) == 3
    assert all(size < CHUNK_SIZE + 64 * 1024 for size in sizes)


async def test_the_filename_is_reduced_to_its_base_name(harness):
    pid = await new_project(harness)
    response = await upload(harness, pid, PDF, filename="..%2F..%2Fetc%2Freport%20v1.pdf")
    assert response.json()["document"]["original_filename"] == "report v1.pdf"


async def test_wrong_content_type_is_415_and_leaves_nothing(harness):
    pid = await new_project(harness)
    response = await upload(harness, pid, PDF, headers={"Content-Type": "text/plain"})
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_format"
    assert not harness.store.objects and not harness.repo.documents


@pytest.mark.parametrize("body", [b"GIF89a not a pdf", b"%PD", b"x"])
async def test_a_body_without_the_pdf_magic_is_415(harness, body):
    pid = await new_project(harness)
    response = await upload(harness, pid, body)
    assert response.status_code == 415
    assert not harness.store.objects and not harness.repo.documents


async def test_a_pdf_content_type_with_parameters_is_accepted(harness):
    pid = await new_project(harness)
    response = await upload(harness, pid, PDF, headers={"Content-Type": "Application/PDF; charset=binary"})
    assert response.status_code == 201


async def test_an_empty_body_is_400(harness):
    pid = await new_project(harness)
    response = await upload(harness, pid, b"")
    assert response.status_code == 400 and response.json()["error"]["code"] == "empty_file"
    assert not harness.store.objects and not harness.repo.documents


async def test_a_missing_filename_is_400(harness):
    pid = await new_project(harness)
    response = await upload(harness, pid, PDF, filename=None)
    assert response.status_code == 400 and response.json()["error"]["code"] == "missing_filename"


async def test_the_same_bytes_in_the_same_project_are_a_duplicate_and_the_new_object_is_deleted(
    harness,
):
    pid = await new_project(harness)
    first = (await upload(harness, pid, PDF)).json()["document"]
    second = await upload(harness, pid, PDF, filename="again.pdf")
    assert second.status_code == 200
    body = second.json()
    assert body["outcome"] == "duplicate" and body["document"]["id"] == first["id"]
    assert list(harness.store.objects) == [f"projects/{pid}/documents/{first['id']}/original.pdf"]
    assert len(harness.repo.documents) == 1


async def test_the_same_bytes_in_another_project_are_a_new_document(harness):
    one, two = await new_project(harness), await new_project(harness)
    assert (await upload(harness, one, PDF)).status_code == 201
    other = await upload(harness, two, PDF)
    assert other.status_code == 201 and len(harness.store.objects) == 2


async def test_a_database_failure_after_the_upload_deletes_the_object(harness, monkeypatch):

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    pid = await new_project(harness)
    monkeypatch.setattr(repo, "insert_document", boom)
    response = await upload(harness, pid, PDF)
    assert response.status_code == 500 and not harness.store.objects


async def test_the_document_listing_and_detail_follow_the_contract(harness):
    pid = await new_project(harness)
    created = (await upload(harness, pid, PDF)).json()["document"]
    listing = (await harness.client.get(f"/api/v1/projects/{pid}/documents", headers=bearer())).json()
    assert set(listing["items"][0]) == {
        "id",
        "original_filename",
        "size_bytes",
        "status",
        "error",
        "summarization_status",
        "created_at",
    }
    detail = (await harness.client.get(f"/api/v1/projects/{pid}/documents/{created['id']}", headers=bearer())).json()
    assert {
        "markdown",
        "markdown_truncated",
        "summarization",
        "summarization_model",
        "summarization_error",
        "summarized_at",
    } <= set(detail)
    assert "graph_node" not in detail
    missing = await harness.client.get(
        f"/api/v1/projects/{pid}/documents/00000000-0000-0000-0000-000000000009",
        headers=bearer(),
    )
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "document_not_found"


# AC-7.7: client abort. Driven at the ASGI level so the disconnect is exact.


async def call_asgi(app, path, messages, headers):
    sent: list[dict] = []
    queue = list(messages)

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"filename=cut.pdf",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    return sent


async def test_a_client_abort_mid_stream_leaves_no_object_and_no_row(harness, monkeypatch):
    pid = await new_project(harness)
    aborted: list[str] = []
    original = harness.store.open_writer

    async def spy(key, *, content_type):
        writer = await original(key, content_type=content_type)
        inner = writer.abort

        async def abort():
            aborted.append(key)
            await inner()

        writer.abort = abort
        return writer

    monkeypatch.setattr(harness.store, "open_writer", spy)
    body = [
        {"type": "http.request", "body": PDF, "more_body": True},
        {"type": "http.request", "body": b"y" * (CHUNK_SIZE + 10), "more_body": True},
        # then the client goes away: the next receive() is http.disconnect
    ]
    sent = await call_asgi(
        harness.app,
        f"/api/v1/projects/{pid}/documents",
        body,
        {**bearer(), **PDF_HEADERS, "host": "test"},
    )
    assert len(aborted) == 1
    assert not harness.store.objects and not harness.repo.documents
    assert sent[0]["status"] == 499


# K5: an upload starts its parse on the server -------------------------------


async def test_an_upload_starts_the_parse_workflow_and_marks_processing(harness):
    pid = await new_project(harness)
    document = (await upload(harness, pid, PDF)).json()["document"]
    assert document["status"] == "processing"
    [call] = harness.temporal.calls
    assert call["workflow"] == "ParseDocumentWorkflow"
    assert call["id"] == f"doc-parse-{document['id']}"
    row = harness.repo.documents[UUID(document["id"])]
    assert row.status == "processing"


async def test_a_duplicate_upload_does_not_start_another_parse(harness):
    pid = await new_project(harness)
    await upload(harness, pid, PDF)
    again = await upload(harness, pid, PDF, filename="again.pdf")
    assert again.status_code == 200 and again.json()["outcome"] == "duplicate"
    assert len(harness.temporal.calls) == 1 and harness.temporal.attempts == 1


async def test_a_duplicate_of_a_parsed_document_does_not_start_a_parse(harness):
    pid = await new_project(harness)
    first = (await upload(harness, pid, PDF)).json()["document"]
    harness.repo.documents[UUID(first["id"])].status = "parsed"
    harness.temporal.calls.clear()  # the first parse has finished
    again = await upload(harness, pid, PDF, filename="again.pdf")
    assert again.status_code == 200
    body = again.json()
    assert body["outcome"] == "duplicate" and body["document"]["status"] == "parsed"
    assert harness.temporal.calls == [] and harness.temporal.attempts == 1


async def test_a_duplicate_of_a_failed_document_parses_it_again(harness):
    pid = await new_project(harness)
    first = (await upload(harness, pid, PDF)).json()["document"]
    row = harness.repo.documents[UUID(first["id"])]
    row.status, row.error = "failed", "pdf_parse_failed"
    harness.temporal.calls.clear()  # the first parse has finished
    again = await upload(harness, pid, PDF, filename="again.pdf")
    assert again.status_code == 200
    body = again.json()
    assert body["outcome"] == "retried" and body["document"]["id"] == first["id"]
    assert body["document"]["status"] == "processing"
    assert "error" not in body["document"] or body["document"]["error"] is None
    assert (row.status, row.error) == ("processing", None)
    [call] = harness.temporal.calls
    assert call["workflow"] == "ParseDocumentWorkflow"
    assert call["id"] == f"doc-parse-{first['id']}"
    assert list(harness.store.objects) == [  # the new object is deleted
        f"projects/{pid}/documents/{first['id']}/original.pdf"
    ]
    assert len(harness.repo.documents) == 1


async def test_a_duplicate_of_a_processing_document_starts_no_second_workflow(
    harness,
):
    pid = await new_project(harness)
    first = (await upload(harness, pid, PDF)).json()["document"]
    assert harness.repo.documents[UUID(first["id"])].status == "processing"
    again = await upload(harness, pid, PDF, filename="again.pdf")
    assert again.status_code == 200
    body = again.json()
    assert body["outcome"] == "duplicate" and body["document"]["status"] == "processing"
    assert len(harness.temporal.calls) == 1 and harness.temporal.attempts == 1


async def test_a_retry_whose_parse_start_fails_marks_the_document_failed(harness):
    pid = await new_project(harness)
    first = (await upload(harness, pid, PDF)).json()["document"]
    row = harness.repo.documents[UUID(first["id"])]
    row.status, row.error = "failed", "pdf_parse_failed"
    harness.temporal.fail = RuntimeError("temporal down")
    again = await upload(harness, pid, PDF, filename="again.pdf")
    assert again.status_code == 200
    body = again.json()
    assert body["outcome"] == "retried"
    assert (body["document"]["status"], body["document"]["error"]) == (
        "failed",
        "workflow_start_failed",
    )
    assert (row.status, row.error) == ("failed", "workflow_start_failed")
    assert len(harness.store.objects) == 1


async def test_a_failed_parse_start_keeps_the_upload_and_marks_it_failed(harness):
    pid = await new_project(harness)
    harness.temporal.fail = RuntimeError("temporal down")
    response = await upload(harness, pid, PDF)
    assert response.status_code == 201
    document = response.json()["document"]
    assert document["status"] == "failed"
    assert document["error"] == "workflow_start_failed"
    key = f"projects/{pid}/documents/{document['id']}/original.pdf"
    assert key in harness.store.objects  # the file stays; "parse" can be retried
    row = harness.repo.documents[UUID(document["id"])]
    assert (row.status, row.error) == ("failed", "workflow_start_failed")
    harness.temporal.fail = None
    retry = await harness.client.post(
        f"/api/v1/projects/{pid}/documents/{document['id']}/parse",
        headers=bearer(),
    )
    assert retry.status_code == 202


async def test_a_failing_mark_failed_is_logged_and_the_upload_stays_201(harness, monkeypatch, caplog):

    async def boom(*args, **kwargs):
        raise RuntimeError("db secret")

    pid = await new_project(harness)
    harness.temporal.fail = RuntimeError("temporal down")
    monkeypatch.setattr(repo, "mark_failed", boom)
    with caplog.at_level("DEBUG"):
        response = await upload(harness, pid, PDF)
    assert response.status_code == 201
    document = response.json()["document"]
    [record] = events(caplog, "api.document.mark_failed_failed")
    assert fields(record)["document_id"] == document["id"]
    assert "db secret" not in caplog.text
