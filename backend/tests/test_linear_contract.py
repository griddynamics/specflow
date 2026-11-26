"""The gateway contract, its two implementations and the foundation stubs of the streams."""

from __future__ import annotations

import inspect

import pytest

from linear.errors import (
    LinearAlreadyExistsError,
    LinearCode,
    LinearError,
    LinearForbiddenError,
    LinearInvalidError,
    LinearNotFoundError,
    LinearRateLimitedError,
    LinearUnauthorizedError,
    LinearUnavailableError,
)
from linear.fake import FakeLinear
from linear.gateway import LinearGateway
from linear.http import HttpLinearGateway

# The methods the streams S3 to S5 code against: a change here is a change of their contract.
PROTOCOL_METHODS = (
    "viewer",
    "teams",
    "projects",
    "project",
    "workflow_states",
    "labels",
    "create_project",
    "create_milestone",
    "create_label",
    "create_issue",
    "update_issue",
    "issue",
    "issues_of_project",
    "children",
    "comments",
    "create_comment",
    "create_relation",
    "relations",
    "attach_link",
    "project_documents",
    "resolve_link",
)


def public_methods(cls: type) -> set[str]:
    return {name for name, member in inspect.getmembers(cls, inspect.isfunction) if not name.startswith("_")}


def test_the_protocol_lists_exactly_the_agreed_methods() -> None:
    assert public_methods(LinearGateway) == set(PROTOCOL_METHODS)


@pytest.mark.parametrize("implementation", [FakeLinear, HttpLinearGateway])
def test_each_implementation_has_the_protocol_methods_with_the_same_parameters(implementation: type) -> None:
    for name in PROTOCOL_METHODS:
        wanted = inspect.signature(getattr(LinearGateway, name))
        got = inspect.signature(getattr(implementation, name))
        assert list(got.parameters) == list(wanted.parameters), name
        assert [p.kind for p in got.parameters.values()] == [p.kind for p in wanted.parameters.values()], name
        assert inspect.iscoroutinefunction(getattr(implementation, name)), name


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (LinearUnauthorizedError(), "linear_unauthorized"),
        (LinearForbiddenError(), "linear_forbidden"),
        (LinearNotFoundError(), "linear_not_found"),
        (LinearRateLimitedError(3), "linear_rate_limited"),
        (LinearInvalidError("estimate"), "linear_invalid"),
        (LinearAlreadyExistsError(), "linear_already_exists"),
        (LinearUnavailableError(), "linear_unreachable"),
    ],
)
def test_an_error_says_its_closed_code_and_nothing_else(error: LinearError, code: str) -> None:
    assert str(error) == code
    assert error.code == LinearCode(code)


def test_a_rate_limit_knows_when_to_retry_and_an_invalid_input_which_field() -> None:
    assert LinearRateLimitedError(12.5).retry_after == 12.5
    assert LinearRateLimitedError().retry_after is None
    assert LinearInvalidError("estimate").field == "estimate"
