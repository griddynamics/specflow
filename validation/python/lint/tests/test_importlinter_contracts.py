"""One detection pair per contract in `importlinter.toml`.

Options come from `importlinter.api.read_configuration`; each contract runs on an in-memory
`grimp.ImportGraph` without the edge it forbids (kept) and with it (broken).
"""

from __future__ import annotations

from typing import Any

import grimp
import importlinter.api as importlinter_api
import pytest
from importlinter.contracts.forbidden import ForbiddenContract
from importlinter.contracts.layers import LayersContract
from importlinter.domain.contract import Contract

from tests.semgrep_support import KIT_DIR

_CONFIG = importlinter_api.read_configuration(str(KIT_DIR / "importlinter.toml"))
_SESSION_OPTIONS: dict[str, Any] = _CONFIG["session_options"]
_ROOT = _SESSION_OPTIONS["root_packages"][0]
_CONTRACT_CLASSES: dict[str, type[Contract]] = {
    "layers": LayersContract,
    "forbidden": ForbiddenContract,
}
_CONTRACTS: dict[str, tuple[type[Contract], dict[str, Any]]] = {
    row["id"]: (
        _CONTRACT_CLASSES[row["type"]],
        {k: v for k, v in row.items() if k not in ("name", "id", "type")},
    )
    for row in _CONFIG["contracts_options"]
}


def _with_ancestors(graph: grimp.ImportGraph, dotted: str) -> None:
    """A synthetic graph registers exactly the names given, so each ancestor package is
    added explicitly for a wildcard expression (`src.*`) to find it."""
    parts = dotted.split(".")
    for index in range(1, len(parts) + 1):
        graph.add_module(".".join(parts[:index]))


def _kept(contract_id: str, *edges: tuple[str, str]) -> bool:
    graph = grimp.ImportGraph()
    graph.add_module(_ROOT)
    for importer, imported in edges:
        _with_ancestors(graph, importer)
        _with_ancestors(graph, imported)
        graph.add_import(importer=importer, imported=imported, line_number=1, line_contents="x")
    contract_cls, options = _CONTRACTS[contract_id]
    contract = contract_cls(
        name=contract_id, session_options=_SESSION_OPTIONS, contract_options=options
    )
    return contract.check(graph, verbose=False).kept


def test_feature_layers_contract_catches_a_reversed_layer_import() -> None:
    assert _kept("feature-layers", ("src.orders.routes", "src.orders.service"))
    assert not _kept("feature-layers", ("src.orders.service", "src.orders.routes"))


def test_routes_thin_contract_catches_a_route_reaching_the_data_layer() -> None:
    assert _kept("routes-thin", ("src.orders.routes", "src.orders.service"))
    assert not _kept("routes-thin", ("src.orders.routes", "src.orders.repositories"))
    assert not _kept("routes-thin", ("src.orders.routes", "sqlalchemy"))


def test_no_framework_below_routes_contract_catches_a_service_importing_fastapi() -> None:
    assert _kept("no-framework-below-routes", ("src.orders.service", "pydantic"))
    assert not _kept("no-framework-below-routes", ("src.orders.service", "fastapi"))
    assert not _kept("no-framework-below-routes", ("src.orders.repositories", "starlette"))


@pytest.mark.parametrize(
    "imported", ["tests.support.clock", "vcr", "pytest", "_pytest.monkeypatch"]
)
def test_no_test_imports_contract_catches_production_reaching_test_code(imported: str) -> None:
    assert _kept("no-test-imports", ("src.orders.service", "httpx"))
    assert not _kept("no-test-imports", ("src.orders.service", imported))


def test_no_test_imports_contract_catches_an_indirect_path() -> None:
    assert not _kept(
        "no-test-imports",
        ("src.orders.service", "src.shared.helper"),
        ("src.shared.helper", "tests.support.clock"),
    )


@pytest.mark.parametrize("contract_id", sorted(_CONTRACTS))
def test_every_contract_survives_a_graph_with_no_matching_module(contract_id: str) -> None:
    """Wildcard sources resolve only to modules that exist: an empty project keeps them."""
    assert _kept(contract_id)


def test_every_configured_contract_has_a_detection_pair() -> None:
    tested_ids = {"feature-layers", "routes-thin", "no-framework-below-routes", "no-test-imports"}
    assert tested_ids == set(_CONTRACTS), (
        "a contract was added to or removed from importlinter.toml without a detection test"
    )
