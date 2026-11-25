"""``api.linear.errors``: the API answer of every Linear failure (fixed text, closed codes)."""

from __future__ import annotations

import pytest

from api.linear.errors import linear_error, linear_not_connected, linear_unavailable
from linear.errors import (
    LinearAlreadyExistsError,
    LinearError,
    LinearForbiddenError,
    LinearInvalidError,
    LinearNotFoundError,
    LinearRateLimitedError,
    LinearUnauthorizedError,
    LinearUnavailableError,
)


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (LinearUnauthorizedError(), 409, "linear_reconnect_required"),
        (LinearForbiddenError(), 409, "linear_forbidden"),
        (LinearNotFoundError(), 409, "linear_not_found"),
        (LinearRateLimitedError(4), 429, "linear_rate_limited"),
        (LinearInvalidError(), 422, "linear_invalid"),
        (LinearAlreadyExistsError(), 422, "linear_invalid"),
        (LinearUnavailableError(), 502, "linear_unreachable"),
    ],
)
def test_a_linear_failure_is_one_closed_api_error(error: LinearError, status: int, code: str) -> None:
    answer = linear_error(error)

    assert (answer.status, answer.code) == (status, code)


def test_a_rate_limit_tells_the_client_when_to_retry_and_an_invalid_input_which_field() -> None:
    assert linear_error(LinearRateLimitedError(4.2)).headers == {"Retry-After": "4"}
    assert linear_error(LinearRateLimitedError()).headers == {}
    assert linear_error(LinearInvalidError("estimate")).details == {"field": "estimate"}


def test_the_two_fixed_answers() -> None:
    assert (linear_unavailable().status, linear_unavailable().code) == (503, "linear_unavailable")
    assert (linear_not_connected().status, linear_not_connected().code) == (409, "linear_not_connected")
