"""Stable identities of plan nodes."""

from __future__ import annotations

import uuid

import pytest

from graph.core.identity import NAMESPACE
from graph.plan.identity import (
    area_id,
    chapter_slug,
    plan_uuid,
    revision_id,
    story_id,
    task_key,
)


def test_area_ids_strip_the_module_prefix_and_resolve_collisions() -> None:
    assert area_id("MOD::ORDERS_SERVICE") == "AREA::ORDERS_SERVICE"
    assert area_id("MOD::ORDERS", taken=("AREA::ORDERS",)) == "AREA::ORDERS_2"
    assert area_id("") == "AREA::PROJECT"


def test_story_ids_are_stable_and_resolve_collisions() -> None:
    assert story_id("AREA::ORDERS_SERVICE", "Order history export") == "STORY::ORDERS_SERVICE::ORDER_HISTORY_EXPORT"
    assert story_id("AREA::A", "Place order", taken=("STORY::A::PLACE_ORDER",)) == "STORY::A::PLACE_ORDER_2"


def test_chapter_slugs_are_stable_and_cut_to_sixty_characters() -> None:
    first = chapter_slug("Requirements", "Checkout and payment")
    second = chapter_slug("Requirements", "Checkout and payment")
    assert first == second == "CH_REQUIREMENTS_CHECKOUT_PAYMENT"
    assert len(chapter_slug("A" * 100, "B" * 100)) <= 60
    assert chapter_slug("", "") == "CH_NODE_NODE"


def test_task_keys_validate_actions_and_revisions() -> None:
    assert task_key("AREA::ORDERS_SERVICE", "ADD_ORDER") == "TASK::ORDERS_SERVICE::ADD_ORDER"
    with pytest.raises(ValueError, match="invalid task action"):
        task_key("AREA::A", "X_R2")
    assert revision_id("TASK::A::ADD_ORDER", 2) == "TASK::A::ADD_ORDER_R2"


def test_plan_uuid_uses_the_core_namespace() -> None:
    assert plan_uuid("project-1", "AREA::A") == str(uuid.uuid5(NAMESPACE, "project-1|plan|AREA::A"))
