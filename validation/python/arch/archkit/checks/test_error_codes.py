"""Every handled error answers a documented `(code, status)` pair, and every pair is reachable.

`[error_codes]`:
- `codes` (or `codes_file`): the documented catalogue, rows `{code = "...", http = 404}`;
  build it from the project's own document with `archkit build-catalogue`.
- `probes`: one row per exception the app handles, `{class = "pkg.mod:Error", args = [...],
  kwargs = {...}, name = "..."}`; each is raised from a throwaway route on a fresh app.
- `ignore_handlers`: handled classes no HTTP request can raise (a websocket-only one).

The handled classes come from `app.exception_handlers`, so a new handler without a probe
fails the coverage check instead of escaping it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from archkit.analysis import uncovered_handler_classes
from archkit.config import Config, load_records, resolve
from archkit.support import envelope_code, section

RULE = "error_codes"


def _probes(table: dict[str, Any]) -> dict[str, BaseException]:
    probes: dict[str, BaseException] = {}
    for spec in table["probes"]:
        exception_class = resolve(spec["class"])
        name = spec.get("name", spec["class"])
        probes[name] = exception_class(*spec.get("args", []), **spec.get("kwargs", {}))
    return probes


def _raiser(exc: BaseException) -> Callable[[], Awaitable[None]]:
    """A zero-argument route handler raising `exc` (an exception is not a valid parameter)."""

    async def _raise_it() -> None:
        raise exc

    return _raise_it


async def test_every_handled_error_answers_a_documented_code(
    archkit_config: Config,
    archkit_boom: Callable[[Callable[[], Awaitable[None]]], Awaitable[httpx.Response]],
) -> None:
    table = section(archkit_config, RULE)
    rows = load_records(archkit_config, table, "codes")
    documented = {(str(row["code"]), int(row["http"])) for row in rows}
    assert documented, f"[{RULE}] documents no error code"

    produced: set[tuple[str, int]] = set()
    undocumented: dict[str, dict[str, Any]] = {}
    for name, exc in _probes(table).items():
        response = await archkit_boom(_raiser(exc))
        code = envelope_code(archkit_config, response)
        shown = str(code) if code is not None else f"<no envelope: {response.text[:80]!r}>"
        pair = (shown, response.status_code)
        produced.add(pair)
        if pair not in documented:
            undocumented[name] = {"status": pair[1], "code": pair[0]}
    assert not undocumented, (
        f"these errors answer an undocumented (code, status) pair or shape: {undocumented}"
    )
    missing = sorted(documented - produced)
    assert not missing, (
        "documented (code, status) pairs no probe produces (add a probe, or drop the row): "
        f"{missing}"
    )


def test_every_handled_exception_class_has_a_probe(
    archkit_config: Config, archkit_app: Any
) -> None:
    table = section(archkit_config, RULE)
    ignored = [resolve(reference) for reference in table.get("ignore_handlers", [])]
    probe_types = [type(exc) for exc in _probes(table).values()]
    uncovered = uncovered_handler_classes(archkit_app.exception_handlers, probe_types, ignored)
    assert not uncovered, (
        f"exception classes with a handler on the app but no [{RULE}] probe: {uncovered}"
    )
