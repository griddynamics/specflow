"""RED tests for `logger.correlation` — LOG-15, LOG-16.

Scenario text: the central-logger test catalog
(rows LOG-15/16). Module contract: the "ID unification" rules.

`src/logger/correlation.py` does not exist yet — Wave 3 of the
central-logger plan (`w3-0`) builds it. Every import below is therefore
expected to fail with `ModuleNotFoundError`/`ImportError` today; that is
the correct RED state for this Wave 2 track (`w2-a1`). Do not skip or
xfail these tests.
"""

from __future__ import annotations

import re

import pytest

from logger.correlation import (
    bound,
    current_agent_id,
    current_request_id,
    current_run_id,
    current_session_id,
    new_request_context,
)

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# LOG-15 — correlation keys appear on a record only when bound; a
# `log_event(..., run_id="x")` kwarg that collides with a correlation key is
# renamed to `field_run_id`, while the *real* bound run_id still appears
# under the reserved `run_id` key.
# ---------------------------------------------------------------------------


def test_log_event_without_binding_has_no_correlation_keys(central_log) -> None:
    from logger.logger import INFO, get_logger, log_event

    central_log.configure()
    logger = get_logger("logger-tests.correlation")

    log_event(logger, INFO, "corr.unbound")

    [record] = central_log.events("corr.unbound")
    for key in ("request_id", "run_id", "session_id", "agent_id"):
        assert key not in record


def test_log_event_records_every_bound_correlation_key(central_log) -> None:
    from logger.logger import INFO, get_logger, log_event

    central_log.configure()
    logger = get_logger("logger-tests.correlation")

    with bound(
        request_id="req-0001",
        run_id="run-0001",
        session_id="sess-0001",
        agent_id="agent-0001",
    ):
        log_event(logger, INFO, "corr.all_bound")

    [record] = central_log.events("corr.all_bound")
    assert record["request_id"] == "req-0001"
    assert record["run_id"] == "run-0001"
    assert record["session_id"] == "sess-0001"
    assert record["agent_id"] == "agent-0001"


def test_log_event_field_colliding_with_run_id_is_renamed_to_field_run_id(
    central_log,
) -> None:
    from logger.logger import INFO, get_logger, log_event

    central_log.configure()
    logger = get_logger("logger-tests.correlation")

    with bound(run_id="the-real-run-id"):
        log_event(logger, INFO, "corr.collision", run_id="x")

    [record] = central_log.events("corr.collision")
    assert record["run_id"] == "the-real-run-id"
    assert record["field_run_id"] == "x"


# ---------------------------------------------------------------------------
# LOG-16 — nested `new_request_context`: yields a fresh 32-hex request id;
# the inner context clears run_id/session_id/agent_id; state is restored on
# normal exit *and* on an exception propagating out of the block.
# ---------------------------------------------------------------------------


def test_new_request_context_yields_a_fresh_32_hex_request_id() -> None:
    with new_request_context() as request_id:
        assert _HEX32.match(request_id)
        assert current_request_id() == request_id


def test_new_request_context_accepts_an_explicit_request_id() -> None:
    with new_request_context(request_id="caller-supplied-id") as request_id:
        assert request_id == "caller-supplied-id"
        assert current_request_id() == "caller-supplied-id"


def test_new_request_context_clears_run_session_agent_inside() -> None:
    with bound(run_id="outer-run", session_id="outer-session", agent_id="outer-agent"):
        with new_request_context():
            assert current_run_id() is None
            assert current_session_id() is None
            assert current_agent_id() is None

        # Restored once the (non-nested) request context exits normally.
        assert current_run_id() == "outer-run"
        assert current_session_id() == "outer-session"
        assert current_agent_id() == "outer-agent"


def test_new_request_context_nested_restores_outer_request_id() -> None:
    with new_request_context() as outer_id:
        with new_request_context() as inner_id:
            assert inner_id != outer_id
            assert _HEX32.match(inner_id)
            assert current_request_id() == inner_id

        assert current_request_id() == outer_id


def test_new_request_context_restores_state_on_exception() -> None:
    # `new_request_context()` clears run_id/session_id/agent_id on entry
    # (LOG-16; SPECS §4.1
    # correlation.py row), so the *middle* context below clears run_id to
    # None the moment it is entered — "crash-run" only reappears once
    # THAT context itself exits, not merely once the inner one does.
    with bound(run_id="crash-run"):
        with new_request_context() as request_id:
            assert current_request_id() == request_id
            assert current_run_id() is None  # cleared by this (middle) context

            def fail_inside_inner_context() -> None:
                with new_request_context():
                    assert current_run_id() is None
                    raise RuntimeError("boom")

            with pytest.raises(RuntimeError, match="boom"):
                fail_inside_inner_context()

            # The inner context's failure must not leak: run_id and the
            # middle request_id are both back to what they were right
            # before the inner `with` block (i.e. still cleared by the
            # middle context, which has not exited yet), even though the
            # inner context exited via an exception rather than falling
            # off the end.
            assert current_run_id() is None
            assert current_request_id() == request_id

        # Only once the middle `new_request_context()` itself exits does
        # run_id restore to the value bound before IT started.
        assert current_run_id() == "crash-run"
