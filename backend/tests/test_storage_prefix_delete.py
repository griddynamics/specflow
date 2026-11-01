"""Document keys by media type and ``ObjectStore.delete_by_prefix`` on every backend.

A prefix delete ends with ``/`` so that ``documents/{did}/`` never matches another document id.
"""

from __future__ import annotations

import boto3
import pytest
from botocore.stub import Stubber
from google.api_core import exceptions as gexc

from file_parsers.formats import FORMATS
from settings import StorageSettings
from storage.base import InvalidObjectKeyError, ObjectStore
from storage.gcs import GcsObjectStore
from storage.keys import document_original, document_prefix, validate_prefix
from storage.memory import InMemoryObjectStore
from storage.s3 import S3ObjectStore

PID, DID = (
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222",
)
OTHER = DID[:-1] + "3"
PREFIX = f"projects/{PID}/documents/{DID}/"


# ---- keys -------------------------------------------------------------------------------


def test_the_pdf_key_is_unchanged_and_is_the_default():
    assert document_original(PID, DID) == PREFIX + "original.pdf"
    assert document_original(PID, DID, "application/pdf") == PREFIX + "original.pdf"


@pytest.mark.parametrize("spec", FORMATS, ids=lambda spec: spec.extension)
def test_every_accepted_media_type_has_a_key_with_its_extension(spec):
    assert document_original(PID, DID, spec.media_type) == f"{PREFIX}original{spec.extension}"


def test_a_media_type_with_parameters_or_upper_case_maps_like_the_trusted_type():
    assert document_original(PID, DID, "Text/HTML; charset=utf-8") == PREFIX + "original.html"


@pytest.mark.parametrize(
    "media_type",
    ["", "application/msword", "../../etc/passwd", "text/plain", "x/y; .exe"],
)
def test_an_unknown_media_type_never_becomes_part_of_a_key(media_type):
    with pytest.raises(InvalidObjectKeyError):
        document_original(PID, DID, media_type)


def test_the_document_prefix_ends_with_a_slash_and_holds_every_original():
    assert document_prefix(PID, DID) == PREFIX
    assert document_original(PID, DID, "text/csv").startswith(PREFIX)
    assert not f"projects/{PID}/documents/{OTHER}/original.pdf".startswith(PREFIX)


@pytest.mark.parametrize("ids", [("a/b", DID), (PID, "../x"), ("", DID), (PID, "")])
def test_the_document_prefix_refuses_a_path_in_an_id(ids):
    with pytest.raises(InvalidObjectKeyError):
        document_prefix(*ids)


@pytest.mark.parametrize("prefix", ["", "/", "a", "a//", "../", "a/../", "/a/", "a\\b/", "a/\x00/"])
def test_validate_prefix_refuses_what_is_not_a_safe_prefix_with_a_trailing_slash(
    prefix,
):
    with pytest.raises(InvalidObjectKeyError):
        validate_prefix(prefix)


@pytest.mark.parametrize(
    "prefix",
    [
        "projects/",
        f"projects/{PID}/",
        f"projects/{PID}/documents/",
        f"projects/{PID}/documents/{DID}/original.pdf/",
        f"projects/{PID}/other/{DID}/",
        f"files/{PID}/documents/{DID}/",
        "a/b/c/d/",
    ],
)
def test_validate_prefix_refuses_anything_but_a_document_prefix(prefix):
    with pytest.raises(InvalidObjectKeyError):
        validate_prefix(prefix)


def test_validate_prefix_returns_a_safe_prefix_unchanged():
    assert validate_prefix(PREFIX) == PREFIX


# ---- in memory --------------------------------------------------------------------------


def seeded_memory() -> InMemoryObjectStore:
    store = InMemoryObjectStore(app_env="local")
    for name in ("original.pdf", "original.docx", "original.json", "original.bin"):
        store.objects[PREFIX + name] = b"x"
        store.content_types[PREFIX + name] = "x/y"
    store.objects[f"projects/{PID}/documents/{OTHER}/original.pdf"] = b"keep"
    store.objects[f"projects/{PID}/documents/{DID}x/original.pdf"] = b"keep"
    return store


async def test_memory_deletes_every_object_of_the_document_and_nothing_else():
    store = seeded_memory()
    assert isinstance(store, ObjectStore)
    assert await store.delete_by_prefix(PREFIX) == 4
    assert sorted(store.objects) == sorted(
        [
            f"projects/{PID}/documents/{OTHER}/original.pdf",
            f"projects/{PID}/documents/{DID}x/original.pdf",
        ]
    )
    assert not any(key.startswith(PREFIX) for key in store.content_types)


async def test_memory_prefix_delete_is_idempotent():
    store = seeded_memory()
    await store.delete_by_prefix(PREFIX)
    assert await store.delete_by_prefix(PREFIX) == 0


@pytest.mark.parametrize("prefix", [f"projects/{PID}/documents/{DID}", "", "../"])
async def test_memory_refuses_a_prefix_without_a_trailing_slash(prefix):
    store = seeded_memory()
    with pytest.raises(InvalidObjectKeyError):
        await store.delete_by_prefix(prefix)
    assert len(store.objects) == 6


# ---- GCS --------------------------------------------------------------------------------

GCS_PREFIX = "dev/local/x/"


class Blob:
    def __init__(self, name, *, missing=False):
        self.name, self.deleted, self.missing = name, False, missing

    def delete(self, timeout=None):
        assert timeout, "a delete must carry a timeout"
        if self.missing:
            raise gexc.NotFound("gone")
        self.deleted = True


class Client:
    def __init__(self, names, missing=()):
        self.blobs = [Blob(n, missing=n in missing) for n in names]
        self.prefixes = []

    def bucket(self, name):
        return name

    def list_blobs(self, _bucket, prefix, timeout=None):
        assert timeout, "a listing must carry a timeout"
        self.prefixes.append(prefix)
        return iter([b for b in self.blobs if b.name.startswith(prefix) and not b.deleted])


def gcs(names, missing=()):
    client = Client(names, missing)
    return GcsObjectStore(StorageSettings(STORAGE_BUCKET="b", STORAGE_PREFIX=GCS_PREFIX), client=client), client


async def test_gcs_deletes_under_the_store_prefix_plus_the_document_prefix():
    names = [GCS_PREFIX + PREFIX + n for n in ("original.pdf", "original.pptx")]
    names += [
        GCS_PREFIX + f"projects/{PID}/documents/{OTHER}/original.pdf",
        f"other/{PREFIX}original.pdf",
    ]
    adapter, client = gcs(names)
    assert await adapter.delete_by_prefix(PREFIX) == 2
    assert client.prefixes == [GCS_PREFIX + PREFIX]
    assert [b.deleted for b in client.blobs] == [True, True, False, False]


async def test_gcs_skips_an_object_deleted_by_a_racing_call():
    names = [GCS_PREFIX + PREFIX + n for n in ("original.pdf", "original.json")]
    adapter, client = gcs(names, missing={names[0]})
    assert await adapter.delete_by_prefix(PREFIX) == 1
    assert client.blobs[1].deleted


async def test_gcs_refuses_a_prefix_without_a_trailing_slash():
    adapter, client = gcs([])
    with pytest.raises(InvalidObjectKeyError):
        await adapter.delete_by_prefix(PREFIX.rstrip("/"))
    assert client.prefixes == []


# ---- S3 ---------------------------------------------------------------------------------

BUCKET, S3_PREFIX = "b", "p/"


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    client = boto3.client("s3", region_name="us-east-1")
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def s3(client) -> S3ObjectStore:
    return S3ObjectStore(
        StorageSettings(STORAGE_BACKEND="s3", STORAGE_BUCKET=BUCKET, STORAGE_PREFIX=S3_PREFIX),
        client=client,
    )


async def test_s3_lists_and_deletes_under_the_store_prefix_plus_the_document_prefix(
    stub,
):
    client, stubber = stub
    full = S3_PREFIX + PREFIX
    keys = [full + "original.docx", full + "original.json"]
    stubber.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": k} for k in keys]},
        {"Bucket": BUCKET, "Prefix": full},
    )
    stubber.add_response(
        "delete_objects",
        {},
        {
            "Bucket": BUCKET,
            "Delete": {"Objects": [{"Key": k} for k in keys], "Quiet": True},
        },
    )
    assert await s3(client).delete_by_prefix(PREFIX) == 2


async def test_s3_with_nothing_under_the_prefix_makes_no_delete_call(stub):
    client, stubber = stub
    stubber.add_response("list_objects_v2", {}, {"Bucket": BUCKET, "Prefix": S3_PREFIX + PREFIX})
    assert await s3(client).delete_by_prefix(PREFIX) == 0


async def test_s3_refuses_a_prefix_without_a_trailing_slash(stub):
    client, _ = stub
    with pytest.raises(InvalidObjectKeyError):
        await s3(client).delete_by_prefix("projects")
