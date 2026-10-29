"""The project's SQLite engine factory sets the runtime pragmas it promises, on a real
connection to a temporary database file (`sqlite-pragmas-mismatch`). Typical promises:
`journal_mode = "wal"` (readers never block the writer), `foreign_keys = 1` (SQLite does
not enforce foreign keys unless asked) and a `busy_timeout` above zero.

`[sqlite_pragmas].factory` names the factory (`app.database:build_engine`); it is called
with the URL (`url_template`, `{path}` the temporary file) and `factory_kwargs`, and may
return a synchronous `Engine` or an `AsyncEngine`. `[sqlite_pragmas.expected]` maps each
pragma to its expected value; text compares case-insensitively."""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from sqlcheck.config import Config
from sqlcheck.findings import Finding, StepResult
from sqlcheck.loader import resolve

STEP = "sqlite-pragmas"
DOC = "RULES.md#sqlite-pragmas"
_PRAGMA_NAME = re.compile(r"^[a-z_]+$")


def _statement(name: str) -> str:
    if not _PRAGMA_NAME.fullmatch(name):
        msg = f"{name!r} is not a pragma name"
        raise ValueError(msg)
    return f"PRAGMA {name}"


def _read_sync(engine: Engine, names: list[str]) -> dict[str, object]:
    with engine.connect() as conn:
        return {n: conn.exec_driver_sql(_statement(n)).scalar() for n in names}


async def _read_async(engine: AsyncEngine, names: list[str]) -> dict[str, object]:
    async with engine.connect() as conn:
        values: dict[str, object] = {}
        for name in names:
            result = await conn.exec_driver_sql(_statement(name))
            values[name] = result.scalar()
        return values


def read_pragmas(engine: object, names: list[str]) -> dict[str, object]:
    """The current value of each pragma on a fresh connection of `engine`."""
    if isinstance(engine, AsyncEngine):

        async def read_and_dispose() -> dict[str, object]:
            try:
                return await _read_async(engine, names)
            finally:
                await engine.dispose()

        return asyncio.run(read_and_dispose())
    if isinstance(engine, Engine):
        try:
            return _read_sync(engine, names)
        finally:
            engine.dispose()
    msg = f"the engine factory returned a {type(engine).__name__}, not an Engine"
    raise TypeError(msg)


def _same(actual: object, expected: object) -> bool:
    if isinstance(expected, str):
        return str(actual).lower() == expected.lower()
    return actual == expected


def check(
    factory: Any,
    url_template: str,
    kwargs: dict[str, Any],
    expected: dict[str, Any],
    where: str,
) -> list[Finding]:
    with tempfile.TemporaryDirectory(prefix="sqlcheck-pragmas-") as tmp:
        url = url_template.format(path=Path(tmp) / "pragmas.db")
        actual = read_pragmas(factory(url, **kwargs), sorted(expected))
    return [
        Finding(
            where,
            1,
            1,
            "sqlite-pragmas-mismatch",
            f"PRAGMA {name} is {actual[name]!r}, expected {value!r}",
            fix="set the pragma on every new connection (a `connect` event listener)",
            doc=DOC,
        )
        for name, value in sorted(expected.items())
        if not _same(actual[name], value)
    ]


def run(config: Config) -> StepResult:
    if not config.engine_factory:
        return StepResult.ok(STEP, detail="skipped: [sqlite_pragmas].factory is not set")
    if not config.expected_pragmas:
        return StepResult.ok(STEP, detail="skipped: [sqlite_pragmas.expected] is empty")
    findings = check(
        resolve(config.engine_factory),
        config.pragmas_url_template,
        config.factory_kwargs,
        config.expected_pragmas,
        config.config_label(),
    )
    return StepResult.from_findings(STEP, findings)
