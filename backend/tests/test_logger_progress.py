"""RED tests for `logger.progress` — LOG-38.

Scenario text: the central-logger test catalog
(row LOG-38: "totals 12/100/1000/None -> step 25/25/100/25; last
tick always; force always"). Module contract:
the Specgraph progress logging rule (Sampling paragraph: "one ... per ~25 fragments (or
~10%, whichever larger)").

`src/logger/progress.py` does not exist yet — Wave 3 of the
central-logger plan (`w3-0`) builds it. Every import below is therefore
expected to fail with `ModuleNotFoundError`/`ImportError` today; that is
the correct RED state for this Wave 2 track (`w2-a1`). Do not skip or
xfail these tests.

Ambiguity note (flagged in the W2.A1 report): the exact boundary of "bad
args" for the constructor is not spelled out beyond "Bad args ->
ValueError" in the SPECS contract. This file's best-effort interpretation
is that a negative `total`, and a non-positive `every` or `fraction`
(neither of which could ever produce a usable positive step), are the
invalid cases.
"""

from __future__ import annotations

import math

import pytest

from logger.progress import ProgressSampler


@pytest.mark.parametrize(
    ("total", "expected_step"),
    [
        pytest.param(12, 25, id="total-12-uses-the-every-floor"),
        pytest.param(100, 25, id="total-100-uses-the-every-floor"),
        pytest.param(1000, 100, id="total-1000-uses-the-fraction"),
        pytest.param(None, 25, id="total-none-uses-every"),
    ],
)
def test_step_matches_every_or_fraction_whichever_is_larger(total: int | None, expected_step: int) -> None:
    sampler = ProgressSampler(total, every=25, fraction=0.10)

    assert sampler.step == expected_step


def test_step_formula_is_max_of_every_and_ceil_total_times_fraction() -> None:
    sampler = ProgressSampler(1000, every=25, fraction=0.10)

    assert sampler.step == max(25, math.ceil(1000 * 0.10))


def test_last_tick_is_always_logged_even_off_step() -> None:
    sampler = ProgressSampler(12, every=25, fraction=0.10)  # step == 25

    assert sampler.should_log(12) is True  # done == total
    assert sampler.should_log(13) is True  # overshoot past total
    assert sampler.should_log(11) is False  # short of total, off-step


def test_should_log_true_on_step_multiples() -> None:
    sampler = ProgressSampler(1000, every=25, fraction=0.10)  # step == 100

    assert sampler.should_log(100) is True
    assert sampler.should_log(200) is True
    assert sampler.should_log(150) is False
    assert sampler.should_log(0) is True  # 0 % step == 0


def test_force_always_logs_regardless_of_step_or_total() -> None:
    sampler = ProgressSampler(1000, every=25, fraction=0.10)

    assert sampler.should_log(1, force=True) is True
    assert sampler.should_log(999, force=True) is True


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"total": -1}, id="negative-total"),
        pytest.param({"total": 100, "every": 0}, id="zero-every"),
        pytest.param({"total": 100, "every": -5}, id="negative-every"),
        pytest.param({"total": 100, "fraction": 0}, id="zero-fraction"),
        pytest.param({"total": 100, "fraction": -0.1}, id="negative-fraction"),
    ],
)
def test_bad_constructor_args_raise_value_error(kwargs: dict[str, object]) -> None:
    kwargs = dict(kwargs)
    total = kwargs.pop("total")

    with pytest.raises(ValueError, match="must be"):
        ProgressSampler(total, **kwargs)
