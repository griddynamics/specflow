"""Packaging contract of the roles, read from the files (no installed environment needed): ``backend/pyproject.toml``
extras, ``backend/uv.lock`` and the installed ``specflow`` metadata.

``test_specflow_role_imports.py`` trusts the installed metadata, which is only as fresh as the last ``uv sync``. These
tests read the source of truth, so widening an extra (say ``sqlalchemy`` into ``mcp``) fails ``make check`` without a
sync, and a stale environment fails loudly.
"""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from role_content_support import ROLE_IMAGE_CONTENT

BACKEND = Path(__file__).resolve().parents[1]
PYPROJECT = BACKEND / "pyproject.toml"
LOCK = BACKEND / "uv.lock"
BASE_DEPENDENCIES = {"pydantic", "pydantic-settings", "pyyaml"}

# extra -> the role whose image content lists the distributions that extra must not declare
ABSENT_FROM_EXTRA = {"api": "api", "worker": "worker-agents", "mcp": "mcp"}


def load_pyproject(path: Path = PYPROJECT) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]


def names(requirements: list[str]) -> set[str]:
    return {canonicalize_name(Requirement(line).name) for line in requirements}


def normal(line: str) -> str:
    """One requirement line, name canonicalized (the installed metadata lowercases names) and marker normalized."""

    requirement = Requirement(line)
    requirement.name = canonicalize_name(requirement.name)
    return str(requirement)


def declared(project: dict) -> list[str]:
    """Normalized ``Requires-Dist`` lines the project metadata declares: base, then each extra with its marker."""

    lines = [normal(line) for line in project["dependencies"]]
    for extra, requirements in project["optional-dependencies"].items():
        lines.extend(normal(f"{line}; extra == '{extra}'") for line in requirements)
    return sorted(lines)


def check_extras(project: dict) -> list[str]:
    """Violations of the extras contract: what the base installs and what each role's extra must not declare."""

    problems = []
    if names(project["dependencies"]) != BASE_DEPENDENCIES:
        problems.append(f"base dependencies are {sorted(names(project['dependencies']))}")
    for extra, role in ABSENT_FROM_EXTRA.items():
        forbidden = {canonicalize_name(name) for name in ROLE_IMAGE_CONTENT[role][2]}
        leaked = names(project["optional-dependencies"][extra]) & forbidden
        if leaked:
            problems.append(f"extra {extra!r} declares {sorted(leaked)}")
    return problems


def test_the_extras_keep_other_roles_libraries_out_of_each_role() -> None:
    assert check_extras(load_pyproject()) == []


def test_widening_an_extra_is_caught_from_the_pyproject_alone(tmp_path: Path) -> None:
    """Negative control on a scratch copy: sqlalchemy in the mcp extra and a fourth base dependency are reported."""

    text = PYPROJECT.read_text(encoding="utf-8")
    widened = text.replace("mcp = [\n", 'mcp = [\n    "sqlalchemy>=2",\n', 1).replace(
        "dependencies = [\n", 'dependencies = [\n    "httpx",\n', 1
    )
    assert widened != text
    scratch = tmp_path / "pyproject.toml"
    scratch.write_text(widened, encoding="utf-8")
    problems = check_extras(load_pyproject(scratch))
    assert any("extra 'mcp' declares ['sqlalchemy']" in p for p in problems), problems
    assert any(p.startswith("base dependencies") for p in problems), problems


def test_the_installed_specflow_metadata_matches_the_pyproject() -> None:
    """A venv that was not synced after a pyproject change is stale: the role import test would trust old extras."""

    project = load_pyproject()
    hint = "the installed specflow metadata is stale: run `make -C backend setup`"
    dist = metadata.distribution("specflow")
    extras = {canonicalize_name(extra) for extra in dist.metadata.get_all("Provides-Extra") or []}
    assert extras == {canonicalize_name(extra) for extra in project["optional-dependencies"]}, hint
    assert sorted(normal(line) for line in dist.requires or []) == declared(project), hint


def lock_packages() -> set[str]:
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    return {canonicalize_name(package["name"]) for package in lock["package"]}


@pytest.mark.parametrize("role", list(ROLE_IMAGE_CONTENT))
def test_every_distribution_named_in_the_role_image_content_is_a_locked_package(role: str) -> None:
    """A mistyped name would make its present/absent check vacuous: each must be a package of ``uv.lock``."""

    _modules, present, absent = ROLE_IMAGE_CONTENT[role]
    unknown = {canonicalize_name(name) for name in (*present, *absent)} - lock_packages()
    assert not unknown, (role, sorted(unknown))
