"""Lifecycle argument checks happen before any query; there is no forced claim."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from graph.core.errors import ConfigurationError
from graph.plan.errors import TaskStateError
from graph.plan.lifecycle import TaskLifecycle
from graph_plan_support import plan_ontology, plan_rules
from graph_support import OfflineExecutor

TOKEN = "t" * 32


def _lifecycle() -> TaskLifecycle:
    ontology = plan_ontology()
    return TaskLifecycle(OfflineExecutor(), ontology, plan_rules(ontology))


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (lambda life: life.complete_task("shop", "T", claim_token=TOKEN, commit="a" * 39), "invalid_commit"),
        (lambda life: life.complete_task("shop", "T", claim_token=TOKEN, commit="g" * 40), "invalid_commit"),
        (lambda life: life.block_task("shop", "T", claim_token=TOKEN, reason="bored"), "invalid_reason"),
        (lambda life: life.fail_task("shop", "T", claim_token=TOKEN, note="x" * 501), "invalid_note"),
        (lambda life: life.claim_task("shop", "T", claimed_by=""), "invalid_claimant"),
        (lambda life: life.claim_task("shop", "T", claimed_by="a" * 201), "invalid_claimant"),
        (lambda life: life.claim_next("shop", "S", claimed_by="agent\n"), "invalid_claimant"),
    ],
)
async def test_invalid_arguments_are_refused_before_any_query(
    call: Callable[[TaskLifecycle], Awaitable[Any]], code: str
) -> None:
    with pytest.raises(ConfigurationError) as raised:
        await call(_lifecycle())

    assert raised.value.code == code


async def test_a_release_needs_the_claim_token_or_force() -> None:
    with pytest.raises(TaskStateError) as raised:
        await _lifecycle().release_task("shop", "T")

    assert raised.value.code == "claim_mismatch"


def test_claims_offer_no_way_to_skip_prerequisites() -> None:
    for method in (TaskLifecycle.claim_task, TaskLifecycle.claim_next):
        assert set(inspect.signature(method).parameters) <= {"self", "project_id", "task_id", "story_id", "claimed_by"}


def test_a_task_state_error_carries_its_blockers_and_their_count() -> None:
    error = TaskStateError("not_ready", blockers=("A", "B"))

    assert error.blockers == ("A", "B") and error.fields["blockers"] == 2
