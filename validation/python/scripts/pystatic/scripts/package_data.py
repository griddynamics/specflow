"""The built wheel contains every data file under the package directory and no test support.
Reads `[package-data]` (`package-dir`, `data-suffixes`). Doc: /README.md#package-data"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from pystatic import config
from pystatic.cli import main as harness_main
from pystatic.files import repo_rel, target_root
from pystatic.harness import Finding, StepResult

STEP = "package-data"
DOC = "/README.md#package-data"
TEST_SUPPORT_DOC = "/README.md#package-data"
#: The module names test support takes; production code takes none of them.
_TEST_SUPPORT_NAME = re.compile(r"(?:test_.*|conftest|fixtures|fixture_.*)\Z")

_PACKAGED_EXTS = (".cypher", ".sql")
_PACKAGE_DIR = "src"


def _settings() -> tuple[str, tuple[str, ...]]:
    table = config.section(STEP)
    package_dir = table.get("package-dir", _PACKAGE_DIR)
    if not isinstance(package_dir, str):
        msg = "`package-dir` must be a string"
        raise config.ConfigError(msg)
    return package_dir, tuple(config.string_list(table, "data-suffixes", list(_PACKAGED_EXTS)))


def _expected_files(root: Path) -> list[Path]:
    package_dir, suffixes = _settings()
    src = root / package_dir
    if not src.is_dir():
        return []
    files = [p for ext in suffixes for p in src.rglob(f"*{ext}")]
    return sorted(set(files))


def _wheel_members(root: Path) -> list[str] | None:
    """Every path inside the wheel `uv build --wheel` produces from `root`,
    or `None` if the build tool is missing or the build fails (a tool-error
    StepResult, never a fabricated finding)."""
    uv = shutil.which("uv")
    if uv is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [uv, "build", "--wheel", "--no-build-isolation", "-o", tmp],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        wheels = list(Path(tmp).glob("*.whl"))
        if not wheels:
            return None
        with zipfile.ZipFile(wheels[0]) as archive:
            return archive.namelist()


def _check_at(root: Path) -> list[Finding]:
    expected = _expected_files(root)
    if not expected:
        return []
    findings: list[Finding] = []
    members = _wheel_members(root)
    if members is None:
        return [
            Finding(
                repo_rel(root / "pyproject.toml"),
                1,
                1,
                "package-data-build-failed",
                "uv build --wheel did not produce a wheel",
                fix="run `uv build --wheel` and fix whatever it reports",
                doc=DOC,
            )
        ]
    member_set = set(members)
    for path in expected:
        wheel_path = path.relative_to(root).as_posix()
        in_package = path.relative_to(root / _settings()[0]).as_posix()
        if wheel_path not in member_set and in_package not in member_set:
            findings.append(
                Finding(
                    repo_rel(path),
                    1,
                    1,
                    "package-data-missing-from-wheel",
                    f"{wheel_path} is not in the built wheel",
                    fix="check the build backend's file selection and the file's location",
                    doc=DOC,
                )
            )
    return sorted(findings, key=lambda f: (f.path, f.line, f.col))


def _modules(src: Path) -> list[Path]:
    """Every module of the package: its `.py` files and the directories holding one."""
    files = [path for path in src.rglob("*.py") if "__pycache__" not in path.parts]
    return sorted({*files, *(path.parent for path in files if path.parent != src)})


def _test_support_at(root: Path) -> list[Finding]:
    src = root / _settings()[0]
    if not src.is_dir():
        return []
    return [
        Finding(
            repo_rel(path),
            1,
            1,
            "package-data-test-support",
            f"{path.relative_to(root).as_posix()} is test support inside the production package",
            fix="move it under tests/ (test support) or the check harness",
            doc=TEST_SUPPORT_DOC,
        )
        for path in _modules(src)
        if _TEST_SUPPORT_NAME.match(path.stem if path.is_file() else path.name)
    ]


def check() -> list[Finding]:
    return sorted(
        [*_check_at(target_root()), *_test_support_at(target_root())],
        key=lambda f: (f.path, f.line, f.col),
    )


def run() -> StepResult:
    return StepResult.from_findings(STEP, check())


def main() -> int:
    return harness_main(STEP, run)


if __name__ == "__main__":
    raise SystemExit(main())
