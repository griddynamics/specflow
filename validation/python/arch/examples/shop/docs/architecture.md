# Shop architecture

The example service the archkit self-tests run against. `archkit build-catalogue` reads the
error-code table below (see `tests/test_markdown.py`).

## HTTP conventions

### Error envelope

Every error answers `{"error": {"code": "...", "message": "..."}}`, never a traceback.

| Code | HTTP |
|---|---|
| `UNAUTHENTICATED` | 401 |
| `FORBIDDEN` | 403 |
| `NOT_FOUND` | 404 |
| `METHOD_NOT_ALLOWED` | 405 |
| `CONFLICT` | 409 |
| `VALIDATION_ERROR` | 422 |
| `INTERNAL_ERROR` | 500 |

### Request ids

Every response, errors included, carries `X-Request-Id`.

### Routes

Every 2xx response is typed; creates answer 201; only the health and login routes are
anonymous.

## Contract snapshot

`contracts/openapi.json` is the committed OpenAPI document.
