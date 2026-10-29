from __future__ import annotations

import pytest

from llm import CrashBackoffPolicy


def test_zero_progress_failures_follow_schedule_then_exhaust() -> None:
    policy = CrashBackoffPolicy((1, 3, 5))
    streak = 0
    delays: list[float] = []

    for _ in policy.delays:
        retry = policy.next_retry(
            crash_streak=streak,
            saved_progress=False,
        )
        assert retry is not None
        streak = retry.crash_streak
        delays.append(retry.delay)

    assert delays == [1, 3, 5]
    assert (
        policy.next_retry(
            crash_streak=streak,
            saved_progress=False,
        )
        is None
    )


def test_saved_progress_resets_streak_and_uses_first_delay() -> None:
    retry = CrashBackoffPolicy((1, 3)).next_retry(
        crash_streak=2,
        saved_progress=True,
    )

    assert retry is not None
    assert retry.delay == 1
    assert retry.crash_streak == 0
    assert retry.saved_progress


def test_policy_copies_caller_delays() -> None:
    delays = [1, 3]
    policy = CrashBackoffPolicy(delays)
    delays.append(5)

    assert policy.delays == (1, 3)


@pytest.mark.parametrize(
    ("delays", "error"),
    [
        ((), ValueError),
        ((1, -1), ValueError),
        ((float("nan"),), ValueError),
        ((float("inf"),), ValueError),
        ((float("-inf"),), ValueError),
        ((False,), TypeError),
        (("1",), TypeError),
    ],
)
def test_policy_validates_delays(
    delays: tuple[object, ...],
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        CrashBackoffPolicy(delays)


@pytest.mark.parametrize(
    ("streak", "error"),
    [
        (-1, ValueError),
        (True, TypeError),
        (1.5, TypeError),
    ],
)
def test_policy_validates_crash_streak(
    streak: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        CrashBackoffPolicy().next_retry(
            crash_streak=streak,
            saved_progress=False,
        )
