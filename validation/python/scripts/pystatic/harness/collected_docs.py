"""Document citations of modules already collected by one pytest session."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from pystatic.harness.docref import doc_anchor_exists


def declared_anchors(module: object) -> set[str]:
    anchors: set[str] = set()
    single = getattr(module, "ANCHOR", None)
    if isinstance(single, str) and "#" in single:
        anchors.add(single)
    many: Iterable[str] | None = getattr(module, "DOC_ANCHORS", None)
    if many is not None:
        anchors.update(many)
    return anchors


def collected_modules(request: pytest.FixtureRequest, owner: Path) -> dict[str, object]:
    """Every distinct module collected into this session, this file's own
    module excluded -- by file identity, not by name (name-based exclusion
    would depend on pytest's import-mode-specific module-naming, which this
    check should not need to know about)."""
    modules: dict[str, object] = {}
    for item in request.session.items:
        module = getattr(item, "module", None)
        if module is None:
            continue
        path = getattr(module, "__file__", None)
        if path is None:
            continue
        mod_path = Path(path).resolve()
        if mod_path == owner or not mod_path.is_relative_to(owner.parent):
            continue
        modules[module.__name__] = module
    return modules


def citation_problems(request: pytest.FixtureRequest, owner: Path) -> tuple[list[str], list[str]]:
    modules = collected_modules(request, owner)
    undeclared = sorted(name for name, module in modules.items() if not declared_anchors(module))
    anchors = {anchor for module in modules.values() for anchor in declared_anchors(module)}
    return undeclared, sorted(anchor for anchor in anchors if not doc_anchor_exists(anchor))
