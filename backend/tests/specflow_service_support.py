"""One definition of "the service code" for the source-scanning tests.

The service is every top-level package under ``backend/src`` except the
libraries (``LIBRARY_PACKAGES``), plus the top-level modules ``cli``,
``health`` and ``settings``. A new service package is scanned automatically;
a new library must be added to ``LIBRARY_PACKAGES``.
"""

from __future__ import annotations

from pathlib import Path

BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
LIBRARY_PACKAGES = frozenset({"llm", "logger", "file_parsers"})
TOP_LEVEL_MODULES = ("cli.py", "health.py", "settings.py")
ANCHORS = ("temporal/agents/activities.py", "db/agent_scope.py", "cli.py")


def service_python_files() -> list[Path]:
    """Sorted ``*.py`` files of the service; fails if a known anchor is missing."""

    files: list[Path] = []
    for package in sorted(BACKEND_SRC.iterdir()):
        if package.is_dir() and package.name not in LIBRARY_PACKAGES and (package / "__init__.py").exists():
            files.extend(p for p in package.rglob("*.py") if "__pycache__" not in p.parts)
    files.extend(p for p in (BACKEND_SRC / n for n in TOP_LEVEL_MODULES) if p.exists())
    files = sorted(files)
    missing = [a for a in ANCHORS if BACKEND_SRC / a not in files]
    assert not missing, f"service scan lost its anchors: {missing}"
    return files
