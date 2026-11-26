"""``linear.ids.node_uuid``: the deterministic client ids of the Linear exports."""

from __future__ import annotations

import uuid

import pytest

from linear.ids import KIND_STORY, KIND_TASK, NAMESPACE, node_uuid

PROJECT = uuid.UUID("11111111-2222-3333-4444-555555555555")


def test_the_same_inputs_make_the_same_uuid_v5() -> None:
    first = node_uuid(PROJECT, KIND_STORY, "STORY::A")

    assert first == node_uuid(PROJECT, KIND_STORY, "STORY::A")
    assert first == node_uuid(str(PROJECT), KIND_STORY, "STORY::A")  # a string project id is the same project
    assert first.version == 5


def test_each_input_changes_the_uuid() -> None:
    base = node_uuid(PROJECT, KIND_STORY, "A")

    assert node_uuid(PROJECT, KIND_TASK, "A") != base
    assert node_uuid(PROJECT, KIND_STORY, "B") != base
    assert node_uuid(uuid.UUID(int=1), KIND_STORY, "A") != base


def test_the_namespace_never_changes() -> None:
    # Every id made so far derives from it: a change would duplicate every exported object on the next run.
    assert str(NAMESPACE) == "9ed25fa5-cf91-5e3c-a6d0-b56fa514674c"


@pytest.mark.parametrize(("kind", "node"), [("", "A"), ("  ", "A"), ("story", ""), ("story", " ")])
def test_a_blank_kind_or_node_is_refused(kind: str, node: str) -> None:
    with pytest.raises(ValueError, match="blank"):
        node_uuid(PROJECT, kind, node)


def test_a_malformed_project_id_is_refused() -> None:
    with pytest.raises(ValueError, match="badly formed"):
        node_uuid("not-a-uuid", KIND_STORY, "A")
