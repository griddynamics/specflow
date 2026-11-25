"""The desired Jira state and the content rules: FR-JIRA-MAP-02..05, 11, UPD-09, REL-01..03, pure."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from api.exports.digest import canonical, digest
from api.jira import content as rules
from api.jira.desired import EPIC, Desired, Row, SpecRef, build, content, spec_ids
from jira.reasons import Reason
from jira_sync_support import basic_plan, make_plan, story, task

NOW = datetime(2026, 1, 2, tzinfo=UTC)


def row(node: str, kind: str = "subtask", key: str | None = "PRJ-9", **values) -> Row:
    fields = {"requested_at": NOW, "content": None, "content_hash": None, "position": None}
    fields |= {"removed_at": None, "error_code": None, "synced_at": None}
    return Row(node, kind, key, **(fields | values))


def desired(plan=None, *, epic_key=None, rows=(), names=None, name="Shop") -> Desired:
    return build(
        plan or basic_plan(),
        project_name=name,
        epic_key=epic_key,
        rows={item.node_id: item for item in rows},
        names=names or {},
    )


def body(result: Desired, node: str, keys=None):
    return content(result.items[node], keys or {}, None)


def test_items_follow_the_mapping_in_creation_order():
    result = desired()
    assert list(result.items) == [
        EPIC,
        "STORY::A::ONE",
        "TASK::A::1",
        "TASK::A::2",
        "STORY::A::THREE",
        "TASK::A::5",
        "STORY::A::TWO",
        "TASK::A::3",
        "TASK::A::4",
    ]
    kinds = {node: item.kind for node, item in result.items.items()}
    assert kinds[EPIC] == "epic" and kinds["STORY::A::TWO"] == "story" and kinds["TASK::A::3"] == "subtask"
    assert result.items["TASK::A::3"].parent == "STORY::A::TWO"
    assert result.items["STORY::A::TWO"].parent == EPIC


def test_a_configured_epic_has_no_epic_item_and_parents_the_stories():
    result = desired(epic_key="PRJ-7")
    assert EPIC not in result.items
    assert content(result.items["STORY::A::ONE"], {}, "PRJ-7")["parent"] == "PRJ-7"


def test_the_epic_content_is_the_project_name_with_the_owned_label():
    epic = body(desired(name="  My\n  shop "), EPIC)
    assert epic == {
        "v": rules.MAPPING_VERSION,
        "summary": "My shop",
        "text": None,
        "release": None,
        "phase": None,
        "done_when": [],
        "specs": [],
        "task_id": None,
        "points": None,
        "labels": ["specflow"],
        "parent": None,
        "blocked_by": [],
    }


def test_points_are_one_three_five_and_a_story_sums_its_live_sub_tasks():
    result = desired()
    assert [body(result, node)["points"] for node in ("TASK::A::1", "TASK::A::2", "TASK::A::3")] == [1, 3, 5]
    assert body(result, "STORY::A::ONE")["points"] == 4
    assert body(result, "STORY::A::TWO")["points"] == 8
    assert body(result, "STORY::A::THREE")["points"] == 3  # a completed task stays live


def test_low_and_high_tasks_give_sub_tasks_one_and_five_and_their_story_six():
    plan = make_plan(
        [story("STORY::A::ONE")], [task("TASK::A::1", complexity="low"), task("TASK::A::2", complexity="high")]
    )
    result = desired(plan)
    assert [body(result, node)["points"] for node in ("TASK::A::1", "TASK::A::2", "STORY::A::ONE")] == [1, 5, 6]


def test_a_story_without_live_tasks_has_no_points():
    plan = make_plan([story("STORY::A::ONE")], [task("TASK::A::1", status="retired")])
    assert body(desired(plan), "STORY::A::ONE")["points"] is None


def test_task_fields_and_labels():
    plan = make_plan(
        [story("STORY::A::ONE", summary="Why.", release="R1", area_id="AREA::Orders & Pay")],
        [task("TASK::A::1", area_id="AREA::Orders & Pay", done_when=({"text": "ships", "source": "spec"},))],
    )
    result = desired(plan)
    assert body(result, "TASK::A::1")["done_when"] == ["ships"]
    assert body(result, "TASK::A::1")["labels"] == ["area-orders-pay", "specflow"]
    assert body(result, "STORY::A::ONE")["text"] == "Why." and body(result, "STORY::A::ONE")["release"] == "R1"


def test_task_waits_become_blocked_by_base_ids_for_live_prerequisites_only():
    plan = make_plan(
        [story("STORY::A::ONE")],
        [
            task("TASK::A::1"),
            task("TASK::A::2_R2", revision=2),
            task("TASK::A::2", status="superseded"),
            task("TASK::A::3", status="retired"),
            task("TASK::A::4"),
        ],
        waits=[
            ("TASK::A::2_R2", "TASK::A::1"),
            ("TASK::A::2_R2", "TASK::A::3"),
            ("TASK::A::4", "TASK::A::2"),
            ("TASK::A::4", "TASK::A::4_R2"),
        ],
    )
    keys = {node: f"K-{node}" for node in ("TASK::A::1", "TASK::A::2", "TASK::A::4")}
    result = desired(plan)
    assert body(result, "TASK::A::2", keys)["blocked_by"] == ["TASK::A::1"]
    assert body(result, "TASK::A::4", keys)["blocked_by"] == ["TASK::A::2"]
    assert body(result, "TASK::A::4", {})["blocked_by"] == []  # a prerequisite without a key is not linked yet
    assert "TASK::A::3" not in result.items


def test_story_links_are_derived_from_cross_story_edges_and_cycles_are_kept():
    plan = make_plan(
        [story("STORY::A::ONE"), story("STORY::A::TWO"), story("STORY::A::THREE")],
        [
            task("TASK::A::1"),
            task("TASK::A::2", "STORY::A::TWO", "completed"),
            task("TASK::A::3", "STORY::A::TWO"),
            task("TASK::A::4", "STORY::A::THREE"),
        ],
        waits=[
            ("TASK::A::1", "TASK::A::2"),
            ("TASK::A::3", "TASK::A::2"),
            ("TASK::A::2", "TASK::A::1"),
            ("TASK::A::1", "TASK::A::4"),
        ],
    )
    result = desired(plan)
    keys = {node: f"K-{node}" for node in result.items}
    assert body(result, "STORY::A::ONE", keys)["blocked_by"] == ["STORY::A::THREE", "STORY::A::TWO"]
    assert body(result, "STORY::A::TWO", keys)["blocked_by"] == ["STORY::A::ONE"]
    assert body(result, "STORY::A::THREE", keys)["blocked_by"] == []
    assert set(result.orders[EPIC]) == {"STORY::A::ONE", "STORY::A::TWO", "STORY::A::THREE"}


def test_rank_orders_put_prerequisites_first_and_append_cycle_members_sorted():
    plan = make_plan(
        [story("STORY::A::ONE"), story("STORY::A::TWO")],
        [task("TASK::A::1"), task("TASK::A::2"), task("TASK::A::3"), task("TASK::A::4"), task("TASK::A::0")],
        waits=[("TASK::A::1", "TASK::A::2"), ("TASK::A::3", "TASK::A::4"), ("TASK::A::4", "TASK::A::3")],
    )
    assert desired(plan).orders["STORY::A::ONE"] == (
        "TASK::A::0",
        "TASK::A::2",
        "TASK::A::1",
        "TASK::A::3",
        "TASK::A::4",
    )


def test_a_superseded_revision_with_a_live_successor_is_not_removed():
    plan = make_plan(
        [story("STORY::A::ONE")],
        [task("TASK::A::1", status="superseded"), task("TASK::A::1_R2", revision=2)],
    )
    result = desired(plan, rows=[row("TASK::A::1")])
    assert result.removed == () and "TASK::A::1" in result.items
    assert body(result, "TASK::A::1")["task_id"] == "TASK::A::1_R2"


def test_missing_closed_and_retired_story_items_are_removed_in_kind_order():
    plan = make_plan(
        [story("STORY::A::ONE", status="retired"), story("STORY::A::TWO")],
        [task("TASK::A::1"), task("TASK::A::2", "STORY::A::TWO", "retired")],
    )
    rows = [
        row("TASK::A::2"),
        row("TASK::A::1"),
        row("STORY::A::ONE", "story"),
        row("TASK::A::9"),
        row("EPIC", "epic"),
        row("TASK::A::pending", key=None),
    ]
    result = desired(plan, epic_key="PRJ-7", rows=rows)
    assert result.removed == ("EPIC", "STORY::A::ONE", "TASK::A::1", "TASK::A::2", "TASK::A::9")


def test_a_removed_row_that_is_wanted_again_is_revived():
    result = desired(rows=[row("STORY::A::ONE", "story", removed_at=NOW), row("TASK::A::1", removed_at=NOW)])
    assert result.revived == ("STORY::A::ONE", "TASK::A::1") and result.removed == ()


def test_a_removed_epic_is_revived_when_the_epic_key_is_cleared():
    assert desired(rows=[row("EPIC", "epic", removed_at=NOW)]).revived == ("EPIC",)


def test_a_row_deleted_in_jira_is_skipped_with_its_new_sub_tasks():
    rows = [
        row("TASK::A::1", removed_at=NOW, error_code=Reason.NOT_FOUND.value),
        row("STORY::A::TWO", "story", removed_at=NOW, error_code=Reason.NOT_FOUND.value),
        row("TASK::A::3"),
    ]
    result = desired(rows=rows)
    assert result.deleted == {"TASK::A::1", "STORY::A::TWO"}
    assert "TASK::A::1" not in result.items and "STORY::A::TWO" not in result.items
    assert not {"TASK::A::3", "TASK::A::4"} & set(result.items)
    assert result.removed == () and result.revived == ()
    assert (
        "TASK::A::2" in result.items and body(result, "STORY::A::ONE")["points"] == 4
    )  # a task deleted in Jira still counts as plan work


def test_a_closed_task_of_a_retired_story_is_not_wanted():
    plan = make_plan([story("STORY::A::ONE", status="retired")], [task("TASK::A::1")])
    assert list(desired(plan, epic_key="PRJ-7").items) == []


def test_specs_are_resolved_to_the_current_node_and_sorted():
    plan = make_plan(
        [story("STORY::A::ONE")],
        [task("TASK::A::1", implements=("OLD::B", "REQ::A", "REQ::NONE"))],
        implements=[("STORY::A::ONE", "FEAT::X"), ("STORY::A::ONE", "OLD::B")],
    )
    names = {"OLD::B": SpecRef("NEW::B", "B"), "REQ::A": SpecRef("REQ::A", "A"), "FEAT::X": SpecRef("FEAT::X", "X")}
    result = desired(plan, names=names)
    assert body(result, "TASK::A::1")["specs"] == [
        {"id": "NEW::B", "name": "B"},
        {"id": "REQ::A", "name": "A"},
        {"id": "REQ::NONE", "name": "REQ::NONE"},
    ]
    assert body(result, "STORY::A::ONE")["specs"] == [{"id": "FEAT::X", "name": "X"}, {"id": "NEW::B", "name": "B"}]
    assert spec_ids(plan) == ["FEAT::X", "OLD::B", "REQ::A", "REQ::NONE"]


def test_parent_and_blocked_by_depend_on_the_keys_known():
    result = desired()
    story_item, task_item = result.items["STORY::A::ONE"], result.items["TASK::A::2"]
    assert content(story_item, {}, None)["parent"] is None
    assert content(story_item, {EPIC: "PRJ-1"}, None)["parent"] == "PRJ-1"
    assert content(story_item, {EPIC: "PRJ-1"}, "PRJ-7")["parent"] == "PRJ-7"
    assert content(task_item, {"STORY::A::ONE": "PRJ-2"}, None)["parent"] == "PRJ-2"
    keys = {"TASK::A::1": "PRJ-3"}
    assert content(task_item, keys, None)["blocked_by"] == ["TASK::A::1"]
    assert content(task_item, keys, None, linked=False)["blocked_by"] == []


# --- content.py: limits (FR-JIRA-MAP-11), hash and the change list (FR-JIRA-UPD-02, 04, 05)


def test_a_summary_folds_whitespace_and_is_cut_to_254_plus_an_ellipsis():
    assert rules.summary("  a \n\t b  ") == "a b"
    long = "x" * 300
    assert rules.summary(long) == "x" * 254 + "…" and len(rules.summary(long)) == 255
    assert rules.summary("y" * 255) == "y" * 255


def test_a_text_is_cut_to_30000_and_a_blank_one_is_none():
    assert rules.text("t" * 30_001) == "t" * 29_999 + "…" and len(rules.text("t" * 30_001) or "") == 30_000
    assert rules.text("t" * 30_000) == "t" * 30_000
    assert rules.text("  \n ") is None and rules.text(None) is None


def test_an_area_label_is_a_lower_case_slug_cut_to_200_without_a_trailing_dash():
    assert rules.area_label("AREA::WEB_SHOP") == "area-web-shop"
    assert rules.area_label("AREA::Orders & Pay") == "area-orders-pay"
    assert rules.area_label("AREA::___") is None
    assert rules.area_label("AREA::") is None
    label = rules.area_label("AREA::" + "a" * 199 + "-b" + "c" * 50) or ""
    assert label == "area-" + "a" * 199 and not label.endswith("-")
    assert len(rules.area_label("AREA::" + "z" * 300) or "") == len("area-") + 200


def test_the_hash_is_the_sha256_of_the_canonical_json():
    value = {"b": 1, "a": ["é", None]}
    assert canonical(value) == '{"a":["é",null],"b":1}'
    assert digest(value) == digest({"a": ["é", None], "b": 1})
    assert len(digest(value)) == 64


def stored(**changes):
    base = content(desired().items["TASK::A::1"], {"STORY::A::ONE": "PRJ-2"}, None)
    return base | changes


def test_no_change_gives_no_items_and_a_version_bump_gives_none():
    assert rules.changes(stored(), stored()) == []
    assert rules.changes(stored(v=0), stored()) == []


def test_every_change_is_worded_as_in_the_spec_and_in_order():
    old = stored(
        done_when=["a", "b"],
        specs=[{"id": "S1", "name": "One"}],
        labels=["specflow"],
        blocked_by=["N1"],
        text="old text",
    )
    new = stored(
        summary="New title",
        text="new text",
        done_when=["b", "c"],
        release="R2",
        phase="data",
        specs=[{"id": "S2", "name": "Two"}],
        task_id="TASK::A::1_R2",
        points=5,
        labels=["area-a", "specflow"],
        parent="PRJ-8",
        blocked_by=["N2"],
    )
    assert rules.changes(old, new, {"N1": "PRJ-5", "N2": "PRJ-6"}) == [
        'Summary: "Task TASK::A::1" → "New title"',
        ("Description changed. New text:", "new text"),
        'Done when: added "c"',
        'Done when: removed "a"',
        "Release: none → R2",
        "Phase: logic → data",
        "Implements: added Two (S2)",
        "Implements: removed One (S1)",
        "Task id: TASK::A::1 → TASK::A::1_R2",
        "Story points: 1 → 5",
        "Label: added area-a",
        "Parent: PRJ-2 → PRJ-8",
        "Blocked by: added PRJ-6",
        "Blocked by: removed PRJ-5",
    ]


def test_a_blocked_by_node_without_a_known_key_keeps_its_node_id():
    assert rules.changes(stored(), stored(blocked_by=["N9"])) == ["Blocked by: added N9"]


def test_a_removed_description_is_worded_and_no_stored_content_compares_with_an_empty_one():
    assert rules.changes(stored(text="x"), stored(text=None)) == ["Description changed. New text: none"]
    items = rules.changes(None, stored())
    assert items[0] == 'Summary: "" → "Task TASK::A::1"'
    assert "Label: added specflow" in items and "Task id: none → TASK::A::1" in items


@pytest.mark.parametrize(
    ("event", "status", "text"),
    [
        (
            rules.CHANGED,
            "In Progress",
            'This issue is in status "In Progress", so Specflow left its fields as they are.',
        ),
        (rules.REMOVED, None, "This item was removed from the Specflow plan."),
        (rules.REVIVED, None, "This item is back in the Specflow plan."),
    ],
)
def test_the_comment_lead(event, status, text):
    assert rules.lead("TASK::A::1", event, status) == f"Specflow plan update for TASK::A::1. {text}"
