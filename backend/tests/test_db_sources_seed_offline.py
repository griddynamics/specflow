"""Tier 1 (no database): the synthetic data generator of the migration rehearsal.

Requirements: sources-ux SPECS 3.2 "Rehearsal" (R04).
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import UTC, datetime
from types import ModuleType

import pytest

import seed_synthetic_sources
from settings import DatabaseSettings

PLACEHOLDER_CRED = "p"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def seed() -> ModuleType:
    return seed_synthetic_sources


@pytest.fixture(scope="module")
def full(seed: ModuleType):
    generator = seed.Generator(20261021, 1.0, NOW)
    generator.build()
    return generator


def test_the_full_set_has_the_planned_size_and_shape(full) -> None:
    uploads = [d for d in full.documents if d["origin"] == "upload"]
    sizes = Counter(i["c"] for i in full.items)
    by_source = {c["id"]: sizes[c["id"]] for c in full.connections}

    assert len(full.projects) == 30 and len(full.users) == 12
    assert len(uploads) == 20_000
    assert len(full.connections) == 15
    assert full.perf_source is not None and by_source[full.perf_source] == 1000
    assert all(200 <= n <= 1000 for n in by_source.values())
    days = {d["at"].date() for d in uploads}
    assert 55 <= len(days) <= 60
    assert 0.03 < sum(d["by"] is None for d in uploads) / len(uploads) < 0.07
    assert 0.08 < sum(d["status"] == "failed" for d in uploads) / len(uploads) < 0.12


def test_every_item_state_and_error_reason_appears(full) -> None:
    assert {i["state"] for i in full.items} == {
        "pending",
        "processed",
        "stored",
        "skipped",
        "error",
    }
    assert {i["reason"] for i in full.items if i["state"] == "error"} == {
        "no_access",
        "drive_unavailable",
        "convert_failed",
        "parse_failed",
        "store_failed",
        "import_failed",
        "unknown_error",
    }
    assert Counter(c["state"] for c in full.connections).keys() == {
        "done",
        "importing",
        "failed",
    }


def test_the_rows_satisfy_the_item_check_constraints(full) -> None:
    for item in full.items:
        assert (item["state"] == "error") == (item["reason"] is not None)
        assert item["step"] in {
            None,
            "list",
            "download",
            "read",
            "convert",
            "store",
            "parse",
        }
        assert item["cls"] in {"processed", "stored_only", "skipped"}
    for source in full.connections:
        assert (source["state"] == "failed") == (source["reason"] is not None)
        assert re.fullmatch(r"[A-Za-z0-9_-]{10,}", source["ext"])
    pairs = [(d["p"], d["hash"]) for d in full.documents]
    assert len(set(pairs)) == len(pairs)  # the unique index on (project_id, content_hash)
    assert all(re.fullmatch(r"[0-9a-f]{64}", d["hash"]) for d in full.documents)
    assert {d["origin"] for d in full.documents} == {"upload", "connection"}
    assert len({i["id"] for i in full.items}) == len(full.items)


def test_about_five_percent_of_the_items_link_a_document_an_earlier_source_holds(
    full,
) -> None:
    own = {d["id"] for d in full.documents if d["origin"] == "connection"}
    owner: dict[uuid.UUID, uuid.UUID] = {}
    for item in full.items:
        if item["doc"] is not None and item["doc"] not in owner:
            owner[item["doc"]] = item["c"]  # the first item to name the document
    links = [i for i in full.items if i["doc"] is not None and owner[i["doc"]] != i["c"]]

    assert links and all(i["doc"] in own for i in links)
    assert 0.02 < len(links) / len(full.items) < 0.09
    assert {i["state"] for i in links} == {"processed", "error"}
    assert {i["reason"] for i in links if i["state"] == "error"} == {"parse_failed"}


def test_the_same_seed_gives_the_same_rows_and_another_seed_does_not(
    seed: ModuleType,
) -> None:
    def build(value: int) -> object:
        generator = seed.Generator(value, 0.02, NOW)
        generator.build()
        return generator.documents, generator.items, generator.connections

    assert build(1) == build(1)
    assert build(1) != build(2)


def test_every_insert_binds_exactly_the_keys_of_its_rows(seed: ModuleType) -> None:
    generator = seed.Generator(7, 0.02, NOW)
    generator.build()

    for statement, rows in (
        (seed.INSERT_USER, generator.users),
        (seed.INSERT_PROJECT, generator.projects),
        (seed.INSERT_MEMBER, generator.members),
        (seed.INSERT_CONNECTION, generator.connections),
        (seed.INSERT_DOCUMENT, generator.documents),
        (seed.INSERT_ITEM, generator.items),
    ):
        names = set(statement.compile().params)
        assert rows and all(set(row) == names for row in rows)


def test_the_script_leaves_the_holder_and_batch_columns_to_their_defaults(
    seed: ModuleType,
) -> None:
    statements = (
        seed.INSERT_USER,
        seed.INSERT_PROJECT,
        seed.INSERT_MEMBER,
        seed.INSERT_CONNECTION,
        seed.INSERT_DOCUMENT,
        seed.INSERT_ITEM,
    )

    for statement in statements:
        sql = str(statement)
        for new_column in (
            "source_id",
            "expected_files",
            "ended_at",
            "name_suffix",
            "removed_files",
        ):
            assert new_column not in sql


def test_the_summary_names_the_perf_source_by_ids_only(seed: ModuleType) -> None:
    generator = seed.Generator(7, 1.0, NOW)
    generator.build()

    summary = generator.summary()["seed_summary"]

    assert summary["perf_source"]["items"] == 1000
    assert set(summary["perf_source"]) == {"project_id", "source_id", "items"}
    assert summary["items_by_state"]["pending"] > 0


@pytest.mark.parametrize(
    ("name", "host", "problem"),
    [
        ("specflow", "localhost", "database name"),
        ("prod_database", "localhost", "database name"),
        ("specflow_rehearsal", "db.internal", "host"),
        ("specflow_rehearsal", "10.0.0.5", "host"),
    ],
)
def test_the_script_refuses_anything_but_a_local_scratch_database(
    seed: ModuleType, name: str, host: str, problem: str
) -> None:
    settings = DatabaseSettings(host=host, port=5432, name=name, user="u", password=PLACEHOLDER_CRED)

    assert problem in (seed.refusal(settings) or "")


def test_a_local_rehearsal_or_pytest_database_is_accepted(seed: ModuleType) -> None:
    for name, host in (
        ("specflow_rehearsal", "localhost"),
        ("specflow_rehearsal_2", "127.0.0.1"),
        ("specflow_pytest_ab12", "::1"),
    ):
        assert (
            seed.refusal(DatabaseSettings(host=host, port=5432, name=name, user="u", password=PLACEHOLDER_CRED)) is None
        )


def test_main_refuses_before_it_connects(
    seed: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key, value in (
        ("DATABASE_HOST", "db.internal"),
        ("DATABASE_PORT", "5432"),
        ("DATABASE_NAME", "x"),
        ("DATABASE_USER", "u"),
        ("DATABASE_PASSWORD", "dummy-pw-1"),
    ):
        monkeypatch.setenv(key, value)

    assert seed.main(["--database", "specflow_rehearsal"]) == 2
    assert seed.main(["--database", "specflow"]) == 2
    assert seed.main(["--database", "specflow_rehearsal", "--scale", "0"]) == 2
    err = capsys.readouterr().err
    assert "refused" in err and "dummy-pw-1" not in err
