"""Fixtures for semgrep/errors.yml: positive and negative cases per rule.

Not named `src/errors.py` or `src/main.py`, and `--test` ignores `paths:`, so each case
exercises the pattern itself."""

from fastapi import HTTPException
from fastapi.responses import JSONResponse

logger = get_logger()


def get_logger() -> object: ...


# --- kit.errors.http-exception-outside-handlers ---


def get_order(order_id: str) -> dict[str, str]:
    if order_id == "":
        # ruleid: kit.errors.http-exception-outside-handlers
        raise HTTPException(status_code=404, detail="not found")
    return {"id": order_id}


# --- kit.errors.envelope-outside-handlers ---


def reject_order(order_id: str) -> object:
    # ruleid: kit.errors.envelope-outside-handlers
    return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND"}})


def build_error_body(code: str) -> dict[str, object]:
    # ruleid: kit.errors.envelope-outside-handlers
    return {"error": {"code": code}}


def get_order_ok(order_id: str) -> object:
    # ok: kit.errors.envelope-outside-handlers
    return JSONResponse(status_code=200, content={"id": order_id})


# --- kit.errors.no-builtin-raise ---


def validate_scope(scope: str) -> None:
    if not scope:
        # ruleid: kit.errors.no-builtin-raise
        raise ValueError("scope is required")


def load_config(key: str, table: dict[str, str]) -> str:
    if key not in table:
        # ruleid: kit.errors.no-builtin-raise
        raise KeyError(key)
    return table[key]


def validate_scope_ok(scope: str) -> None:
    if not scope:
        # ok: kit.errors.no-builtin-raise
        raise ValidationError("scope is required")


class ValidationError(Exception): ...


# --- kit.errors.lifespan-swallowed-failure ---

# Swallowed broad excepts are ruff's (BLE001, E722, S110, TRY203), not a semgrep rule.

# the `async def lifespan($APP): ... try: ...` pattern starts at the `async
# def` line itself, so the marker sits there, not on the try/except lines


# ruleid: kit.errors.lifespan-swallowed-failure
async def lifespan(app: object):
    ready = False
    # the except body neither raises nor logs -- ruff BLE001 is this
    # shape's owner now (see the note above); this fixture proves only
    # `kit.errors.lifespan-swallowed-failure` still fires on it
    try:
        run_migrations()
    except MigrationError:
        ready = False
    yield
    _ = (app, ready)


# ok: kit.errors.lifespan-swallowed-failure
async def lifespan(app: object):  # a second AST-level case, same name -- static analysis only, never executed
    try:
        run_migrations()
    except MigrationError:
        raise
    yield
    _ = app


def run_migrations() -> None: ...


class MigrationError(Exception): ...
