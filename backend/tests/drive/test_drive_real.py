"""Real gateway over a mocked HTTP transport (no Google, no sockets).

The service objects are real google-api-python-client resources built from the
bundled discovery documents; only the HTTP object is replaced.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from typing import Any

import httplib2
import pytest
from google.auth.exceptions import DefaultCredentialsError, RefreshError, TransportError
from googleapiclient import http as ghttp
from googleapiclient.discovery import build

from drive import real as real_module
from drive.gateway import DriveError
from drive.real import GoogleDriveGateway
from drive.reasons import Reason
from logger.logger import ERROR, get_logger, log_event

IDENTITY = "drive-reader@example.com"
FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
PRIVATE_FILE_NAME = "Q4 board plan for jane.doe@example.com"  # a file name that must never leak


def fid(prefix: str, n: int) -> str:
    return f"{prefix}{n:09d}"


class FakeHttp:
    """httplib2-shaped transport: routes by URL, records calls, adds latency."""

    def __init__(self, routes=None, latency: float = 0.0) -> None:
        self.routes = routes or {}
        self.latency = latency
        self.calls: list[tuple[str, str]] = []
        self.threads: set[int] = set()

    def request(self, uri, method="GET", **_kwargs):
        self.calls.append((method, uri))
        self.threads.add(threading.get_ident())
        if self.latency:
            threading.Event().wait(self.latency)  # real latency; the retry-backoff patch must not hide it
        status, payload = self.answer(uri)
        content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        response = httplib2.Response({"status": str(status), "content-type": "application/json"})
        return response, content

    def answer(self, uri: str):
        for pattern, handler in self.routes.items():
            if re.search(pattern, uri):
                return handler(uri) if callable(handler) else handler
        raise AssertionError(f"unexpected call {uri}")


def gateway(http: FakeHttp) -> GoogleDriveGateway:
    def factory(api: str, version: str) -> Any:
        return build(api, version, http=http, cache_discovery=False)

    return GoogleDriveGateway(service_factory=factory)


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    monkeypatch.setattr(ghttp.time, "sleep", lambda _seconds: None)


def error_body(status: int, reason: str, message: str = PRIVATE_FILE_NAME) -> dict:
    return {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"reason": reason, "message": message}],
            "details": [{"reason": reason}],
        }
    }


# -- check_folder and error mapping -----------------------------------------
def test_check_folder_ok_and_shared_drive_flag():
    http = FakeHttp(
        {
            r"/files/folder00000": (
                200,
                {
                    "id": "folder00000",
                    "name": "Specs",
                    "mimeType": FOLDER,
                    "driveId": "d",
                },
            )
        }
    )
    folder = gateway(http).check_folder("folder00000")
    assert (folder.id, folder.name, folder.in_shared_drive) == (
        "folder00000",
        "Specs",
        True,
    )
    uri = http.calls[0][1]
    assert "supportsAllDrives=true" in uri and "fields=id%2Cname%2CmimeType%2CdriveId" in uri


def test_check_folder_not_a_folder_and_invalid_id():
    http = FakeHttp(
        {
            r"/files/file0000000": (
                200,
                {"id": "file0000000", "name": "n", "mimeType": "application/pdf"},
            )
        }
    )
    with pytest.raises(DriveError) as raised:
        gateway(http).check_folder("file0000000")
    assert raised.value.reason is Reason.NOT_A_FOLDER
    with pytest.raises(DriveError) as raised:
        gateway(FakeHttp()).check_folder("x' or '1'='1")
    assert raised.value.reason is Reason.INVALID_LINK


@pytest.mark.parametrize(
    ("status", "token", "reason"),
    [
        (404, "notFound", Reason.NO_ACCESS),
        (403, "forbidden", Reason.NO_ACCESS),
        (403, "SERVICE_DISABLED", Reason.SETUP_ERROR),
        (403, "ACCESS_TOKEN_SCOPE_INSUFFICIENT", Reason.SETUP_ERROR),
        (403, "rateLimitExceeded", Reason.DRIVE_UNAVAILABLE),
        (429, "rateLimitExceeded", Reason.DRIVE_UNAVAILABLE),
        (503, "backendError", Reason.DRIVE_UNAVAILABLE),
        (400, "badRequest", Reason.UNKNOWN_ERROR),
    ],
)
def test_http_errors_map_to_reasons(status, token, reason):
    http = FakeHttp({r"/files/": (status, error_body(status, token))})
    with pytest.raises(DriveError) as raised:
        gateway(http).check_folder("folder00000")
    assert raised.value.reason is reason
    assert raised.value.http_status == status
    assert str(raised.value) == reason.value


def test_retries_then_succeeds_on_5xx():
    state = {"n": 0}

    def flaky(uri):
        state["n"] += 1
        if state["n"] < 3:
            return 503, error_body(503, "backendError")
        return 200, {"id": "folder00000", "name": "n", "mimeType": FOLDER}

    http = FakeHttp({r"/files/": flaky})
    assert gateway(http).check_folder("folder00000").name == "n"
    assert len(http.calls) == 3


def test_gives_up_after_five_retries_as_drive_unavailable():
    http = FakeHttp({r"/files/": (503, error_body(503, "backendError"))})
    with pytest.raises(DriveError) as raised:
        gateway(http).check_folder("folder00000")
    assert raised.value.reason is Reason.DRIVE_UNAVAILABLE
    assert len(http.calls) == 6  # 1 + num_retries=5


def test_transport_failure_is_drive_unavailable():
    class Broken(FakeHttp):
        def request(self, *_args, **_kwargs):
            raise httplib2.ServerNotFoundError("x")

    with pytest.raises(DriveError) as raised:
        gateway(Broken()).check_folder("folder00000")
    assert raised.value.reason is Reason.DRIVE_UNAVAILABLE


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (RefreshError("secret token abc"), Reason.SETUP_ERROR),
        (DefaultCredentialsError("x"), Reason.SETUP_ERROR),
        (TransportError("x"), Reason.DRIVE_UNAVAILABLE),
        (RuntimeError("file name x"), Reason.UNKNOWN_ERROR),
    ],
)
def test_other_exceptions(exc, reason):
    class Broken(FakeHttp):
        def request(self, *_args, **_kwargs):
            raise exc

    with pytest.raises(DriveError) as raised:
        gateway(Broken()).check_folder("folder00000")
    assert raised.value.reason is reason


# -- identity ----------------------------------------------------------------
def test_identity_after_adc_succeeds(monkeypatch):
    seen = {}

    def default(scopes=None):
        seen["scopes"] = scopes
        return object(), "project"

    monkeypatch.setattr(real_module.google.auth, "default", default)
    assert GoogleDriveGateway(identity_email=IDENTITY).identity() == IDENTITY
    assert seen["scopes"] == ["https://www.googleapis.com/auth/drive.readonly"]


def test_identity_without_adc_is_setup_error(monkeypatch):
    def default(scopes=None):
        raise DefaultCredentialsError("no adc")

    monkeypatch.setattr(real_module.google.auth, "default", default)
    with pytest.raises(DriveError) as raised:
        GoogleDriveGateway(identity_email=IDENTITY).identity()
    assert raised.value.reason is Reason.SETUP_ERROR


@pytest.mark.parametrize("value", [None, "", "   "])
def test_identity_without_a_configured_email_is_setup_error_and_skips_adc(value, monkeypatch):
    def default(scopes=None):
        raise AssertionError("ADC must not be read without an identity email")

    monkeypatch.setattr(real_module.google.auth, "default", default)
    with pytest.raises(DriveError) as raised:
        GoogleDriveGateway(identity_email=value).identity()
    assert raised.value.reason is Reason.SETUP_ERROR


def test_identity_returns_the_configured_email_stripped(monkeypatch):
    monkeypatch.setattr(real_module.google.auth, "default", lambda *_args, **_kwargs: (object(), "project"))
    assert GoogleDriveGateway(identity_email=f"  {IDENTITY}\n").identity() == IDENTITY


# -- listing -----------------------------------------------------------------
def tree_routes(tree: dict[str, list[dict]], root: str):
    """files.get for the root and files.list answering by parent ids in ``q``."""

    def lister(uri: str):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(uri).query)
        q = query["q"][0]
        assert q.endswith(") and trashed = false"), q
        parents = re.findall(r"'([^']+)' in parents", q)
        assert 1 <= len(parents) <= 20
        everything = [c for p in parents for c in tree.get(p, [])]
        size = int(query["pageSize"][0])
        start = int(query.get("pageToken", ["0"])[0])
        page = everything[start : start + size]
        body: dict[str, Any] = {"files": page}
        if start + size < len(everything):
            body["nextPageToken"] = str(start + size)
        return 200, body

    return {
        rf"/files/{root}\?": (200, {"id": root, "name": "Root", "mimeType": FOLDER}),
        r"/files\?": lister,
    }


def file_(parent: str, n: int, mime: str = "application/pdf") -> dict:
    return {
        "id": fid("file", n),
        "name": f"f{n}",
        "mimeType": mime,
        "parents": [parent],
        "size": "10",
        "version": "3",
        "modifiedTime": "2026-01-01T00:00:00.000Z",
    }


def folder_(parent: str, n: int) -> dict:
    return {
        "id": fid("fold", n),
        "name": f"d{n}",
        "mimeType": FOLDER,
        "parents": [parent],
    }


def test_listing_walks_all_levels_once_and_never_follows_shortcuts():
    root = "root000000"
    tree = {
        root: [
            folder_(root, 1),
            file_(root, 1, DOC),
            {
                "id": "short00000",
                "name": "s",
                "mimeType": "application/vnd.google-apps.shortcut",
                "parents": [root],
            },
        ],
        fid("fold", 1): [
            folder_(fid("fold", 1), 2),
            file_(fid("fold", 1), 2),
            {
                "id": root,
                "name": "loop",
                "mimeType": FOLDER,
                "parents": [fid("fold", 1)],
            },
        ],
        fid("fold", 2): [file_(fid("fold", 2), 3)],
    }
    http = FakeHttp(tree_routes(tree, root))
    listing = gateway(http).list_all(root)
    assert listing.folder_count == 3
    assert listing.revisited == 1
    assert [i.id for i in listing.items] == [
        fid("file", 1),
        "short00000",
        fid("file", 2),
        fid("file", 3),
    ]
    assert listing.items[0].size == 10 and listing.items[0].version == 3
    assert listing.items[2].parent_id == fid("fold", 1)
    uri = http.calls[1][1]
    assert "includeItemsFromAllDrives=true" in uri and "supportsAllDrives=true" in uri
    assert "pageSize=1000" in uri and "parents" in uri


def test_listing_pages():
    root = "root000000"
    tree = {root: [file_(root, n) for n in range(2300)]}
    http = FakeHttp(tree_routes(tree, root))
    assert len(gateway(http).list_all(root).items) == 2300
    assert len(http.calls) == 1 + 3


def test_listing_a_file_is_not_a_folder():
    http = FakeHttp(
        {
            r"/files/": (
                200,
                {"id": "file0000000", "name": "n", "mimeType": "application/pdf"},
            )
        }
    )
    with pytest.raises(DriveError) as raised:
        gateway(http).list_all("file0000000")
    assert raised.value.reason is Reason.NOT_A_FOLDER


def test_deadline_gives_too_slow():
    root = "root000000"
    http = FakeHttp(tree_routes({root: [folder_(root, 1)]}, root))
    with pytest.raises(DriveError) as raised:
        gateway(http).list_all(root, deadline=time.monotonic() - 1)
    assert raised.value.reason is Reason.TOO_SLOW


def test_nfr_perf_0001_thousand_files_hundred_folders_with_300ms_calls():
    """100 folders / 1,000 files in 3 levels, 300 ms per call: <= 20 s and few calls."""
    root = "root000000"
    tree: dict[str, list[dict]] = {root: []}
    level1 = [folder_(root, n) for n in range(1, 10)]
    tree[root] = list(level1)
    n_folder = 10
    level2 = []
    for parent in level1:
        tree[parent["id"]] = []
        for _ in range(10):
            f = folder_(parent["id"], n_folder)
            n_folder += 1
            tree[parent["id"]].append(f)
            level2.append(f)
    assert n_folder - 1 + 1 == 100  # 99 subfolders + root
    for f in level2:
        tree[f["id"]] = []
    holders = [root] + [p["id"] for p in level1] + [f["id"] for f in level2]
    for n in range(1000):
        holder = holders[n % len(holders)]
        tree[holder].append(file_(holder, n))
    http = FakeHttp(tree_routes(tree, root), latency=0.3)
    started = time.monotonic()
    listing = gateway(http).list_all(root)
    elapsed = time.monotonic() - started
    assert (listing.folder_count, len(listing.items)) == (100, 1000)
    assert elapsed <= 20
    # root get + root + 9 + ceil(90 / 20) = 5 list calls = 8 calls (paging adds none: < 1000 rows each)
    assert len(http.calls) == 8
    assert len(http.threads) == 1


# -- download ----------------------------------------------------------------
def test_download_streams_chunks():
    payload = b"x" * 25

    def media(uri):
        return 200, payload

    class Ranged(FakeHttp):
        def request(self, uri, method="GET", **kwargs):
            self.calls.append((method, uri))
            start, end = re.search(r"bytes=(\d+)-(\d+)", kwargs["headers"]["range"]).groups()
            chunk = payload[int(start) : int(end) + 1]
            response = httplib2.Response(
                {
                    "status": "206",
                    "content-range": f"bytes {start}-{int(start) + len(chunk) - 1}/{len(payload)}",
                }
            )
            return response, chunk

    http = Ranged({r"/files/file0000000\?.*alt=media": media})
    chunks = list(gateway(http).iter_download("file0000000", chunk_size=10))
    assert b"".join(chunks) == payload
    assert [len(c) for c in chunks] == [10, 10, 5]
    assert "supportsAllDrives=true" in http.calls[0][1]


def test_download_error_maps_without_text():
    http = FakeHttp({r"/files/": (404, error_body(404, "notFound"))})
    with pytest.raises(DriveError) as raised:
        list(gateway(http).iter_download("file0000000"))
    assert raised.value.reason is Reason.NO_ACCESS


# -- Google files ------------------------------------------------------------
def test_docs_include_tabs_and_image_links_are_dropped():
    document = {
        "title": "T",
        "tabs": [{"documentTab": {"body": {"content": []}}}],
        "inlineObjects": {
            "o": {
                "inlineObjectProperties": {
                    "embeddedObject": {
                        "imageProperties": {
                            "contentUri": "https://lh3.googleusercontent.com/secret",
                            "sourceUri": "https://x",
                        }
                    }
                }
            }
        },
    }
    http = FakeHttp({r"docs.*documents/doc0000000": (200, document)})
    raw = gateway(http).read_google("doc0000000", DOC)
    assert "includeTabsContent=true" in http.calls[0][1]
    assert "contentUri" not in json.dumps(raw) and "googleusercontent" not in json.dumps(raw)
    assert raw["tabs"][0]["documentTab"]["body"] == {"content": []}


def test_slides_read():
    http = FakeHttp({r"slides.*presentations/pres000000": (200, {"title": "P", "slides": []})})
    mime = "application/vnd.google-apps.presentation"
    assert gateway(http).read_google("pres000000", mime)["title"] == "P"


def test_sheets_quote_titles_and_use_formatted_values():
    meta = {
        "properties": {"title": "B"},
        "sheets": [
            {"properties": {"sheetId": 1, "title": "It's Q4", "index": 0}},
            {"properties": {"sheetId": 2, "title": "Two", "index": 1}},
        ],
    }
    ranges = {
        "valueRanges": [
            {"range": "'It''s Q4'!A1:B2", "values": [["a"]]},
            {"range": "Two!A1", "values": []},
        ]
    }
    http = FakeHttp({r"batchGet": (200, ranges), r"sheets.*spreadsheets/sheet00000": (200, meta)})
    result = gateway(http).read_google("sheet00000", "application/vnd.google-apps.spreadsheet")
    assert result == {"spreadsheet": meta, "valueRanges": ranges["valueRanges"]}
    batch = next(u for _, u in http.calls if "batchGet" in u)
    decoded = urllib.parse.parse_qs(urllib.parse.urlsplit(batch).query)
    assert decoded["ranges"] == ["'It''s Q4'", "'Two'"]
    assert decoded["valueRenderOption"] == ["FORMATTED_VALUE"]
    first = next(u for _, u in http.calls if "batchGet" not in u)
    assert "properties.title" in urllib.parse.unquote(first)


def test_sheet_without_tabs_makes_no_batch_call():
    http = FakeHttp({r"sheets.*spreadsheets/sheet00000": (200, {"properties": {"title": "B"}})})
    result = gateway(http).read_google("sheet00000", "application/vnd.google-apps.spreadsheet")
    assert result["valueRanges"] == []
    assert len(http.calls) == 1


def test_read_google_errors_and_unsupported_mime():
    http = FakeHttp({r"docs": (403, error_body(403, "forbidden"))})
    with pytest.raises(DriveError) as raised:
        gateway(http).read_google("doc0000000", DOC)
    assert raised.value.reason is Reason.NO_ACCESS
    with pytest.raises(DriveError):
        gateway(http).read_google("doc0000000", "application/pdf")
    with pytest.raises(DriveError):
        gateway(http).read_google("bad id", DOC)


def test_service_is_built_per_call_and_credentials_are_shared(monkeypatch):
    built: list[Any] = []

    def fake_build(api, version, credentials=None, cache_discovery=None):
        built.append(credentials)
        return build(
            api,
            version,
            http=FakeHttp(
                {
                    r"/files/": (
                        200,
                        {"id": "folder00000", "name": "n", "mimeType": FOLDER},
                    )
                }
            ),
            cache_discovery=False,
        )

    monkeypatch.setattr(real_module, "build", fake_build)
    calls = []
    monkeypatch.setattr(
        real_module.google.auth,
        "default",
        lambda *_args, **_kwargs: (calls.append(1) or object(), "p"),
    )
    gw = GoogleDriveGateway()
    gw.check_folder("folder00000")
    gw.check_folder("folder00000")
    assert len(built) == 2 and built[0] is built[1]
    assert calls == [1]


# -- log scrub ---------------------------------------------------------------
def test_http_error_content_never_reaches_a_log_record(caplog):
    """INT-GDUI-0002 / NFR-MAIN-0001.AC4: file names and emails stay out of logs."""
    caplog.set_level(logging.DEBUG)
    http = FakeHttp({r"/files/": (403, error_body(403, "forbidden", PRIVATE_FILE_NAME))})
    logger = get_logger("drive.test")
    with pytest.raises(DriveError) as raised:
        gateway(http).check_folder("folder00000")
    error = raised.value
    assert PRIVATE_FILE_NAME not in repr(error) and "@" not in repr(error)
    assert error.__cause__ is None and error.__suppress_context__
    log_event(
        logger,
        ERROR,
        "drive.test.failed",
        reason=error.reason.value,
        http_status=error.http_status,
        google_reason=error.google_reason,
        exc_info=error,
    )
    formatter = logging.Formatter()
    assert caplog.records
    for record in caplog.records:
        text = formatter.format(record) + repr(record.__dict__)
        assert "jane.doe" not in text and "board plan" not in text


def sheet_http(titles: list[tuple[str, str]]):
    meta = {
        "properties": {"title": "B"},
        "sheets": [
            {"properties": {"sheetId": n, "title": t, "index": n, "sheetType": kind}}
            for n, (t, kind) in enumerate(titles)
        ],
    }

    def batch(uri):
        asked = urllib.parse.parse_qs(urllib.parse.urlsplit(uri).query)["ranges"]
        assert len(asked) <= 20
        return 200, {"valueRanges": [{"range": r, "values": [[r]]} for r in asked]}

    return meta, FakeHttp({r"batchGet": batch, r"sheets.*spreadsheets/sheet00000": (200, meta)})


SHEET = "application/vnd.google-apps.spreadsheet"


def test_chart_tab_keeps_valueranges_aligned():
    _meta, http = sheet_http([("A", "GRID"), ("Chart", "OBJECT"), ("C", "GRID")])
    result = gateway(http).read_google("sheet00000", SHEET)
    assert [r.get("range") for r in result["valueRanges"]] == ["'A'", None, "'C'"]
    assert result["valueRanges"][1] == {}
    first = next(u for _, u in http.calls if "batchGet" not in u)
    assert "sheetType" in urllib.parse.unquote(first)


def test_many_tabs_are_batched_by_twenty():
    _meta, http = sheet_http([(f"T{n}", "GRID") for n in range(45)])
    result = gateway(http).read_google("sheet00000", SHEET)
    assert [r["range"] for r in result["valueRanges"]] == [f"'T{n}'" for n in range(45)]
    assert sum("batchGet" in u for _, u in http.calls) == 3


def test_download_unexpected_exception_is_unknown_error(monkeypatch):
    class Boom(FakeHttp):
        def request(self, *_args, **_kwargs):
            raise ValueError("file name jane@example.com")

    monkeypatch.setattr(
        real_module.MediaIoBaseDownload,
        "next_chunk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("secret")),
    )
    with pytest.raises(DriveError) as raised:
        list(gateway(Boom()).iter_download("file0000000"))
    assert raised.value.reason is Reason.UNKNOWN_ERROR
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "folder00000"},
        {"mimeType": FOLDER},
        {"mimeType": FOLDER, "id": "folder00000"},
        [],
    ],
)
def test_malformed_folder_payload_is_unknown_error(payload):
    http = FakeHttp({r"/files/": (200, payload)})
    with pytest.raises(DriveError) as raised:
        gateway(http).check_folder("folder00000")
    assert raised.value.reason is Reason.UNKNOWN_ERROR


@pytest.mark.parametrize("child", [{"id": "x"}, {"mimeType": "a"}, "str"])
def test_malformed_child_is_unknown_error(child):
    root = "root000000"
    http = FakeHttp(tree_routes({root: [child]}, root))
    with pytest.raises(DriveError) as raised:
        gateway(http).list_all(root)
    assert raised.value.reason is Reason.UNKNOWN_ERROR


def test_401_is_setup_error():
    http = FakeHttp({r"/files/": (401, error_body(401, "authError"))})
    with pytest.raises(DriveError) as raised:
        gateway(http).check_folder("folder00000")
    assert raised.value.reason is Reason.SETUP_ERROR
