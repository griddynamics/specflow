"""Role import test: each role's entrypoints import with only that role's libraries installed (no Docker).

The images install one role each (``uv sync --extra ...`` in ``docker/<role>/Dockerfile``), so a module-level import of
a library from another role breaks that image at start. This test reproduces the images in the dev venv, which has
every extra: for each role it takes the requirement closure of ``specflow`` + the role's extras from the installed
metadata (markers evaluated), blocks the import roots of every other installed distribution with a ``sys.meta_path``
finder, and imports the role's entrypoint modules in a fresh interpreter. The finder raises ``ModuleNotFoundError``,
so an optional import that has a fallback (``psutil``, ``rich``, ``sniffio``) still falls back, as in the image.

Single source of the role -> extras map: the ``--extra`` flags of the role's Dockerfile (the same lines the image
contract test in ``test_specflow_images.py`` and the licence gate roles follow). ``base`` has no image: no extras.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from functools import cache
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from role_content_support import ROLE_IMAGE_CONTENT

BACKEND = Path(__file__).resolve().parents[1]
DOCKER = BACKEND / "docker"
SOURCE_DIST = "specflow"
BLOCKED_MARK = "blocked by role check"

# role -> (Dockerfile directory or None for the base install, entrypoint modules imported for that role). The modules of
# the image roles are the ones the image test imports in the built image (role_content_support.py); pinned below.
ROLES: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "base": (None, ("cli", "settings", "auth")),
    **{role: (role, modules) for role, (modules, _present, _absent) in ROLE_IMAGE_CONTENT.items()},
}

# Modules a `.pth` file of the venv imports at interpreter start although their distribution is not a role's: setuptools
# ships `distutils-precedence.pth`, which imports `_distutils_hack`. Nothing else may be loaded before the blocker.
PRELOADED = ("_distutils_hack",)

# Runs in the child interpreter: the blocked import roots arrive as JSON on stdin, the modules as arguments.
CHILD = """
import importlib, json, sys

blocked = tuple(json.load(sys.stdin))


def is_blocked(name):
    return any(name == b or name.startswith(b + ".") for b in blocked)


early = sorted(m for m in sys.modules if is_blocked(m) and not m.startswith(PRELOADED))
if early:
    sys.exit("modules imported before the blocker: " + ", ".join(early))


class Block:
    def find_spec(self, name, path=None, target=None):
        if is_blocked(name):
            raise ModuleNotFoundError("%s: " % MARK + name, name=name)


sys.meta_path.insert(0, Block())
sys.path.insert(0, SRC)
for module in sys.argv[1:]:
    importlib.import_module(module)
print("OK")
"""


def run_instructions(dockerfile: str) -> list[str]:
    """The ``RUN`` instructions of a Dockerfile as single lines: continuation lines joined, ``#`` lines dropped."""

    kept = "\n".join(line for line in dockerfile.splitlines() if not line.lstrip().startswith("#"))
    joined = re.sub(r"\\[ \t]*\n", " ", kept)
    return [line.strip() for line in joined.splitlines() if re.match(r"\s*RUN\s", line, re.IGNORECASE)]


def role_extras(service: str | None) -> list[str]:
    """The extras the role's image installs: every ``--extra`` of its Dockerfile, in order, once."""

    if service is None:
        return []
    runs = run_instructions((DOCKER / service / "Dockerfile").read_text(encoding="utf-8"))
    extras = [extra for run in runs for extra in re.findall(r"--extra (\S+)", run)]
    return list(dict.fromkeys(extras))


def requirement_closure(extras: list[str]) -> set[str]:
    """Names of the installed distributions that ``specflow[extras]`` needs (markers evaluated)."""

    todo: list[tuple[str, str | None]] = [(SOURCE_DIST, extra) for extra in [None, *extras]]
    seen: set[tuple[str, str | None]] = set()
    found: set[str] = set()
    while todo:
        name, extra = todo.pop()
        key = (canonicalize_name(name), extra)
        if key in seen:
            continue
        seen.add(key)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        found.add(key[0])
        todo.extend(_needs(dist, extra))
    return found


def _needs(dist: metadata.Distribution, extra: str | None) -> list[tuple[str, str | None]]:
    needs: list[tuple[str, str | None]] = []
    for line in dist.requires or []:
        req = Requirement(line)
        if req.marker is not None and not req.marker.evaluate({"extra": extra or ""}):
            continue
        needs.extend((req.name, wanted) for wanted in (req.extras or {None}))
        if req.extras:
            needs.append((req.name, None))
    return needs


def import_roots(dist: metadata.Distribution) -> set[str]:
    """Import roots from the RECORD at the first package level (``google.cloud.storage``, ``opentelemetry.sdk``)."""

    files = [
        str(f).split("/")
        for f in dist.files or []
        if str(f).endswith((".py", ".so", ".pyd")) and not str(f).startswith(("..", "__"))
    ]
    packages = {tuple(parts[:-1]) for parts in files if parts[-1] == "__init__.py"}
    roots: set[str] = set()
    for parts in files:
        top = next((parts[:i] for i in range(1, len(parts)) if tuple(parts[:i]) in packages), None)
        roots.add(".".join(top) if top else ".".join([*parts[:-1], parts[-1].split(".")[0]]))
    return {root for root in roots if not any(root.startswith(other + ".") for other in roots)}


@cache
def distribution_roots() -> dict[str, frozenset[str]]:
    return {
        canonicalize_name(dist.metadata["Name"]): frozenset(import_roots(dist))
        for dist in metadata.distributions()
        if dist.metadata["Name"]
    }


def blocked_roots(extras: list[str]) -> list[str]:
    """Import roots of installed distributions outside ``specflow`` + ``extras``, less what an allowed one owns."""

    allowed = requirement_closure(extras)
    roots = distribution_roots()
    allowed_roots = {root for name in allowed for root in roots.get(name, ())}
    outside = {root for name, owned in roots.items() if name not in allowed for root in owned}
    blocked = {
        root
        for root in outside - allowed_roots
        if not any(owned.startswith(root + ".") or root.startswith(owned + ".") for owned in allowed_roots)
    }
    return sorted(blocked)


def import_under_block(extras: list[str], modules: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    code = f"MARK = {BLOCKED_MARK!r}\nPRELOADED = {PRELOADED!r}\nSRC = {str(BACKEND / 'src')!r}\n{CHILD}"
    return subprocess.run(
        [sys.executable, "-c", code, *modules],
        input=json.dumps(blocked_roots(extras)),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.mark.parametrize("role", list(ROLES))
def test_the_role_entrypoints_import_with_only_the_roles_extras_installed(role: str) -> None:
    service, modules = ROLES[role]
    extras = role_extras(service)
    result = import_under_block(extras, modules)
    assert result.returncode == 0 and result.stdout.strip() == "OK", (role, extras, result.stderr[-800:])


@pytest.mark.parametrize(
    ("service", "given", "modules", "missing"),
    [
        ("mcp", [], ("mcp_server.server",), "mcp"),  # the mcp role without its extra
        ("api", [], ("api.server",), "fastapi"),  # the api role with only the base install
        ("worker-agents", [], ("temporal.worker",), "temporalio"),
    ],
)
def test_withholding_a_roles_extra_fails_the_import_so_the_check_can_fail(
    service: str, given: list[str], modules: tuple[str, ...], missing: str
) -> None:
    """Negative control: the blocker really blocks, and the role needs the extras its Dockerfile installs."""

    assert role_extras(service), service  # the real role has extras; the control withholds them
    result = import_under_block(given, modules)
    assert result.returncode != 0
    assert BLOCKED_MARK in result.stderr, result.stderr[-800:]
    assert missing in blocked_roots(given)


def test_the_role_extras_come_from_the_dockerfiles_and_cover_every_image() -> None:
    assert role_extras("mcp") == ["mcp"]
    assert role_extras("worker-parse") == ["worker", "parse", "gcp", "aws"]
    assert {service for service, _ in ROLES.values() if service} == {p.parent.name for p in DOCKER.glob("*/Dockerfile")}


def test_the_run_parser_reads_run_lines_with_continuations_and_ignores_comments() -> None:
    dockerfile = (
        "# RUN uv sync --extra comment-only\n"
        "FROM python:3.12\n"
        "ENV NOTE='--extra not-a-run'\n"
        "RUN pip install uv \\\n"
        "    # --extra in a comment between continuation lines\n"
        "    && uv sync --extra api \\\n"
        "       --extra gcp\n"
        "  run uv sync --extra aws\n"
        'CMD ["x", "--extra", "cmd"]\n'
    )
    runs = run_instructions(dockerfile)
    assert [" ".join(run.split()) for run in runs] == [
        "RUN pip install uv && uv sync --extra api --extra gcp",
        "run uv sync --extra aws",
    ]
    assert [e for run in runs for e in re.findall(r"--extra (\S+)", run)] == ["api", "gcp", "aws"]


def test_the_child_refuses_to_run_when_a_blocked_module_is_already_imported() -> None:
    """The finder only works on imports that happen after it: a pre-imported blocked root must fail loudly."""

    code = f"import json, sys\nsys.modules['json'] = json\nMARK = ''\nPRELOADED = ()\nSRC = ''\n{CHILD}"
    result = subprocess.run(
        [sys.executable, "-c", code], input='["json"]', capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode != 0
    assert "modules imported before the blocker: json" in result.stderr, result.stderr[-400:]


# role -> the module its CLI command runs (src/cli.py), as the entrypoints each image must import
CLI_TARGETS = {
    "api": ("api.server", "db.migrate"),
    "worker-agents": ("temporal.worker",),
    "worker-parse": ("temporal.worker",),
    "mcp": ("mcp_server.server",),
}


def cli_modules() -> dict[str, str]:
    """The ``_*_MODULE`` constants of ``src/cli.py`` (``_API_MODULE: Final = "api.server"``), read without importing."""

    tree = ast.parse((BACKEND / "src" / "cli.py").read_text(encoding="utf-8"))
    return {
        node.target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id.endswith("_MODULE")
        and isinstance(node.value, ast.Constant)
    }


def test_the_role_entrypoints_cover_every_module_the_cli_runs() -> None:
    constants = cli_modules()
    assert set(constants) == {"_API_MODULE", "_WORKER_MODULE", "_MCP_MODULE", "_MIGRATE_MODULE", "_HEALTH_MODULE"}
    covered = {module for _service, modules in ROLES.values() for module in modules}
    assert set(constants.values()) <= covered, set(constants.values()) - covered
    for role, targets in CLI_TARGETS.items():
        assert set(targets) <= set(ROLES[role][1]), (role, targets)


def test_the_role_entrypoint_lists_are_pinned() -> None:
    """Dropping an entrypoint from a role (so its import goes unchecked) must turn this red."""

    assert {role: modules for role, (_service, modules) in ROLES.items()} == {
        "base": ("cli", "settings", "auth"),
        "api": ("api.server", "api.app", "db.migrate", "health"),
        "worker-agents": ("temporal.agents.registration", "temporal.worker", "health"),
        "worker-parse": ("temporal.documents.registration", "temporal.worker", "health"),
        "mcp": ("mcp_server.server", "mcp_server.app"),
    }
