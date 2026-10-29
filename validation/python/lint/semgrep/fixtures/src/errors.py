"""Fixture for semgrep/errors.yml's `kit.errors.http-exception-outside-handlers` rule, the
`ok` half. Named and nested exactly as `src/errors.py` so `paths.exclude` shields it -- this
is the one module allowed to build an HTTPException by hand."""

from fastapi import HTTPException


def not_found() -> HTTPException:
    # ok: kit.errors.http-exception-outside-handlers
    return HTTPException(status_code=404, detail="not found")
