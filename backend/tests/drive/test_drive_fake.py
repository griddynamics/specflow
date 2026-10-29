"""NFR-PORT-0002 fake Drive; INT-GDUI-0001/0002 answers, DATA-GDUI-0003 recordings."""

from __future__ import annotations

import contextlib
import json
import re
import socket
import time
from importlib.resources import files

import pymupdf
import pytest

from drive import fake as fake_module
from drive.categories import CATEGORY_ORDER, categorize
from drive.fake import FakeDriveGateway
from drive.gateway import FAKE_DRIVE_EMAIL, DriveError
from drive.model import summarize
from drive.reasons import Reason
from settings import ConfigurationError

CONTRACT = json.loads(files("drive").joinpath("fixtures/contract.json").read_text("utf-8"))


@pytest.fixture
def gw(google_samples):
    return FakeDriveGateway(app_env="local", google_samples=google_samples, file_delay_s=0)


def test_identity_is_the_fake_email(gw):
    assert gw.identity() == FAKE_DRIVE_EMAIL == "fake-drive@specflow.local"
    assert gw.mode == "fake"


@pytest.mark.parametrize("env", ["dev", "prod", ""])
def test_fake_refuses_outside_local(env):
    with pytest.raises(ConfigurationError) as raised:
        FakeDriveGateway(app_env=env)
    assert raised.value.variables == ("APP_ENV",)


def test_recorded_folder_counts_without_network(gw):
    with pytest.raises(AssertionError):
        socket.create_connection(("example.com", 80))
    root = gw.check_folder("fake-synthetic-30")
    listing = gw.list_all("fake-synthetic-30")
    assert root.name == "Recorded folder"
    assert listing.folder_count == 7
    assert len(listing.items) == 30
    assert {c.category for c in summarize(listing)} == set(CATEGORY_ORDER)


def test_synthetic_23_shape(gw):
    listing = gw.list_all("fake-synthetic-23")
    assert (listing.folder_count, len(listing.items)) == (6, 23)
    assert sum(i.mime_type == "application/pdf" for i in listing.items) <= 3


def test_subfolder_lists_only_its_subtree(gw):
    sub = gw.list_all("fake-synthetic-30-f1")
    assert sub.root.id == "fake-synthetic-30-f1"
    assert sub.folder_count == 3
    assert 0 < len(sub.items) < 30


def test_empty_and_forms_only(gw):
    assert gw.list_all("fake-empty-folder").items == ()
    forms = gw.list_all("fake-forms-only")
    assert [categorize(i.mime_type)[1].value for i in forms.items] == [
        "skipped",
        "skipped",
    ]


@pytest.mark.parametrize(
    "reason",
    [
        "no_access",
        "not_a_folder",
        "drive_unavailable",
        "setup_error",
        "too_slow",
        "unknown_error",
    ],
)
def test_reserved_reason_ids(gw, reason):
    with pytest.raises(DriveError) as raised:
        gw.check_folder(f"fake-reason-{reason}")
    assert raised.value.reason is Reason(reason)


def test_too_slow_list_waits_for_the_deadline(gw):
    started = time.monotonic()
    with pytest.raises(DriveError) as raised:
        gw.list_all("fake-reason-too_slow", deadline=started + 0.05)
    assert raised.value.reason is Reason.TOO_SLOW
    assert time.monotonic() - started >= 0.05


def test_import_fails_api_role_passes_check_and_listing(gw):
    """FR-GDUI-0018.AC4: Test connection and Save succeed (api role, the default)."""
    assert gw.check_folder("fake-import-fails").id == "fake-import-fails"
    listing = gw.list_all("fake-import-fails")
    same = gw.list_all("fake-synthetic-23")
    assert listing.root.id == "fake-import-fails"
    assert (len(listing.items), listing.folder_count) == (23, same.folder_count)


def test_import_fails_worker_role_passes_check_then_fails_listing(google_samples):
    worker = FakeDriveGateway(app_env="local", google_samples=google_samples, file_delay_s=0, role="worker")
    assert worker.check_folder("fake-import-fails").id == "fake-import-fails"
    with pytest.raises(DriveError) as raised:
        worker.list_all("fake-import-fails")
    assert raised.value.reason is Reason.NO_ACCESS
    # the role changes nothing else
    assert len(worker.list_all("fake-synthetic-23").items) == 23


def test_an_unknown_role_is_refused():
    with pytest.raises(ValueError, match="unknown fake Drive role"):
        FakeDriveGateway(app_env="local", role="batch")


def test_any_other_valid_id_is_no_access(gw):
    with pytest.raises(DriveError) as raised:
        gw.check_folder("1AbCdEfGhIjKlMnOpQrStUvWxYz012345")
    assert raised.value.reason is Reason.NO_ACCESS
    with pytest.raises(DriveError) as raised:
        gw.check_folder("bad id")
    assert raised.value.reason is Reason.INVALID_LINK


def test_every_reserved_id_in_the_contract_is_answered(gw):
    for folder_id, answer in CONTRACT["reserved_folder_ids"].items():
        if answer.startswith("reason:"):
            with pytest.raises(DriveError):
                gw.check_folder(folder_id)
        else:
            assert gw.check_folder(folder_id).id == folder_id
    assert len(CONTRACT["reserved_folder_ids"]) == 11


def test_pdf_download_is_a_valid_one_page_pdf(gw):
    pdf = next(i for i in gw.list_all("fake-synthetic-30").items if i.mime_type == "application/pdf")
    data = b"".join(gw.iter_download(pdf.id))
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        assert doc.page_count == 1
        assert "Fake Drive" in doc[0].get_text()


def test_other_download_is_tiny_and_chunked(gw):
    item = next(i for i in gw.list_all("fake-synthetic-30").items if i.mime_type == "image/png")
    chunks = list(gw.iter_download(item.id, chunk_size=5))
    assert b"".join(chunks).startswith(b"fake drive file")
    assert max(map(len, chunks)) <= 5
    with pytest.raises(DriveError) as raised:
        list(gw.iter_download("fake-synthetic-30-i999"))
    assert raised.value.reason is Reason.NO_ACCESS


def test_read_google_uses_the_sample_files(gw):
    items = {i.mime_type: i for i in gw.list_all("fake-synthetic-30").items}
    for mime, name in (
        ("application/vnd.google-apps.document", "document"),
        ("application/vnd.google-apps.presentation", "presentation"),
        ("application/vnd.google-apps.spreadsheet", "spreadsheet"),
    ):
        assert gw.read_google(items[mime].id, mime) == {"placeholder": name}
    with pytest.raises(DriveError):
        gw.read_google(items["application/pdf"].id, "application/pdf")


def test_read_google_without_samples_is_unknown_error(tmp_path):
    gw = FakeDriveGateway(app_env="local", google_samples=tmp_path, file_delay_s=0)
    item = next(i for i in gw.list_all("fake-synthetic-30").items if "document" in i.mime_type)
    with pytest.raises(DriveError) as raised:
        gw.read_google(item.id, item.mime_type)
    assert raised.value.reason is Reason.UNKNOWN_ERROR


def test_default_samples_are_the_converters_package_files():
    """Default path: ``drive.convert/samples`` (placeholders until they exist)."""
    gateway = FakeDriveGateway(app_env="local", file_delay_s=0)
    item = next(i for i in gateway.list_all("fake-synthetic-30").items if "document" in i.mime_type)
    outcome: object
    try:
        outcome = gateway.read_google(item.id, item.mime_type)
    except DriveError as error:
        outcome = error.reason
    assert outcome is Reason.UNKNOWN_ERROR or isinstance(outcome, dict)


def test_synthetic_23_import_fits_sixty_seconds_at_default_delay():
    """NFR-PORT-0002.AC3: one delay per file, files one after another, 3 PDF parses."""
    slept: list[float] = []
    gateway = FakeDriveGateway(app_env="local", sleep=slept.append)
    assert gateway._delay == 1.0
    items = gateway.list_all("fake-synthetic-23").items
    for item in items:
        if item.mime_type.startswith("application/vnd.google-apps."):
            with contextlib.suppress(DriveError):
                gateway.read_google(item.id, item.mime_type)
        else:
            list(gateway.iter_download(item.id))
    pdfs = sum(i.mime_type == "application/pdf" for i in items)
    parse_allowance_s = 5 * pdfs  # a 1-page PDF parses in well under a second
    assert sum(slept) + parse_allowance_s <= 60


def test_recordings_hold_no_real_names_ids_or_emails():
    names = (
        r"(?:Google Docs|Google Slides|Google Sheets|PDF|Word documents|PowerPoint files|Excel files"
        r"|Images|Videos|Other files|Other Google files|Shortcuts) [0-9]+"
    )
    for name in ("synthetic-30", "synthetic-23"):
        data = json.loads(files("drive").joinpath(f"fixtures/recordings/{name}.json").read_text("utf-8"))
        assert set(data) == {"recorded", "folders", "items"}
        for folder in data["folders"]:
            assert re.fullmatch(r"Recorded folder|Folder [0-9]+", folder["name"])
        for item in data["items"]:
            assert re.fullmatch(names, item["name"])
            assert set(item) == {"name", "mimeType", "size", "modifiedTime", "folder"}
        text = json.dumps([{k: v for k, v in i.items() if k != "mimeType"} for i in data["items"]] + data["folders"])
        assert "@" not in text
        assert not re.search(r"[A-Za-z0-9_-]{25,}", text)


def test_tiny_pdf_constant():
    assert fake_module.TINY_PDF.startswith(b"%PDF-")


def test_item_id_as_folder_is_not_a_folder(gw):
    with pytest.raises(DriveError) as raised:
        gw.check_folder("fake-synthetic-30-i0")
    assert raised.value.reason is Reason.NOT_A_FOLDER
