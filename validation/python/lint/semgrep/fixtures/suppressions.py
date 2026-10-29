"""Fixtures for semgrep/suppressions.yml: positive and
negative cases per rule, one section per rule id."""

import pytest


# --- kit.suppressions.python-comment-suppression ---


def parse(value):
    # ruleid: kit.suppressions.python-comment-suppression
    return value  # noqa: E501


def load(raw):
    # ruleid: kit.suppressions.python-comment-suppression
    result = raw  # type: ignore[assignment]
    return result


def run_shell(cmd: str) -> None:
    # ruleid: kit.suppressions.python-comment-suppression
    exec(cmd)  # nosec B102


def slow_path(items: list) -> list:
    # ruleid: kit.suppressions.python-comment-suppression
    return items  # pragma: no cover


def legacy(value: int) -> int:
    # ruleid: kit.suppressions.python-comment-suppression
    return value  # pylint: disable=invalid-name


# ruleid: kit.suppressions.python-comment-suppression
# fmt: off
MATRIX = [1, 0, 0,
          0, 1, 0]
# fmt: on


def documented(value: int) -> int:
    # ok: kit.suppressions.python-comment-suppression
    # explains why value is damped, no suppression token here.
    return value // 2


# The suppressed line below cannot carry a test marker: semgrep self-suppresses it even
# under `--test`, so `test_nosemgrep_is_caught_by_a_real_scan` in tests/test_semgrep_rules.py proves
# detection with a real scan that disables nosem handling.
def bypass_review(value: int) -> int:
    return value  # nosemgrep


# --- kit.suppressions.pytest-skip-decorator ---


# ruleid: kit.suppressions.pytest-skip-decorator
@pytest.mark.skip(reason="flaky")
def test_something_flaky() -> None:
    assert True


# ruleid: kit.suppressions.pytest-skip-decorator
@pytest.mark.skipif(True, reason="not ready")
def test_something_not_ready() -> None:
    assert True


# ruleid: kit.suppressions.pytest-skip-decorator
@pytest.mark.xfail
def test_something_broken() -> None:
    assert True


# ok: kit.suppressions.pytest-skip-decorator
@pytest.mark.slow
def test_order_total_is_positive() -> None:
    assert True


# --- kit.suppressions.pytest-skip-call ---


def test_conditional_skip() -> None:
    if not has_docker():
        # ruleid: kit.suppressions.pytest-skip-call
        pytest.skip("docker not available")
    assert True


def test_conditional_xfail() -> None:
    if not has_docker():
        # ruleid: kit.suppressions.pytest-skip-call
        pytest.xfail("known issue")
    assert True


def test_conditional_assert() -> None:
    # ok: kit.suppressions.pytest-skip-call
    assert has_docker()


def has_docker() -> bool: ...
