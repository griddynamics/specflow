"""Identity rules and the ``SpecIdRegistry`` contract (plan 1.T3; FR-CORE-06; data-model.md "Identity")."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from graph.core import files as files_module
from graph.core.errors import ConfigurationError, InfrastructureUnavailableError
from graph.core.files import FileSpecIdRegistry, write_atomic
from graph.core.identity import (
    NAMESPACE,
    SLUG_MAX_CHARS,
    SpecIdRegistry,
    SpecIdRow,
    allocate_spec_id,
    edge_uuid,
    identity_key,
    node_uuid,
    normalise_id,
    normalise_surface,
    slug,
    validate_project_id,
)

# validate_project_id


@pytest.mark.parametrize("project_id", ["p", "P1", "proj-1_x", "9lives", "a" * 64])
def test_validate_project_id_accepts_partition_names(project_id: str) -> None:
    assert validate_project_id(project_id) == project_id


@pytest.mark.parametrize(
    "project_id",
    ["", " ", "-a", "_a", "a/b", "../x", "a b", "a.b", "a" * 65, "ünï", "a\n", None, 7],
)
def test_validate_project_id_rejects_everything_else(project_id: object) -> None:
    with pytest.raises(ConfigurationError) as raised:
        validate_project_id(project_id)
    assert raised.value.code == "invalid_project_id"


# normalise_id


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("FR-ADM-007", "FR-ADM-007"),
        ("fr_adm 007.", "FR-ADM-007"),
        ("  fr.012  ", "FR-012"),
        ("us-1", "US-1"),
        ("FR__ADM  007", "FR-ADM-007"),
        ("_FR_012_", "FR-012"),
        ("NFR-0012", "NFR-0012"),
    ],
)
def test_normalise_id(text: str, expected: str) -> None:
    assert normalise_id(text) == expected


# normalise_surface


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Orders Service", "orders service"),
        ("  Hello,   WORLD! ", "hello, world"),
        ("Straße", "strasse"),
        ("\uff21\uff22\uff23", "abc"),
        ("“Quoted”", "quoted"),
        ('"Checkout service".', "checkout service"),
        ("...", ""),
        ("", ""),
        ("Orders\tservice\n", "orders service"),
    ],
)
def test_normalise_surface(text: str, expected: str) -> None:
    assert normalise_surface(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ERP (external)", "erp (external)"),
        ("(note) text", "(note) text"),
        ("[a] b [c]", "[a] b [c]"),
        ("Stripe (external).", "stripe (external)"),
        ("(unmatched", "unmatched"),
        ("unmatched)", "unmatched"),
        ("(unmatched.", "unmatched"),
    ],
)
def test_normalise_surface_keeps_brackets_whose_partner_is_inside(text: str, expected: str) -> None:
    assert normalise_surface(text) == expected


def test_normalise_surface_is_idempotent() -> None:
    for text in ["ERP (external)", "  “Hi”, there! ", "Straße", "[x] y", "(("]:
        once = normalise_surface(text)
        assert normalise_surface(once) == once


# identity_key


def test_identity_key_joins_scope_kind_and_value() -> None:
    assert identity_key("Module", "surface", "erp (external)") == "Module|surface|erp (external)"
    assert identity_key("requirement", "id", "FR-012") == "requirement|id|FR-012"
    assert identity_key("requirement", "span", "users can log in") == ("requirement|span|users can log in")


# slug


def test_slug_uppercases_and_joins_with_underscores() -> None:
    assert slug("Checkout service") == "CHECKOUT_SERVICE"
    assert slug("Order-history (CSV) export!") == "ORDER_HISTORY_CSV_EXPORT"


def test_slug_drops_stop_words() -> None:
    assert slug("The export of the orders") == "EXPORT_ORDERS"
    assert slug("users can log in to the app") == "USERS_LOG_APP"


def test_slug_keeps_the_first_five_words() -> None:
    assert slug("one two three four five six seven") == "ONE_TWO_THREE_FOUR_FIVE"
    assert slug("Users can export the order history as CSV") == "USERS_EXPORT_ORDER_HISTORY_CSV"


def test_slug_explicit_id_leads_as_one_word() -> None:
    assert slug("Export orders", "FR-012") == "FR_012_EXPORT_ORDERS"
    assert slug("Export orders", "fr_adm 007") == "FR_ADM_007_EXPORT_ORDERS"
    # the id counts as one of the five words
    assert slug("a1 b2 c3 d4 e5 f6", "FR-1") == "FR_1_A1_B2_C3_D4"


def test_slug_explicit_id_is_not_repeated_by_an_identical_word_of_the_name() -> None:
    assert slug("FR012 export", "FR012") == "FR012_EXPORT"
    # the name may spell the id with other separators or case
    assert slug("FR-012 export", "FR-012") == "FR_012_EXPORT"
    assert slug("FR-012: Users can export order history", "fr_012") == "FR_012_USERS_EXPORT_ORDER_HISTORY"


def test_slug_explicit_id_alone_when_the_name_has_only_stop_words() -> None:
    assert slug("the of and", "NFR-7") == "NFR_7"


def test_slug_is_at_most_80_characters() -> None:
    long = slug(" ".join(["w" * 30] * 5))
    assert len(long) <= SLUG_MAX_CHARS
    assert not long.endswith("_")
    assert slug("x" * 200) == "X" * SLUG_MAX_CHARS


@pytest.mark.parametrize("name", ["", "   ", "!!!", "the of and", "…", "—"])
def test_slug_is_never_empty(name: str) -> None:
    assert slug(name)
    assert slug(name, "") == slug(name)


# node_uuid and edge_uuid


def test_namespace_is_the_documented_constant() -> None:
    assert uuid.UUID("36012a50-eff4-4a22-8410-fad8c8a2474a") == NAMESPACE


def test_node_uuid_is_uuid5_over_project_node_and_key() -> None:
    key = identity_key("Module", "surface", "erp")
    assert node_uuid("proj", key) == str(uuid.uuid5(NAMESPACE, "proj|node|Module|surface|erp"))


def test_edge_uuid_is_uuid5_over_project_edge_and_key() -> None:
    assert edge_uuid("proj", "a|PART_OF|b") == str(uuid.uuid5(NAMESPACE, "proj|edge|a|PART_OF|b"))


def test_uuids_are_pinned() -> None:
    """A change here changes every stored uuid: the namespace and the format never change."""

    assert node_uuid("p", "k") == "a5521adc-a426-5ecc-b5e7-818e8eeb2c7c"
    assert edge_uuid("p", "k") == "453a2a4b-0ddf-5c05-a83b-4556e45c36bc"


def test_uuids_are_stable_and_partitioned() -> None:
    key = "Module|surface|erp"
    assert node_uuid("p1", key) == node_uuid("p1", key)
    assert node_uuid("p1", key) != node_uuid("p2", key)
    assert node_uuid("p1", key) != node_uuid("p1", "Module|surface|crm")
    assert node_uuid("p1", key) != edge_uuid("p1", key)
    uuid.UUID(node_uuid("p1", key))


# allocate_spec_id (FR-CORE-06)


def row(spec_id: str, key: str, state: str = "active") -> SpecIdRow:
    return SpecIdRow(spec_id, key, node_uuid("p", key), state)


def test_allocate_returns_prefix_plus_slug_when_free() -> None:
    assert allocate_spec_id("MOD::", "ORDERS", "Module|surface|orders", {}) == "MOD::ORDERS"


def test_allocate_suffixes_when_taken_by_another_key() -> None:
    taken = {"MOD::ORDERS": row("MOD::ORDERS", "Module|surface|orders")}
    assert allocate_spec_id("MOD::", "ORDERS", "Module|surface|orders 2", taken) == "MOD::ORDERS_2"
    taken["MOD::ORDERS_2"] = row("MOD::ORDERS_2", "Module|surface|orders 2")
    assert allocate_spec_id("MOD::", "ORDERS", "Module|surface|orders 3", taken) == "MOD::ORDERS_3"


@pytest.mark.parametrize("state", ["active", "retired"])
def test_allocate_reuses_the_id_for_the_same_key(state: str) -> None:
    taken = {"MOD::ORDERS": row("MOD::ORDERS", "Module|surface|orders", state)}
    assert allocate_spec_id("MOD::", "ORDERS", "Module|surface|orders", taken) == "MOD::ORDERS"


def test_allocate_returns_the_suffixed_id_of_the_same_key() -> None:
    taken = {
        "MOD::ORDERS": row("MOD::ORDERS", "Module|surface|other"),
        "MOD::ORDERS_2": row("MOD::ORDERS_2", "Module|surface|orders", "retired"),
    }
    assert allocate_spec_id("MOD::", "ORDERS", "Module|surface|orders", taken) == "MOD::ORDERS_2"


def test_allocate_never_reuses_an_alias_row_even_for_the_same_key() -> None:
    taken = {"MOD::ORDERS": row("MOD::ORDERS", "Module|surface|orders", "alias")}
    assert allocate_spec_id("MOD::", "ORDERS", "Module|surface|orders", taken) == "MOD::ORDERS_2"


def test_allocate_never_reuses_another_keys_id_whatever_its_state() -> None:
    """Build 2 deletes a node; build 3 creates another with the same name: a different spec_id."""

    for state in ("active", "retired", "alias"):
        taken = {"REQ::FR::EXPORT": row("REQ::FR::EXPORT", "requirement|span|old line", state)}
        got = allocate_spec_id("REQ::FR::", "EXPORT", "requirement|span|new line", taken)
        assert got == "REQ::FR::EXPORT_2"


def test_allocate_does_not_mutate_the_taken_mapping() -> None:
    taken = {"MOD::A": row("MOD::A", "Module|surface|a")}
    allocate_spec_id("MOD::", "A", "Module|surface|b", taken)
    assert list(taken) == ["MOD::A"]


# SpecIdRegistry contract suite: every implementation passes the same tests

RegistryFactory = Callable[[Path], SpecIdRegistry]

IMPLEMENTATIONS = [pytest.param(FileSpecIdRegistry, id="file")]


@pytest.fixture(params=IMPLEMENTATIONS)
def registry(request: pytest.FixtureRequest, tmp_path: Path) -> SpecIdRegistry:
    factory: RegistryFactory = request.param
    return factory(tmp_path / "registry")


async def test_registry_of_a_new_project_is_empty(registry: SpecIdRegistry) -> None:
    assert await registry.taken("proj") == {}


async def test_registry_record_taken_round_trip(registry: SpecIdRegistry) -> None:
    rows = [row("MOD::ORDERS", "Module|surface|orders"), row("FEAT::CART", "Feature|surface|cart")]
    await registry.record("proj", "build-1", rows)
    assert await registry.taken("proj") == {r.spec_id: r for r in rows}


async def test_registry_record_upserts_by_spec_id(registry: SpecIdRegistry) -> None:
    await registry.record(
        "proj",
        "build-1",
        [row("MOD::A", "Module|surface|a"), row("MOD::B", "Module|surface|b")],
    )
    await registry.record("proj", "build-2", [row("MOD::A", "Module|surface|a", "retired")])
    taken = await registry.taken("proj")
    assert set(taken) == {"MOD::A", "MOD::B"}
    assert taken["MOD::A"].state == "retired"
    assert taken["MOD::B"].state == "active"


async def test_registry_record_with_no_rows_is_harmless(registry: SpecIdRegistry) -> None:
    await registry.record("proj", "build-1", [])
    assert await registry.taken("proj") == {}


async def test_registry_projects_are_isolated(registry: SpecIdRegistry) -> None:
    await registry.record("one", "b1", [row("MOD::A", "Module|surface|a")])
    assert await registry.taken("two") == {}
    await registry.record("two", "b1", [row("MOD::A", "Module|surface|other")])
    assert (await registry.taken("one"))["MOD::A"].identity_key == "Module|surface|a"
    assert (await registry.taken("two"))["MOD::A"].identity_key == "Module|surface|other"


@pytest.mark.parametrize("project_id", ["", "../escape", "a/b", "-x", "a b"])
async def test_registry_rejects_an_invalid_project_id(
    registry: SpecIdRegistry, project_id: str, tmp_path: Path
) -> None:
    with pytest.raises(ConfigurationError) as taken_error:
        await registry.taken(project_id)
    assert taken_error.value.code == "invalid_project_id"
    with pytest.raises(ConfigurationError) as record_error:
        await registry.record(project_id, "b1", [row("MOD::A", "Module|surface|a")])
    assert record_error.value.code == "invalid_project_id"
    assert not (tmp_path / "escape").exists()


async def test_registry_allocation_flow_keeps_ids_stable_across_builds(
    registry: SpecIdRegistry,
) -> None:
    """The registry plus ``allocate_spec_id``: a returning key keeps its id, a new key never takes it."""

    old = "requirement|span|old line"
    spec_id = allocate_spec_id("REQ::FR::", "EXPORT", old, await registry.taken("proj"))
    await registry.record("proj", "b1", [SpecIdRow(spec_id, old, node_uuid("proj", old), "active")])
    await registry.record("proj", "b2", [SpecIdRow(spec_id, old, node_uuid("proj", old), "retired")])
    taken = await registry.taken("proj")
    assert allocate_spec_id("REQ::FR::", "EXPORT", old, taken) == spec_id
    assert allocate_spec_id("REQ::FR::", "EXPORT", "requirement|span|new line", taken) == ("REQ::FR::EXPORT_2")


# FileSpecIdRegistry specifics


def files_under(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


async def test_file_registry_persists_across_instances(tmp_path: Path) -> None:
    rows = [row("MOD::A", "Module|surface|a")]
    await FileSpecIdRegistry(tmp_path).record("proj", "b1", rows)
    assert await FileSpecIdRegistry(tmp_path).taken("proj") == {"MOD::A": rows[0]}


async def test_file_registry_writes_one_json_file_per_project(tmp_path: Path) -> None:
    registry = FileSpecIdRegistry(tmp_path)
    await registry.record("proj", "b1", [row("MOD::A", "Module|surface|a")])
    await registry.record("proj", "b2", [row("MOD::B", "Module|surface|b")])
    assert files_under(tmp_path) == ["proj/spec-ids.json"]
    data = json.loads((tmp_path / "proj" / "spec-ids.json").read_text(encoding="utf-8"))
    assert set(data["rows"]) == {"MOD::A", "MOD::B"}
    assert data["builds"] == ["b1", "b2"]


async def test_file_registry_records_a_build_id_once(tmp_path: Path) -> None:
    registry = FileSpecIdRegistry(tmp_path)
    await registry.record("proj", "b1", [row("MOD::A", "Module|surface|a")])
    await registry.record("proj", "b1", [row("MOD::B", "Module|surface|b")])
    data = json.loads((tmp_path / "proj" / "spec-ids.json").read_text(encoding="utf-8"))
    assert data["builds"] == ["b1"]


async def test_file_registry_write_leaves_no_temporary_files(tmp_path: Path) -> None:
    registry = FileSpecIdRegistry(tmp_path)
    for index in range(5):
        await registry.record("proj", f"b{index}", [row(f"MOD::M{index}", f"Module|surface|{index}")])
    assert files_under(tmp_path) == ["proj/spec-ids.json"]


async def test_file_registry_concurrent_records_lose_no_rows(tmp_path: Path) -> None:
    registry = FileSpecIdRegistry(tmp_path)
    await asyncio.gather(
        *(
            registry.record("proj", f"b{index}", [row(f"MOD::M{index}", f"Module|surface|{index}")])
            for index in range(12)
        )
    )
    assert set(await registry.taken("proj")) == {f"MOD::M{index}" for index in range(12)}
    assert files_under(tmp_path) == ["proj/spec-ids.json"]


async def test_file_registry_unreadable_file_is_infrastructure_unavailable(
    tmp_path: Path,
) -> None:
    registry = FileSpecIdRegistry(tmp_path)
    await registry.record("proj", "b1", [row("MOD::A", "Module|surface|a")])
    (tmp_path / "proj" / "spec-ids.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(InfrastructureUnavailableError) as raised:
        await registry.taken("proj")
    assert raised.value.code == "registry_unreadable"


async def test_file_registry_failed_write_keeps_the_old_file_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = FileSpecIdRegistry(tmp_path)
    first = row("MOD::A", "Module|surface|a")
    await registry.record("proj", "b1", [first])

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(files_module.os, "replace", fail)
    with pytest.raises(InfrastructureUnavailableError) as raised:
        await registry.record("proj", "b2", [row("MOD::B", "Module|surface|b")])
    assert raised.value.code == "registry_unwritable"
    monkeypatch.undo()
    assert files_under(tmp_path) == ["proj/spec-ids.json"]
    assert await registry.taken("proj") == {"MOD::A": first}


def test_write_atomic_replaces_content_and_creates_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "file.bin"
    write_atomic(target, b"one")
    write_atomic(target, b"two")
    assert target.read_bytes() == b"two"
    assert files_under(tmp_path) == ["a/b/file.bin"]


def test_write_atomic_cleans_up_when_the_rename_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "file.bin"
    write_atomic(target, b"keep")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("nope")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(PermissionError):
        write_atomic(target, b"lost")
    monkeypatch.undo()
    assert target.read_bytes() == b"keep"
    assert files_under(tmp_path) == ["file.bin"]
