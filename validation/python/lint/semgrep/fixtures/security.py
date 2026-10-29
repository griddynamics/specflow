"""Fixtures for semgrep/security.yml: positive and negative
cases per rule, one section per rule id."""

import asyncio
import os
import uuid


# --- kit.security.identity-from-header ---


def resolve_role(request: object) -> str:
    # ruleid: kit.security.identity-from-header
    return request.headers.get("X-User-Role", "")


def resolve_actor(request: object) -> str:
    # ruleid: kit.security.identity-from-header
    return request.headers.get("X-User-Id")


def resolve_locale(request: object) -> str:
    # ok: kit.security.identity-from-header
    return request.headers.get("Accept-Language", "en")


# --- kit.security.cookie-missing-safe-flags ---


def issue_session_cookie_unsafe(response: object, token: str) -> None:
    # ruleid: kit.security.cookie-missing-safe-flags
    response.set_cookie(key="session", value=token, secure=True, samesite="lax")


def issue_session_cookie(response: object, token: str, *, ttl_s: int) -> None:
    # ok: kit.security.cookie-missing-safe-flags
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=ttl_s,
    )


# --- kit.security.runtime-tunable-literal ---


async def call_with_timeout(agent: object, prompt: str) -> object:
    # ruleid: kit.security.runtime-tunable-literal
    return await asyncio.wait_for(agent.run(prompt), timeout=10)


def build_retry_policy() -> dict[str, int]:
    # ruleid: kit.security.runtime-tunable-literal
    return {"max_attempts": retries(retries=3)}


def retries(retries: int) -> int:
    return retries


async def call_with_configured_timeout(agent: object, prompt: str, settings: object) -> object:
    # ok: kit.security.runtime-tunable-literal
    return await asyncio.wait_for(agent.run(prompt), timeout=settings.request_timeout_s)


def request_query_bound(min_length: int) -> object:
    # a request-validation bound, not a settings tunable
    # ok: kit.security.runtime-tunable-literal
    return Query(min_length=1)


def Query(**kwargs: object) -> object: ...


# --- kit.security.delete-cookie-missing-matching-flags ---


def clear_session_cookie_unsafe(response: object) -> None:
    # ruleid: kit.security.delete-cookie-missing-matching-flags
    response.delete_cookie(key="session")


def clear_session_cookie(response: object) -> None:
    # ok: kit.security.delete-cookie-missing-matching-flags
    response.delete_cookie(key="session", httponly=True, secure=True, samesite="lax")


def clear_session_cookie_without_secure(response: object) -> None:
    # ruleid: kit.security.delete-cookie-missing-matching-flags
    response.delete_cookie(key="session", httponly=True, samesite="lax")


# --- kit.security.request-id-outside-middleware ---
#
# The middleware's "ok" case is the real-scan file `fixtures/src/main.py`, since `--test`
# ignores `paths:` (`test_request_id_middleware_exclusion_via_real_scan`).


def stamp_request_id(payload: dict[str, object]) -> dict[str, object]:
    # ruleid: kit.security.request-id-outside-middleware
    request_id = uuid.uuid4()
    payload["request_id"] = str(request_id)
    return payload


def stamp_request_id_stringified() -> str:
    # ruleid: kit.security.request-id-outside-middleware
    request_id = str(uuid.uuid4())
    return request_id


def stamp_correlation_id() -> str:
    # not request-id-named
    # ok: kit.security.request-id-outside-middleware
    correlation_id = str(uuid.uuid4())
    return correlation_id


# --- kit.security.request-id-read-outside-middleware ---


def read_incoming_request_id(request: object) -> str | None:
    # ruleid: kit.security.request-id-read-outside-middleware
    return request.headers.get("X-Request-Id")


def read_incoming_request_id_lowercase(request: object) -> str | None:
    # ruleid: kit.security.request-id-read-outside-middleware
    return request.headers.get("x-request-id")


def read_locale_header(request: object) -> str | None:
    # ok: kit.security.request-id-read-outside-middleware
    return request.headers.get("Accept-Language", "en")


# --- kit.security.environment-read-outside-settings ---


def read_feature_flag() -> str:
    # ruleid: kit.security.environment-read-outside-settings
    return os.environ.get("FEATURE_FLAG", "off")


def read_bucket_name() -> str | None:
    # ruleid: kit.security.environment-read-outside-settings
    return os.getenv("GCS_BUCKET")


def read_feature_flag_from_settings(settings: object) -> str:
    # ok: kit.security.environment-read-outside-settings
    return settings.feature_flag
