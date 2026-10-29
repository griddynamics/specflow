"""Adoption/gate tests for the central-logger rollout (Wave 5 target state).

Source: the central-logger test catalog §3 (INT-21,
22, 23, 24, 25, 26, 34) and §5 (the resolved INT-21/INT-22 conflict — the
tracing-only test below closes that gap explicitly, not just implicitly via
the general src/ scan).

Every assertion here describes the state Wave 5 (`w5-*` plan steps) is
supposed to reach, not today's `src/`. Right now only
`src/file_parsers/pdf/parser.py` still uses ad-hoc `logging.getLogger` /
`import logging` / `logging.basicConfig`, so INT-21/INT-22 (and the
tracing-only extension) are expected to fail for that one real violation.
INT-23/24/25/26 currently have nothing to violate them (no `log_event` /
`extra=` call exists anywhere yet, and every `print(` in `src/` already
lives inside its own module's `if __name__ == "__main__":` guard) — those
pass today, but *vacuously*: the assertions are written against the Wave-5
contract, not tuned to today's absence of logging. INT-34 fails for a real
reason: `coding-tasks/graph-driven-coding/task.py` does not yet wire
`extra_sinks=(LoggingSink(),)` / `new_request_context()` / `close()`.

Scope: INT-21/22/23 are scanned over `src/` only, matching the FD wording
they cite (`rg "getLogger" src`, "no print( in src"). INT-24/25/26 are
scanned over `src/`, `examples/`, and `coding-tasks/`, since `log_event`
call sites can land in any of the three once Wave 5's entrypoint wiring
(§4.4) is in place.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
SRC_DIR = REPO_ROOT / "src"
LOGGER_DIR = SRC_DIR / "logger"
TRACING_DIR = SRC_DIR / "llm" / "tracing"
EXAMPLES_DIR = REPO_ROOT / "examples"
CODING_TASKS_DIR = WORKSPACE_ROOT / "coding-tasks"

_GETLOGGER_RE = re.compile(r"\bgetLogger\b")
_BASICCONFIG_RE = re.compile(r"\bbasicConfig\b")
_IMPORT_LOGGING_RE = re.compile(r"^\s*import\s+logging(\s|\.|$)", re.MULTILINE)
_FROM_LOGGING_IMPORT_RE = re.compile(r"^\s*from\s+logging\s+import\b", re.MULTILINE)

_SECRET_FIELD_RE = re.compile(
    r"(?i)(^|_)(api_?key|(secret|public|private)_?key|secret|password"
    r"|access_token|auth_token|token)$"
)
_EVENT_NAME_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
_LOGGER_METHOD_NAMES = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})
_RESERVED_LOG_EVENT_KWARGS = frozenset({"message", "exc_info", "event"})


def _python_files(*roots: Path) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        files.extend(path for path in sorted(root.rglob("*.py")) if "__pycache__" not in path.parts)
    return files


def _src_files_outside_logger() -> list[Path]:
    return [path for path in _python_files(SRC_DIR) if LOGGER_DIR not in path.parents]


def _display(path: Path) -> str:
    return str(path.relative_to(WORKSPACE_ROOT))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# --------------------------------------------------------------------------
# INT-21 — `getLogger` only under `src/logger/`.
# --------------------------------------------------------------------------


def test_getlogger_is_never_used_outside_src_logger() -> None:
    """strategy.md:118 (narrowed per qa/tests.md §5): `rg "getLogger" src`
    must show nothing outside `src/logger/`."""

    offenders = [
        _display(path) for path in _src_files_outside_logger() if _GETLOGGER_RE.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


# --------------------------------------------------------------------------
# INT-22 — no `import logging` / `from logging import ...` / `basicConfig`
# outside `src/logger/`.
# --------------------------------------------------------------------------


def test_stdlib_logging_is_never_imported_outside_src_logger() -> None:
    """Logging rule 1: import only from the central module."""

    offenders = []
    for path in _src_files_outside_logger():
        text = path.read_text(encoding="utf-8")
        if _IMPORT_LOGGING_RE.search(text) or _FROM_LOGGING_IMPORT_RE.search(text):
            offenders.append(_display(path))

    assert offenders == []


def test_basicconfig_is_never_called_outside_src_logger() -> None:
    """SPEC.md:33 / ops.md: bootstrap owns handler/formatter setup; nothing
    else may call `logging.basicConfig`."""

    offenders = [
        _display(path)
        for path in _src_files_outside_logger()
        if _BASICCONFIG_RE.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


# --------------------------------------------------------------------------
# INT-21 (+INT-22) conflict resolution, qa/tests.md §5: the narrower
# `src/logger/`-only allowlist is safe specifically because `src/llm/
# tracing/` never calls the literal `logging.getLogger` / `import logging` /
# `logging.basicConfig` either (it only ever calls the `get_logger`
# factory, once Wave 3's `w3-b` lands it) — asserted here directly, per the
# "action taken" note in §5, not just implied by the repo-wide scan above.
# --------------------------------------------------------------------------


def test_tracing_package_never_touches_stdlib_logging_directly() -> None:
    """qa/tests.md §5: the one thing that makes INT-21's narrower rule a
    safe subset of the wider FD wording (`getLogger` allowed under
    `src/logger/` *and* `src/llm/tracing/`) is that `src/llm/tracing/`
    itself never actually needs the wider allowance."""

    offenders = []
    for path in _python_files(TRACING_DIR):
        text = path.read_text(encoding="utf-8")
        hits = [
            name
            for name, pattern in (
                ("getLogger", _GETLOGGER_RE),
                ("import logging", _IMPORT_LOGGING_RE),
                ("from logging import", _FROM_LOGGING_IMPORT_RE),
                ("basicConfig", _BASICCONFIG_RE),
            )
            if pattern.search(text)
        ]
        if hits:
            offenders.append(f"{_display(path)}: {', '.join(hits)}")

    assert offenders == []


# --------------------------------------------------------------------------
# INT-23 — `print(` in `src/` only inside `if __name__ == "__main__":`.
# Deliberately AST-based: a text/regex scan for the substring `print(` would
# false-positive on a function such as `def fingerprint(`, since that
# literally contains the substring "print(".
# --------------------------------------------------------------------------


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


class _PrintOutsideMainGuardVisitor(ast.NodeVisitor):
    """Walks the AST, tracking whether we're lexically inside a top-level
    `if __name__ == "__main__":` block, and records every `print(...)`
    *call* (never a same-named function/attribute, e.g. `fingerprint(...)`)
    found outside it."""

    def __init__(self) -> None:
        self.offenders: list[int] = []
        self._main_guard_depth = 0

    def visit_If(self, node: ast.If) -> None:
        entered = _is_main_guard(node)
        if entered:
            self._main_guard_depth += 1
        self.generic_visit(node)
        if entered:
            self._main_guard_depth -= 1

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "print" and self._main_guard_depth == 0:
            self.offenders.append(node.lineno)
        self.generic_visit(node)


def _print_offenders(path: Path) -> list[int]:
    visitor = _PrintOutsideMainGuardVisitor()
    visitor.visit(_parse(path))
    return visitor.offenders


def test_print_is_only_called_inside_the_main_guard_in_src() -> None:
    """strategy.md:119. AST-based — see the two regression guards below for
    why a text/regex scan is unsafe for this specific rule."""

    offenders = [f"{_display(path)}:{lineno}" for path in _python_files(SRC_DIR) for lineno in _print_offenders(path)]

    assert offenders == []


def test_print_scanner_does_not_false_positive_on_fingerprint_like_names() -> None:
    """Regression guard for the exact trap this rule must avoid: a bare text
    search for the substring `print(` would flag `def fingerprint(` and any
    call to it. The AST visitor must not, because `fingerprint` and `print`
    are different identifiers, not overlapping substrings, once parsed."""

    source = (
        "def fingerprint():\n    return _fingerprint_helper()\n\n\ndef _fingerprint_helper() -> str:\n    return 'x'\n"
    )
    visitor = _PrintOutsideMainGuardVisitor()
    visitor.visit(ast.parse(source))

    assert visitor.offenders == []


# --------------------------------------------------------------------------
# INT-24 — no `extra=` on any logger call in src/examples/coding-tasks
# (except inside `src/logger/`, which owns the stdlib logging call sites).
# --------------------------------------------------------------------------


def _extra_kwarg_offenders(path: Path) -> list[int]:
    offenders: list[int] = []
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        if name not in _LOGGER_METHOD_NAMES and name != "log_event":
            continue
        if any(keyword.arg == "extra" for keyword in node.keywords):
            offenders.append(node.lineno)
    return offenders


def test_no_extra_kwarg_on_logger_calls() -> None:
    """StrFormatter.md §1: correlation/structured fields never go through
    stdlib's reserved `extra=` kwarg (it KeyErrors on collision); central
    logger calls pass fields as plain keyword arguments to `log_event`
    instead, and correlation ids are injected by the filter automatically."""

    roots = (SRC_DIR, EXAMPLES_DIR, CODING_TASKS_DIR)
    offenders = [
        f"{_display(path)}:{lineno}"
        for path in _python_files(*roots)
        if LOGGER_DIR not in path.parents
        for lineno in _extra_kwarg_offenders(path)
    ]

    assert offenders == []


# --------------------------------------------------------------------------
# INT-25 — no `log_event` field name that looks like a secret/token.
# --------------------------------------------------------------------------


def _log_event_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
        if name == "log_event":
            calls.append(node)
    return calls


def _field_names(call: ast.Call) -> list[str]:
    return [
        keyword.arg
        for keyword in call.keywords
        if keyword.arg is not None and keyword.arg not in _RESERVED_LOG_EVENT_KWARGS
    ]


def test_log_event_field_names_never_look_like_secrets() -> None:
    """strategy.md:99-102,106-107: the regex specifically catches
    `langfuse_secret_key` / `langfuse_public_key`-shaped names (the
    Langfuse public-key leak called out at review) as well as bare
    `secret` / `password` / `*_token` / `token`, while still allowing token
    *counts* like `usage_input_tokens` (the trailing `|token)$` branch
    requires the field to END in `token`, so `..._tokens` is unaffected)."""

    roots = (SRC_DIR, EXAMPLES_DIR, CODING_TASKS_DIR)
    offenders = []
    for path in _python_files(*roots):
        if LOGGER_DIR in path.parents:
            continue
        for call in _log_event_calls(_parse(path)):
            offenders.extend(
                f"{_display(path)}:{call.lineno}:{field_name}"
                for field_name in _field_names(call)
                if _SECRET_FIELD_RE.search(field_name)
            )

    assert offenders == []


@pytest.mark.parametrize(
    ("field_name", "should_match"),
    [
        ("langfuse_secret_key", True),
        ("langfuse_public_key", True),
        ("secret_key", True),
        ("public_key", True),
        ("private_key", True),
        ("secret", True),
        ("password", True),
        ("access_token", True),
        ("auth_token", True),
        ("token", True),
        ("refresh_token", True),
        ("bearer_token", True),
        ("api_key", True),
        ("apikey", True),
        ("usage_input_tokens", False),
        ("usage_output_tokens", False),
        ("tokens", False),
        ("duration_ms", False),
        ("run_id", False),
    ],
)
def test_secret_field_regex_matches_the_documented_examples(field_name: str, *, should_match: bool) -> None:
    """Pins down the exact regex from qa/tests.md's INT-25 row against its
    own documented examples, independent of any real `log_event` call site
    existing yet — this passes today regardless of adoption progress."""

    assert bool(_SECRET_FIELD_RE.search(field_name)) is should_match


# --------------------------------------------------------------------------
# INT-26 — literal `log_event` event names match `<area>.<object>.<verb>`.
# --------------------------------------------------------------------------


def _event_literal(call: ast.Call) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == "event" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    if len(call.args) >= 3 and isinstance(call.args[2], ast.Constant) and isinstance(call.args[2].value, str):
        return call.args[2].value
    return None


def test_log_event_literal_event_names_follow_the_naming_convention() -> None:
    """strategy.md:60: `^[a-z0-9_]+(\\.[a-z0-9_]+)+$`."""

    roots = (SRC_DIR, EXAMPLES_DIR, CODING_TASKS_DIR)
    offenders = []
    for path in _python_files(*roots):
        if LOGGER_DIR in path.parents:
            continue
        for call in _log_event_calls(_parse(path)):
            literal = _event_literal(call)
            if literal is None:
                continue
            if not _EVENT_NAME_RE.match(literal):
                offenders.append(f"{_display(path)}:{call.lineno}:{literal!r}")

    assert offenders == []


def test_event_name_regex_matches_the_documented_examples() -> None:
    """Pins down the naming-convention regex itself, independent of any
    real `log_event` call site existing yet."""

    assert _EVENT_NAME_RE.match("pdf.stage.start")
    assert _EVENT_NAME_RE.match("graph.ingest.done")
    assert not _EVENT_NAME_RE.match("PdfStageStart")
    assert not _EVENT_NAME_RE.match("no_dots_at_all")
    assert not _EVENT_NAME_RE.match("trailing.dot.")


# --------------------------------------------------------------------------
# INT-34 — coding-tasks/*/task.py wires extra_sinks=(LoggingSink(),) and
# wraps its run in new_request_context() + close(). AST-based per the
# catalog (Parsing.md:122-123).
# --------------------------------------------------------------------------


def _has_logging_sink_extra_sink(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "extra_sinks" or not isinstance(keyword.value, (ast.Tuple, ast.List)):
                continue
            for element in keyword.value.elts:
                if not isinstance(element, ast.Call):
                    continue
                func = element.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name == "LoggingSink":
                    return True
    return False


def _uses_new_request_context(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            expr = item.context_expr
            if not isinstance(expr, ast.Call):
                continue
            func = expr.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "new_request_context":
                return True
    return False


def _calls_bare_close(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "close"
        for node in ast.walk(tree)
    )


def test_coding_task_wires_logging_sink_and_request_context() -> None:
    """Parsing.md:122-123: coding-tasks/graph-driven-coding/task.py must
    pass `extra_sinks=(LoggingSink(),)` to its trace observer and wrap its
    run in `with new_request_context(): ... finally: close()`, matching the
    entrypoint wiring every examples/*.py file gets."""

    # coding-tasks/ is untracked, so a fresh clone has no task.py files: the loop is
    # then empty. That is an environment difference, not a logging-adoption regression.
    task_files = sorted(CODING_TASKS_DIR.glob("*/task.py"))

    for path in task_files:
        tree = _parse(path)
        assert _has_logging_sink_extra_sink(tree), (
            f"{_display(path)}: missing extra_sinks=(LoggingSink(),) on its trace observer call"
        )
        assert _uses_new_request_context(tree), (
            f"{_display(path)}: missing `with new_request_context():` around its run"
        )
        assert _calls_bare_close(tree), f"{_display(path)}: missing a bare close() call (finally block)"
