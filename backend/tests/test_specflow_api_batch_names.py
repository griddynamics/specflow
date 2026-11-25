"""Tier 1: naming an upload batch at start and renaming it later (``PATCH /sources/{sid}``).

The database is the in-memory ``FakeBatches``; the name rules are the real schema.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from pydantic import ValidationError

from api import errors
from api.schemas import BatchStartIn, SourceRenameIn
from logger_harness import events, fields
from specflow_api_support import bearer
from test_specflow_api_batches import ALICE, BOB, code, files, project, start, url


async def rename(harness, pid, sid, body, email=ALICE):
    return await harness.client.patch(url(pid, f"/{sid}"), json=body, headers=bearer(email))


async def start_named(harness, pid, body):
    return await harness.client.post(url(pid, "/uploads"), json=body, headers=bearer(ALICE))


# ---- the name rules ----------------------------------------------------------------


def test_the_start_name_is_stripped_and_blank_means_no_name():
    assert BatchStartIn(expected_files=1).name is None
    assert BatchStartIn(expected_files=1, name=None).name is None
    assert BatchStartIn(expected_files=1, name="  \xa0 ").name is None
    assert BatchStartIn(expected_files=1, name="  Pricing research ").name == "Pricing research"
    assert BatchStartIn(expected_files=1, name="x" * 80).name == "x" * 80


@pytest.mark.parametrize("name", ["x" * 81, "a\nb", "a\tb", "a\x00b", "a\u2028b"])
def test_an_invalid_name_is_refused_by_both_bodies(name):
    with pytest.raises(ValidationError, match="the name must"):
        BatchStartIn(expected_files=1, name=name)
    with pytest.raises(ValidationError, match="the name must"):
        SourceRenameIn(name=name)


@pytest.mark.parametrize("name", ["", "   ", "\xa0"])
def test_a_blank_rename_is_invalid(name):
    with pytest.raises(ValidationError, match="the name must"):
        SourceRenameIn(name=name)


def test_the_new_error_code_is_a_409():
    assert (errors.source_not_renamable().status, errors.source_not_renamable().code) == (409, "source_not_renamable")


# ---- name at start -----------------------------------------------------------------


async def test_start_without_a_name_keeps_the_default(harness):
    pid = await project(harness)
    for body in ({"expected_files": 2}, {"expected_files": 2, "name": None}, {"expected_files": 2, "name": "  "}):
        response = await start_named(harness, pid, body)
        assert response.status_code == 201 and response.json()["source"]["name"] == "Local files"


async def test_start_with_a_name_stores_it_stripped_and_numbers_as_before(harness):
    pid = await project(harness)
    first = await start_named(harness, pid, {"expected_files": 3, "name": "  Pricing research "})
    second = await start_named(harness, pid, {"expected_files": 3, "name": "Pricing research"})
    assert first.status_code == 201
    one, two = first.json()["source"], second.json()["source"]
    assert (one["name"], one["name_suffix"]) == ("Pricing research", 1)
    assert (two["name"], two["name_suffix"]) == ("Pricing research", 2)


@pytest.mark.parametrize("name", ["x" * 81, "a\nb", "a\x07b", 5])
async def test_start_with_an_invalid_name_is_422_and_creates_nothing(harness, name):
    pid = await project(harness)
    response = await start_named(harness, pid, {"expected_files": 3, "name": name})
    assert response.status_code == 422 and code(response) == "validation_failed"
    [error] = response.json()["error"]["details"]["errors"]
    assert error["loc"][-1] == "name"
    assert not harness.batches.sources


# ---- rename ------------------------------------------------------------------------


async def test_rename_answers_the_source_summary_and_the_reads_show_the_name(harness, caplog):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    with caplog.at_level("DEBUG"):
        response = await rename(harness, pid, sid, {"name": "  Board decks "})
    assert response.status_code == 200
    source = response.json()
    assert source["id"] == sid and source["name"] == "Board decks" and source["name_suffix"] == 1
    page = (await files(harness, pid, sid)).json()
    assert page["source"] == source
    listed = (await harness.client.get(url(pid, "?batches=1"), headers=bearer())).json()
    assert [s["name"] for s in listed["sources"]] == ["Board decks"]
    [record] = events(caplog, "api.source.renamed")
    logged = fields(record)
    assert logged["source_id"] == sid and logged["project_id"] == pid
    assert "Board decks" not in caplog.text


@pytest.mark.parametrize(
    "body", [{"name": ""}, {"name": "   "}, {"name": "x" * 81}, {"name": "a\nb"}, {}, {"name": None}]
)
async def test_rename_with_an_invalid_name_is_422_and_changes_nothing(harness, body):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    response = await rename(harness, pid, sid, body)
    assert response.status_code == 422 and code(response) == "validation_failed"
    assert (await files(harness, pid, sid)).json()["source"]["name"] == "Local files"


async def test_rename_refuses_an_extra_field(harness):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    response = await rename(harness, pid, sid, {"name": "ok", "kind": "gdrive"})
    assert response.status_code == 422 and code(response) == "validation_failed"


async def test_rename_of_a_non_batch_source_is_409(harness):
    pid = await project(harness)
    drive = harness.batches.add_source(uuid.UUID(pid), kind="gdrive", name="Folder")
    response = await rename(harness, pid, drive.id, {"name": "New"})
    assert response.status_code == 409 and code(response) == "source_not_renamable"
    assert drive.name == "Folder"


async def test_rename_of_an_unknown_source_or_a_hidden_batch_is_404(harness):
    pid = await project(harness)
    hidden = harness.batches.add_source(uuid.UUID(pid), ended=False, expected=2, age=timedelta(minutes=16))
    for sid in (uuid.uuid4(), hidden.id):
        response = await rename(harness, pid, sid, {"name": "New"})
        assert response.status_code == 404 and code(response) == "source_not_found"
    assert hidden.name == "Local files"


async def test_rename_of_another_projects_source_is_404_and_changes_nothing(harness):
    mine, theirs = await project(harness), await project(harness)
    sid = (await start(harness, theirs)).json()["source"]["id"]
    response = await rename(harness, mine, sid, {"name": "New"})
    assert response.status_code == 404 and code(response) == "source_not_found"
    assert (await files(harness, theirs, sid)).json()["source"]["name"] == "Local files"


async def test_rename_by_a_non_member_is_project_not_found(harness):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    await project(harness, BOB)
    response = await rename(harness, pid, sid, {"name": "New"}, BOB)
    assert response.status_code == 404 and code(response) == "project_not_found"
    assert (await files(harness, pid, sid)).json()["source"]["name"] == "Local files"
