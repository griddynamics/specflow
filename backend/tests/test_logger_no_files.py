"""The central logger never writes to the filesystem (AC-1, AC-2).

stdout is the only sink, one JSON object per line; the platform collects it
(Cloud Logging / Grafana). These tests guard that statically (no file API in
`src/logger/`) and at runtime (a read-only cwd and `HOME` stay empty).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

LOGGER_DIR = Path(__file__).resolve().parents[1] / "src" / "logger"

#: Method names whose call would open, create, change or remove a file
#: (`Path.open`/`io.open`/`os.open`, `os.remove`, `Path.replace`, ...).
#: Flagged on ANY receiver: nothing in `src/logger` calls them today. A
#: future legitimate collision (e.g. `str.replace`) must be allow-listed by
#: its exact `file:line` here, with a comment, rather than loosening the rule.
_FILE_METHODS = frozenset(
    {
        "open",
        "mkdir",
        "remove",
        "replace",
        "rename",
        "rmdir",
        "unlink",
        "write_text",
        "write_bytes",
        "touch",
    }
)

#: Modules whose import alone means filesystem work is intended.
_FILE_MODULES = frozenset({"shutil", "tempfile"})


def _call_uses(path: Path, node: ast.Call) -> list[str]:
    """Filesystem-writing calls: `open()`, write-style methods, `os.makedirs()`."""

    func = node.func
    found: list[str] = []
    if isinstance(func, ast.Name) and func.id == "open":
        found.append(f"{path.name}:{node.lineno} open()")
    if isinstance(func, ast.Attribute) and func.attr in _FILE_METHODS:
        found.append(f"{path.name}:{node.lineno} .{func.attr}()")
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "makedirs"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    ):
        found.append(f"{path.name}:{node.lineno} os.makedirs()")
    return found


def _import_uses(path: Path, node: ast.AST) -> list[str]:
    """Imports of modules whose presence means filesystem work is intended."""

    if isinstance(node, ast.Import):
        return [
            f"{path.name}:{node.lineno} import {alias.name}"
            for alias in node.names
            if alias.name.split(".")[0] in _FILE_MODULES
        ]
    if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in _FILE_MODULES:
        return [f"{path.name}:{node.lineno} import {node.module}"]
    return []


def _file_handler_name(node: ast.AST) -> str | None:
    """The `FileHandler`-style name a node refers to or defines, if any."""

    if isinstance(node, ast.Name):
        name: str | None = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    elif isinstance(node, ast.ClassDef):
        name = node.name
    else:
        name = None
    if name is not None and "FileHandler" in name:
        return name
    return None


def _file_api_uses(path: Path) -> list[str]:
    """Return `file:line name` for every filesystem-writing call or name."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            found.extend(_call_uses(path, node))
        found.extend(_import_uses(path, node))
        handler_name = _file_handler_name(node)
        if handler_name is not None:
            found.append(f"{path.name}:{node.lineno} {handler_name}")
    return found


# AC-1
def test_logger_package_has_no_file_sink_or_file_api() -> None:
    sources = sorted(LOGGER_DIR.glob("*.py"))
    assert sources, f"no sources under {LOGGER_DIR}"
    assert not (LOGGER_DIR / "retention.py").exists()

    uses = [use for path in sources for use in _file_api_uses(path)]
    assert uses == []


def test_the_file_api_scan_catches_what_it_guards(tmp_path: Path) -> None:
    """Self-check: the AC-1 scan really flags each banned call, each
    `shutil`/`tempfile` import and any name containing `FileHandler`
    (defined, referenced or used as a base)."""

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import logging, os, shutil\n"
        "from tempfile import mkdtemp\n"
        "open('x')\n"
        "p.mkdir()\n"
        "os.makedirs('y')\n"
        "q.rename('z')\n"
        "Path('a').open('w')\n"
        "os.remove('b')\n"
        "q.replace('c')\n"
        "q.rmdir()\n"
        "q.unlink()\n"
        "q.write_text('d')\n"
        "q.write_bytes(b'e')\n"
        "q.touch()\n"
        "class DailyFileHandler(logging.FileHandler):\n"
        "    pass\n"
        "h = DailyFileHandler()\n",
        encoding="utf-8",
    )

    uses = _file_api_uses(probe)

    assert {use.split(" ", 1)[1] for use in uses} == {
        "import shutil",
        "import tempfile",
        "open()",
        ".open()",
        ".mkdir()",
        "os.makedirs()",
        ".rename()",
        ".remove()",
        ".replace()",
        ".rmdir()",
        ".unlink()",
        ".write_text()",
        ".write_bytes()",
        ".touch()",
        "DailyFileHandler",
        "FileHandler",
    }
    assert sum("DailyFileHandler" in use for use in uses) == 2


# AC-2
def test_logging_in_a_read_only_cwd_and_home_writes_nothing(tmp_path: Path) -> None:
    read_only = tmp_path / "read-only"
    read_only.mkdir()
    script = (
        "from logger.logger import INFO, get_logger, log_event\n"
        "log = get_logger('logger-tests.no_files')\n"
        "for i in range(10):\n"
        "    log_event(log, INFO, 'test.no_files', i=i)\n"
        # Fall off the end: normal shutdown drains through the atexit hook.
    )
    env = os.environ.copy()
    env.update({"HOME": str(read_only), "LOGS_LEVEL": "INFO", "LOGS_STDOUT_LEVEL": "INFO"})

    read_only.chmod(0o555)
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=read_only,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        leftovers = sorted(p.name for p in read_only.iterdir())
    finally:
        read_only.chmod(0o755)

    assert result.returncode == 0, result.stderr
    assert leftovers == []
    assert "[central-logger]" not in result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert [record["i"] for record in records if record["event"] == "test.no_files"] == list(range(10))
    assert all(record["severity"] == "INFO" for record in records)
