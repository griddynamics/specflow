"""What W0 froze concretely for the Dropbox streams (docs/dropbox/source.md, oauth.md): the canonical link, the entry
parser over the recorded shapes, the workspace of a connection (DB5), the request bodies of the source routes and the
caps (DB11). The stubs stay for S1 and S2."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from tests.dropbox_support import (
    FOLDER,
    PAPER_ENTRY,
    PDF_ENTRY,
    SHARED_FOREIGN_META,
    T0,
)

from api.imports import dropbox as dropbox_import
from api.schemas import DROPBOX_EXCLUDE_MAX, DropboxCheckOut, DropboxLinkIn
from dropbox.connections import ConnectionView, StoredConnection
from dropbox.content import entry_of
from dropbox.links import DropboxTarget, canonical_link


def test_the_canonical_link_is_the_home_address_of_the_path_url_encoded() -> None:
    assert canonical_link("/Graphico Lab") == "https://www.dropbox.com/home/Graphico%20Lab"
    assert canonical_link("/Zespół/Spec #1.pdf") == "https://www.dropbox.com/home/Zesp%C3%B3%C5%82/Spec%20%231.pdf"


def test_a_target_never_shows_its_shared_link_or_path() -> None:
    target = DropboxTarget("shared", shared_link="https://www.dropbox.com/scl/fo/secret/x?rlkey=k", path=None)

    assert "secret" not in repr(target) and "rlkey" not in repr(target)
    assert "Lab" not in repr(DropboxTarget("path", path="/Lab"))


def test_entries_parse_the_recorded_shapes_and_hide_names_in_repr() -> None:
    folder, pdf, paper, foreign = (entry_of(v) for v in (FOLDER, PDF_ENTRY, PAPER_ENTRY, SHARED_FOREIGN_META))

    assert folder is not None and (folder.kind, folder.path_lower) == ("folder", "/graphico lab")
    assert pdf is not None and (pdf.kind, pdf.size, pdf.server_modified) == ("file", 592_000, "2026-11-20T10:00:01Z")
    assert paper is not None and paper.is_downloadable is False
    assert foreign is not None and foreign.path_lower is None  # not in the member's Dropbox (DB7)
    assert "Spec" not in repr(pdf) and entry_of({".tag": "file"}) is None and entry_of("x") is None


def test_the_workspace_is_the_team_or_the_person() -> None:
    team = ConnectionView.of(StoredConnection(*_row(team_id="dbtid:T", team_name="Acme")))
    person = ConnectionView.of(StoredConnection(*_row()))
    expired = ConnectionView.of(StoredConnection(*_row(status="expired")))

    assert (team.workspace_id, team.workspace_name) == ("team:dbtid:T", "Acme")
    assert (person.workspace_id, person.workspace_name) == ("user:dbid:A", "Ana")
    assert expired.status == "expired"


def _row(*, team_id: str | None = None, team_name: str | None = None, status: str = "active") -> tuple:
    return (uuid.uuid4(), "dbid:A", "Ana", team_id, team_name, b"x", status, T0, T0)


def test_the_link_body_takes_dropbox_ids_to_exclude_up_to_the_file_cap() -> None:
    assert DropboxLinkIn(link="x").exclude == []
    assert DropboxLinkIn(link="x", exclude=["id:a4ayc_80_OEAAAAAAAAAXw"]).exclude == ["id:a4ayc_80_OEAAAAAAAAAXw"]
    for bad in (["a4ayc"], ["id:"], ["id:../x"], ["id:x"] * (DROPBOX_EXCLUDE_MAX + 1)):
        with pytest.raises(ValidationError):
            DropboxLinkIn(link="x", exclude=bad)
    with pytest.raises(ValidationError):
        DropboxLinkIn.model_validate({"link": "x", "other": 1})


def test_the_check_answer_has_the_frozen_shape() -> None:
    assert list(DropboxCheckOut.model_fields) == [
        "kind",
        "name",
        "files",
        "files_capped",
        "skipped",
        "folders",
        "bytes",
        "capped",
    ]


def test_the_caps_are_the_ones_of_the_decision() -> None:
    assert (dropbox_import.MAX_FILES, dropbox_import.MAX_ENTRIES, dropbox_import.DOWNLOAD_SLOTS) == (1000, 5000, 4)
    assert dropbox_import.MAX_BYTES == 2 * 1024**3 and DROPBOX_EXCLUDE_MAX == dropbox_import.MAX_FILES
