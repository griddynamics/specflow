"""S3ObjectStore offline: botocore Stubber, parametrised over the part size (SPL-STO-04)."""

from __future__ import annotations

import inspect
from io import BytesIO
from pathlib import Path

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

import storage.s3 as s3_module
from settings import StorageSettings
from storage.base import (
    InvalidObjectKeyError,
    MiB,
    ObjectAdmin,
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    StorageUnavailableError,
)
from storage.s3 import S3_PART_SIZE, S3ObjectStore

PART_SIZES = [S3_PART_SIZE, 8 * MiB]
BUCKET, PREFIX, KEY = "b", "p/", "k/obj.bin"
FULL = PREFIX + KEY


def settings(**extra: str) -> StorageSettings:
    return StorageSettings(STORAGE_BACKEND="s3", STORAGE_BUCKET=BUCKET, STORAGE_PREFIX=PREFIX, **extra)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    client = boto3.client("s3", region_name="us-east-1")
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def make_store(client, part_size=S3_PART_SIZE, clock=None) -> S3ObjectStore:
    return S3ObjectStore(settings(), client=client, part_size=part_size, **({"clock": clock} if clock else {}))


def put_params(**extra):
    return {"Bucket": BUCKET, "Key": FULL, "Body": ANY, "ContentType": "text/plain", "IfNoneMatch": "*", **extra}


def create_upload(stubber):
    stubber.add_response(
        "create_multipart_upload",
        {"UploadId": "u1"},
        {"Bucket": BUCKET, "Key": FULL, "ContentType": "text/plain"},
    )


def upload_part(stubber, number):
    stubber.add_response(
        "upload_part",
        {"ETag": f"e{number}"},
        {"Bucket": BUCKET, "Key": FULL, "UploadId": "u1", "PartNumber": number, "Body": ANY},
    )


def complete_params():
    return {
        "Bucket": BUCKET,
        "Key": FULL,
        "UploadId": "u1",
        "MultipartUpload": {"Parts": [{"ETag": "e1", "PartNumber": 1}, {"ETag": "e2", "PartNumber": 2}]},
        "IfNoneMatch": "*",
    }


def abort_params():
    return {"Bucket": BUCKET, "Key": FULL, "UploadId": "u1"}


async def open_writer(store):
    return await store.open_writer(KEY, content_type="text/plain")


@pytest.mark.parametrize("part", PART_SIZES)
async def test_empty_and_exact_part_use_one_put(stub, part):
    client, stubber = stub
    for size in (0, part):
        stubber.add_response("put_object", {}, put_params())
        writer = await open_writer(make_store(client, part))
        await writer.write(b"x" * size)
        stored = await writer.close()
        assert (stored.key, stored.size) == (KEY, size)


@pytest.mark.parametrize("part", PART_SIZES)
async def test_one_byte_over_a_part_is_a_multipart_upload(stub, part):
    client, stubber = stub
    create_upload(stubber)
    upload_part(stubber, 1)
    upload_part(stubber, 2)
    stubber.add_response("complete_multipart_upload", {}, complete_params())
    writer = await open_writer(make_store(client, part))
    await writer.write(b"x" * (part + 1))
    stored = await writer.close()
    assert stored.size == part + 1


async def test_chunks_of_one_mib_fill_parts_in_order(stub):
    client, stubber = stub
    sent = []
    create_upload(stubber)
    for number in (1, 2):
        upload_part(stubber, number)
    stubber.add_response("complete_multipart_upload", {}, complete_params())
    client.meta.events.register(
        "before-parameter-build.s3.UploadPart", lambda params, **_: sent.append(len(params["Body"]))
    )
    writer = await open_writer(make_store(client))
    for _ in range(6):
        await writer.write(b"y" * MiB)
    await writer.close()
    assert sent == [S3_PART_SIZE, MiB]


async def test_one_chunk_of_several_parts_is_split_in_order(stub):
    client, stubber = stub
    sent = []
    create_upload(stubber)
    for number in (1, 2, 3):
        upload_part(stubber, number)
    params = complete_params()
    params["MultipartUpload"]["Parts"].append({"ETag": "e3", "PartNumber": 3})
    stubber.add_response("complete_multipart_upload", {}, params)
    client.meta.events.register(
        "before-parameter-build.s3.UploadPart", lambda params, **_: sent.append(bytes(params["Body"][:1]))
    )
    writer = await open_writer(make_store(client))
    await writer.write(b"a" * S3_PART_SIZE + b"b" * S3_PART_SIZE + b"c")
    stored = await writer.close()
    assert (sent, stored.size) == ([b"a", b"b", b"c"], 2 * S3_PART_SIZE + 1)


async def test_put_412_is_object_already_exists(stub):
    client, stubber = stub
    stubber.add_client_error("put_object", "PreconditionFailed", http_status_code=412, expected_params=put_params())
    writer = await open_writer(make_store(client))
    with pytest.raises(ObjectAlreadyExistsError):
        await writer.close()


async def test_complete_412_aborts_and_raises(stub):
    client, stubber = stub
    create_upload(stubber)
    upload_part(stubber, 1)
    upload_part(stubber, 2)
    stubber.add_client_error(
        "complete_multipart_upload", "PreconditionFailed", http_status_code=412, expected_params=complete_params()
    )
    stubber.add_response("abort_multipart_upload", {}, abort_params())
    writer = await open_writer(make_store(client))
    await writer.write(b"x" * (S3_PART_SIZE + 1))
    with pytest.raises(ObjectAlreadyExistsError):
        await writer.close()
    await writer.abort()  # already aborted: no second request (the stub queue is empty)


async def test_complete_409_then_200_succeeds(stub):
    client, stubber = stub
    create_upload(stubber)
    upload_part(stubber, 1)
    upload_part(stubber, 2)
    stubber.add_client_error(
        "complete_multipart_upload",
        "ConditionalRequestConflict",
        http_status_code=409,
        expected_params=complete_params(),
    )
    stubber.add_response("complete_multipart_upload", {}, complete_params())
    writer = await open_writer(make_store(client))
    await writer.write(b"x" * (S3_PART_SIZE + 1))
    assert (await writer.close()).size == S3_PART_SIZE + 1


async def test_complete_409_twice_is_treated_as_412(stub):
    client, stubber = stub
    create_upload(stubber)
    upload_part(stubber, 1)
    upload_part(stubber, 2)
    for _ in range(2):
        stubber.add_client_error(
            "complete_multipart_upload",
            "ConditionalRequestConflict",
            http_status_code=409,
            expected_params=complete_params(),
        )
    stubber.add_response("abort_multipart_upload", {}, abort_params())
    writer = await open_writer(make_store(client))
    await writer.write(b"x" * (S3_PART_SIZE + 1))
    with pytest.raises(ObjectAlreadyExistsError):
        await writer.close()


async def test_abort_twice_sends_one_request(stub):
    client, stubber = stub
    create_upload(stubber)
    upload_part(stubber, 1)
    stubber.add_response("abort_multipart_upload", {}, abort_params())
    writer = await open_writer(make_store(client))
    await writer.write(b"x" * (S3_PART_SIZE + 1))
    await writer.abort()
    await writer.abort()
    with pytest.raises(ObjectStoreError):
        await writer.write(b"z")


async def test_abort_no_such_upload_is_success(stub):
    client, stubber = stub
    create_upload(stubber)
    upload_part(stubber, 1)
    stubber.add_client_error(
        "abort_multipart_upload", "NoSuchUpload", http_status_code=404, expected_params=abort_params()
    )
    writer = await open_writer(make_store(client))
    await writer.write(b"x" * (S3_PART_SIZE + 1))
    await writer.abort()


async def test_abort_before_any_upload_sends_nothing(stub):
    client, _ = stub
    writer = await open_writer(make_store(client))
    await writer.write(b"small")
    await writer.abort()


async def test_a_failed_part_upload_surfaces_and_abort_cleans_up(stub):
    client, stubber = stub
    create_upload(stubber)
    stubber.add_client_error("upload_part", "InternalError", http_status_code=500)
    stubber.add_response("abort_multipart_upload", {}, abort_params())
    writer = await open_writer(make_store(client))
    with pytest.raises(Exception, match="InternalError"):
        await writer.write(b"x" * (S3_PART_SIZE + 1))
    await writer.abort()


async def test_buffer_is_a_list_of_chunks_never_a_bytearray(stub):
    client, _ = stub
    writer = await open_writer(make_store(client))
    for _ in range(3):
        await writer.write(b"c" * 1024)
    assert type(writer._buffer) is list and all(type(c) is bytes for c in writer._buffer)
    source = inspect.getsource(s3_module)
    assert "bytearray" not in source.replace("`bytearray +=`", "")
    assert 5 * MiB == S3_PART_SIZE


async def test_head_404_is_false_and_200_is_true(stub):
    client, stubber = stub
    store = make_store(client)
    stubber.add_client_error(
        "head_object", "404", http_status_code=404, expected_params={"Bucket": BUCKET, "Key": FULL}
    )
    stubber.add_response("head_object", {}, {"Bucket": BUCKET, "Key": FULL})
    assert await store.exists(KEY) is False
    assert await store.exists(KEY) is True


async def test_download_streams_to_the_file(stub, tmp_path: Path):
    client, stubber = stub
    data = b"hello world"
    stubber.add_response(
        "get_object", {"Body": StreamingBody(BytesIO(data), len(data))}, {"Bucket": BUCKET, "Key": FULL}
    )
    target = tmp_path / "out"
    assert await make_store(client).download_to_path(KEY, target) == len(data)
    assert target.read_bytes() == data


async def test_download_404_is_not_found_and_leaves_no_file(stub, tmp_path: Path):
    client, stubber = stub
    stubber.add_client_error(
        "get_object", "NoSuchKey", http_status_code=404, expected_params={"Bucket": BUCKET, "Key": FULL}
    )
    target = tmp_path / "out"
    with pytest.raises(ObjectNotFoundError):
        await make_store(client).download_to_path(KEY, target)
    assert not target.exists()


async def test_delete_missing_is_success_and_other_errors_raise(stub):
    client, stubber = stub
    store = make_store(client)
    stubber.add_client_error(
        "delete_object", "NoSuchKey", http_status_code=404, expected_params={"Bucket": BUCKET, "Key": FULL}
    )
    await store.delete(KEY)
    stubber.add_response("delete_object", {}, {"Bucket": BUCKET, "Key": FULL})
    await store.delete(KEY)
    stubber.add_client_error("delete_object", "AccessDenied", http_status_code=403)
    with pytest.raises(Exception, match="AccessDenied"):
        await store.delete(KEY)


async def test_check_lists_once_and_caches_for_ten_seconds(stub):
    client, stubber = stub
    clock = Clock()
    store = make_store(client, clock=clock)
    stubber.add_response("list_objects_v2", {}, {"Bucket": BUCKET, "Prefix": PREFIX, "MaxKeys": 1})
    await store.check()
    clock.now = 9.0
    await store.check()
    clock.now = 11.0
    stubber.add_response("list_objects_v2", {}, {"Bucket": BUCKET, "Prefix": PREFIX, "MaxKeys": 1})
    await store.check()


async def test_check_failure_is_unavailable_and_cached(stub):
    client, stubber = stub
    clock = Clock()
    store = make_store(client, clock=clock)
    stubber.add_client_error("list_objects_v2", "AccessDenied", http_status_code=403)
    with pytest.raises(StorageUnavailableError):
        await store.check()
    clock.now = 1.0
    with pytest.raises(StorageUnavailableError):  # cached: no second request
        await store.check()


async def test_invalid_key_fails_before_any_request(stub):
    client, _ = stub
    store = make_store(client)
    with pytest.raises(InvalidObjectKeyError):
        await store.open_writer("../x", content_type="text/plain")
    with pytest.raises(InvalidObjectKeyError):
        await store.exists("/abs")


def test_client_uses_settings_endpoint_addressing_and_checksum_config(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    store = S3ObjectStore(
        settings(AWS_ENDPOINT_URL_S3="http://localhost:19000", AWS_REGION="eu-west-1", STORAGE_S3_ADDRESSING="path")
    )
    meta = store.client.meta
    assert meta.endpoint_url == "http://localhost:19000" and meta.region_name == "eu-west-1"
    config = meta.config
    assert config.s3["addressing_style"] == "path"
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"
    assert (config.connect_timeout, config.read_timeout) == (5, 60)


async def test_close_releases_only_an_owned_client(stub, monkeypatch):
    client, _ = stub
    await make_store(client).close()  # handed-in client stays open
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    owned = S3ObjectStore(settings(AWS_REGION="us-east-1"))
    closed = []
    monkeypatch.setattr(owned.client, "close", lambda: closed.append(1))
    await owned.close()
    await owned.close()
    assert closed == [1]


def test_store_and_admin_protocols(stub):
    store = make_store(stub[0])
    assert isinstance(store, ObjectStore) and isinstance(store, ObjectAdmin)


# ---- ObjectAdmin ----

ADMIN_PREFIX = "dev/local/projects/abc/"


async def test_count_prefix_follows_continuation_tokens(stub):
    client, stubber = stub
    stubber.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": "a"}, {"Key": "b"}], "IsTruncated": True, "NextContinuationToken": "t"},
        {"Bucket": BUCKET, "Prefix": ADMIN_PREFIX},
    )
    stubber.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": "c"}]},
        {"Bucket": BUCKET, "Prefix": ADMIN_PREFIX, "ContinuationToken": "t"},
    )
    assert await make_store(client).count_prefix(ADMIN_PREFIX) == 3


async def test_delete_prefix_deletes_listed_keys_in_one_batch(stub):
    client, stubber = stub
    stubber.add_response(
        "list_objects_v2", {"Contents": [{"Key": "a"}, {"Key": "b"}]}, {"Bucket": BUCKET, "Prefix": ADMIN_PREFIX}
    )
    stubber.add_response(
        "delete_objects",
        {},
        {"Bucket": BUCKET, "Delete": {"Objects": [{"Key": "a"}, {"Key": "b"}], "Quiet": True}},
    )
    assert await make_store(client).delete_prefix(ADMIN_PREFIX) == 2


async def test_delete_prefix_with_nothing_makes_no_delete_call(stub):
    client, stubber = stub
    stubber.add_response("list_objects_v2", {}, {"Bucket": BUCKET, "Prefix": ADMIN_PREFIX})
    assert await make_store(client).delete_prefix(ADMIN_PREFIX) == 0


async def test_delete_prefix_reports_per_key_errors(stub):
    client, stubber = stub
    stubber.add_response("list_objects_v2", {"Contents": [{"Key": "a"}]}, {"Bucket": BUCKET, "Prefix": ADMIN_PREFIX})
    stubber.add_response(
        "delete_objects",
        {"Errors": [{"Key": "a", "Code": "AccessDenied"}]},
        {"Bucket": BUCKET, "Delete": {"Objects": [{"Key": "a"}], "Quiet": True}},
    )
    with pytest.raises(ObjectStoreError):
        await make_store(client).delete_prefix(ADMIN_PREFIX)


async def test_missing_bucket_is_a_clear_unavailable_error(stub):
    client, stubber = stub
    stubber.add_client_error("put_object", "NoSuchBucket", http_status_code=404, expected_params=put_params())
    writer = await make_store(client).open_writer(KEY, content_type="text/plain")
    await writer.write(b"x")
    with pytest.raises(StorageUnavailableError, match="bucket 'b' does not exist"):
        await writer.close()
    stubber.add_client_error("head_object", "NoSuchBucket", http_status_code=404)
    with pytest.raises(StorageUnavailableError, match="does not exist"):
        await make_store(client).exists(KEY)


@pytest.mark.parametrize("call", ["count_prefix", "delete_prefix"])
async def test_missing_bucket_on_a_prefix_call_is_a_clear_unavailable_error(stub, call):
    client, stubber = stub
    stubber.add_client_error("list_objects_v2", "NoSuchBucket", http_status_code=404)
    with pytest.raises(StorageUnavailableError, match="bucket 'b' does not exist"):
        await getattr(make_store(client), call)(ADMIN_PREFIX)
