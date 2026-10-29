"""No-suppressions enforcement: configuration scoping of a project's `pyproject.toml`.

Every ruff/mypy/pytest scoping entry must appear, with a reason, in the reviewed table
(`--reviewed`, default `<root>/reviewed-suppressions.toml`); a reviewed entry with no
matching key is `no-suppressions-stale-review`. Doc: /README.md#config-scoping"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pystatic.cli import build_parser
from pystatic.files import configure, target_root
from pystatic.harness.core import main as harness_main
from pystatic.harness.models import Finding, StepResult

STEP = "config-scoping"
PYPROJECT_REL = "pyproject.toml"
REVIEWED_NAME = "reviewed-suppressions.toml"
DOC = "/README.md#config-scoping"
FIX = f"drop the ignore or add a reviewed reason to {REVIEWED_NAME}"
STALE = "no-suppressions-stale-review"

_RUFF_GLOBAL_KEYS = ("ignore", "extend-ignore")
_RUFF_PER_FILE_KEYS = ("per-file-ignores", "extend-per-file-ignores")
_RUFF_EXCLUDE_KEYS = ("exclude", "extend-exclude")

# Short local names so each `_flag(...)` call below fits on one line.
_RG, _RF = "no-suppressions-ruff-global-ignore", "no-suppressions-ruff-per-file-ignore"
_MD, _MI = "no-suppressions-mypy-disable-error-code", "no-suppressions-mypy-ignore-missing-imports"
_PW = "no-suppressions-pytest-filterwarnings"
_RX, _MX = "no-suppressions-ruff-exclude", "no-suppressions-mypy-exclude"


@dataclass(frozen=True)
class Reviewed:
    """The reviewed table: each suppression a project accepts, with its reason."""

    ruff_global: dict[str, str] = field(default_factory=dict)
    ruff_per_file: dict[str, set[str]] = field(default_factory=dict)
    ruff_exclude: dict[str, str] = field(default_factory=dict)
    mypy_exclude: dict[str, str] = field(default_factory=dict)
    mypy_disable_error_code: set[str] = field(default_factory=set)
    mypy_ignore_missing_imports: set[str] = field(default_factory=set)
    pytest_filterwarnings: set[str] = field(default_factory=set)


def load_reviewed(path: Path) -> Reviewed:
    """Read the reviewed table; an absent file reviews nothing."""
    if not path.is_file():
        return Reviewed()
    data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    ruff = data.get("ruff", {}) or {}
    mypy = data.get("mypy", {}) or {}
    pytest = data.get("pytest", {}) or {}
    return Reviewed(
        ruff_global=dict(ruff.get("global-ignore", {}) or {}),
        ruff_per_file={g: set(c) for g, c in (ruff.get("per-file-ignores", {}) or {}).items()},
        ruff_exclude=dict(ruff.get("exclude", {}) or {}),
        mypy_exclude=dict(mypy.get("exclude", {}) or {}),
        mypy_disable_error_code=set(mypy.get("disable-error-code", []) or []),
        mypy_ignore_missing_imports=set(mypy.get("ignore-missing-imports", []) or []),
        pytest_filterwarnings=set(pytest.get("filterwarnings", []) or []),
    )


def _f(config_rel: str, rule: str, msg: str) -> Finding:
    return Finding(config_rel, 1, 1, rule, msg, fix=FIX, doc=DOC)


def _flag(
    config_rel: str, values: list[str], allowed: set[str], rule: str, tail: str
) -> list[Finding]:
    """The one shape every format below reduces to: a list of strings read
    from some TOML path, each flagged unless it is in the reviewed set."""
    return [_f(config_rel, rule, f"{v!r} {tail}") for v in values if v not in allowed]


def _stale_review_findings(
    reviewed: Reviewed,
    seen_global: set[str],
    seen_globs: set[str],
    seen_ruff_exclude: set[str],
    seen_mypy_exclude: set[str],
    seen_mypy_codes: set[str],
    seen_mypy_modules: set[str],
    seen_filterwarnings: set[str],
) -> list[Finding]:
    """The table can only shrink to match reality, never grow ahead of it:
    a reviewed entry whose key never showed up in the config today is a
    finding here, not a silent, unverifiable allowance."""
    findings = [
        _f(PYPROJECT_REL, STALE, f"global ignore {c!r} is reviewed but not in the config today")
        for c in reviewed.ruff_global
        if c not in seen_global
    ]
    findings += [
        _f(
            PYPROJECT_REL,
            STALE,
            f"per-file-ignore glob {g!r} is reviewed but not in the config today",
        )
        for g in reviewed.ruff_per_file
        if g not in seen_globs
    ]
    findings += [
        _f(PYPROJECT_REL, STALE, f"ruff exclude {p!r} is reviewed but not in the config today")
        for p in reviewed.ruff_exclude
        if p not in seen_ruff_exclude
    ]
    findings += [
        _f(PYPROJECT_REL, STALE, f"mypy exclude {p!r} is reviewed but not in the config today")
        for p in reviewed.mypy_exclude
        if p not in seen_mypy_exclude
    ]
    findings += [
        _f(PYPROJECT_REL, STALE, f"mypy error code {c!r} is reviewed but not in the config today")
        for c in reviewed.mypy_disable_error_code
        if c not in seen_mypy_codes
    ]
    findings += [
        _f(PYPROJECT_REL, STALE, f"mypy module {m!r} is reviewed but not in the config today")
        for m in reviewed.mypy_ignore_missing_imports
        if m not in seen_mypy_modules
    ]
    findings += [
        _f(PYPROJECT_REL, STALE, f"filterwarnings {w!r} is reviewed but not in the config today")
        for w in reviewed.pytest_filterwarnings
        if w not in seen_filterwarnings
    ]
    return findings


def check(root: Path | None = None, reviewed: Reviewed | None = None) -> list[Finding]:
    base = target_root() if root is None else root
    table = load_reviewed(base / REVIEWED_NAME) if reviewed is None else reviewed
    findings: list[Finding] = []
    seen_global: set[str] = set()
    seen_globs: set[str] = set()
    seen_ruff_exclude: set[str] = set()
    seen_mypy_exclude: set[str] = set()
    seen_mypy_codes: set[str] = set()
    seen_mypy_modules: set[str] = set()
    path = base / PYPROJECT_REL
    if not path.is_file():
        return findings
    tool = tomllib.loads(path.read_text()).get("tool", {})
    ruff = tool.get("ruff", {}) or {}
    for s in (ruff, ruff.get("lint", {}) or {}):
        for k in _RUFF_GLOBAL_KEYS:
            vals = s.get(k, []) or []
            seen_global.update(vals)
            findings += _flag(
                PYPROJECT_REL, vals, set(table.ruff_global), _RG, f"({k}) not reviewed"
            )
        for k in _RUFF_PER_FILE_KEYS:
            for g, codes in (s.get(k, {}) or {}).items():
                seen_globs.add(g)
                allowed = table.ruff_per_file.get(g, set())
                findings += _flag(
                    PYPROJECT_REL, codes, allowed, _RF, f"-> glob {g!r} ({k}) not reviewed"
                )
    # `exclude`/`extend-exclude` are top-level-only ruff settings (never under
    # `[tool.ruff.lint]`): a whole-path exclusion, broader than a per-file-ignore.
    for k in _RUFF_EXCLUDE_KEYS:
        vals = ruff.get(k, []) or []
        seen_ruff_exclude.update(vals)
        findings += _flag(PYPROJECT_REL, vals, set(table.ruff_exclude), _RX, f"({k}) not reviewed")
    mypy = tool.get("mypy", {}) or {}
    for sec in (mypy, *(mypy.get("overrides", []) or [])):
        mods = sec.get("module", [])
        mods = mods if isinstance(mods, list) else ([mods] if mods else [])
        label = ", ".join(mods) or "the whole project"
        if sec.get("ignore_errors"):
            findings.append(
                _f(PYPROJECT_REL, "no-suppressions-mypy-ignore-errors", f"set for {label}")
            )
        codes = sec.get("disable_error_code", []) or []
        seen_mypy_codes.update(codes)
        findings += _flag(
            PYPROJECT_REL, codes, table.mypy_disable_error_code, _MD, f"for {label} not reviewed"
        )
        if sec.get("ignore_missing_imports"):
            seen_mypy_modules.update(mods)
            findings += _flag(
                PYPROJECT_REL, mods, table.mypy_ignore_missing_imports, _MI, "not reviewed"
            )
    # `exclude` is a top-level-only mypy setting (never inside an override):
    # same whole-path-exclusion gap as ruff's above.
    mypy_exclude = mypy.get("exclude", [])
    mypy_exclude = mypy_exclude if isinstance(mypy_exclude, list) else [mypy_exclude]
    seen_mypy_exclude.update(mypy_exclude)
    findings += _flag(PYPROJECT_REL, mypy_exclude, set(table.mypy_exclude), _MX, "not reviewed")
    opts = tool.get("pytest", {}).get("ini_options", {}) or {}
    ignores = [e for e in opts.get("filterwarnings", []) or [] if e.startswith("ignore:")]
    findings += _flag(
        PYPROJECT_REL, ignores, table.pytest_filterwarnings, _PW, "is not in the reviewed table"
    )
    findings += _stale_review_findings(
        table,
        seen_global,
        seen_globs,
        seen_ruff_exclude,
        seen_mypy_exclude,
        seen_mypy_codes,
        seen_mypy_modules,
        set(ignores),
    )
    return findings


def run(root: Path | None = None, reviewed_path: Path | None = None) -> StepResult:
    base = target_root() if root is None else root
    if not (base / PYPROJECT_REL).is_file():
        detail = f"tool-error: {PYPROJECT_REL} not found under {base}"
        return StepResult(step=STEP, exit_code=2, detail=detail)
    reviewed = load_reviewed(reviewed_path or base / REVIEWED_NAME)
    return StepResult.from_findings(STEP, check(base, reviewed))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(STEP)
    parser.add_argument("--reviewed", type=Path, default=None, help="reviewed-suppressions TOML")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    configure(args.root, args.scan)
    return harness_main(STEP, lambda: run(reviewed_path=args.reviewed))


if __name__ == "__main__":
    raise SystemExit(main())
