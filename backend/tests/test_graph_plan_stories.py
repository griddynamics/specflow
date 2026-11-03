"""Stories, home stories, chapters, releases and retirement."""

from __future__ import annotations

from datetime import UTC, datetime

from graph.plan.models import ChapterRef, CurrentPlan, StoryInfo, StoryPlan
from graph.plan.partition import partition, planning_parts
from graph_plan_support import plan_ontology, plan_rules, shop_partition, shop_snapshot

CREATED = datetime(2026, 1, 1, 12, tzinfo=UTC)
UPDATED = datetime(2026, 1, 2, 12, tzinfo=UTC)


async def test_shop_stories_and_home_stories_are_created_without_a_model() -> None:
    artifact = await shop_partition()

    assert {story.story_id for story in artifact.stories if story.kind in {"feature", "interface", "entity"}} >= {
        "STORY::ORDERS_SERVICE::ORDERS_API",
        "STORY::ORDERS_DATABASE::ORDER",
        "STORY::ORDERS_SERVICE::PLACE_ORDER",
        "STORY::ORDERS_SERVICE::ORDER_HISTORY_EXPORT",
        "STORY::PAYMENTS_SERVICE::CARD_PAYMENT",
        "STORY::WEB_SHOP::CHECKOUT",
    }
    assert {
        "STORY::ORDERS_SERVICE::CORE",
        "STORY::PROJECT::SETUP",
        "STORY::PROJECT::CLOSING",
    } <= {story.story_id for story in artifact.stories}
    assert artifact.home_story["IFC::ORDERS_API"] == "STORY::ORDERS_SERVICE::ORDERS_API"
    assert artifact.home_story["REQ::FR::PLACE_ORDER"] == "STORY::ORDERS_SERVICE::PLACE_ORDER"
    assert artifact.home_story["REQ::DATA::ORDER_FIELDS"] == "STORY::ORDERS_DATABASE::ORDER"
    assert artifact.home_story["REQ::BR::ORDER_LIMIT"] == "STORY::ORDERS_SERVICE::CORE"
    assert artifact.home_story["CON::EU_REGION"].startswith("STORY::PROJECT::CH_")
    active_story_ids = {story.story_id for story in artifact.stories if story.status == "active"}
    assert artifact.home_story.keys() == {spec_id for spec_id in artifact.placements if not spec_id.startswith("MOD::")}
    assert set(artifact.home_story.values()) <= active_story_ids
    assert artifact.home_story["CAP::AUTHENTICATION"] == "STORY::PROJECT::SETUP"
    assert artifact.home_story["CAP::UNREALISED"] == "STORY::PROJECT::SETUP"
    assert artifact.home_story["REQ::FR::A"] == "STORY::PROJECT::SETUP"
    assert artifact.home_story["REQ::FR::B"] == "STORY::PROJECT::SETUP"
    assert artifact.home_story["REQ::FR::ORPHAN"] == "STORY::PROJECT::SETUP"


async def test_anchor_stories_contain_their_anchor_members_and_releases_are_inherited() -> None:
    artifact = await shop_partition()

    place_order = next(story for story in artifact.stories if story.anchor == "FEAT::PLACE_ORDER")
    assert set(place_order.members) >= {"FEAT::PLACE_ORDER", "REQ::FR::PLACE_ORDER"}
    assert place_order.release == "MVP"
    assert artifact.home_story["REQ::FR::PLACE_ORDER"] == place_order.story_id


async def test_story_titles_releases_and_ordinals_are_deterministic() -> None:
    artifact = await shop_partition()

    orders_api = next(story for story in artifact.stories if story.story_id == "STORY::ORDERS_SERVICE::ORDERS_API")
    assert orders_api.title == "Orders API"
    assert orders_api.release is None
    place_order = next(story for story in artifact.stories if story.story_id == "STORY::ORDERS_SERVICE::PLACE_ORDER")
    assert place_order.release == "MVP"
    ordinals = [
        story.ordinal
        for story in sorted(artifact.stories, key=lambda story: story.story_id)
        if story.area_id == orders_api.area_id
    ]
    assert ordinals == sorted(ordinals)
    assert partition(
        await shop_snapshot(),
        plan_rules(plan_ontology()),
        plan_ontology(),
    ) == partition(await shop_snapshot(), plan_rules(plan_ontology()), plan_ontology())


async def test_a_persisted_anchor_story_returns_under_its_old_id() -> None:
    snapshot = await shop_snapshot()
    persisted = CurrentPlan(
        plan=None,
        stories=(
            StoryInfo(
                story_id="STORY::ORDERS_SERVICE::OLD_FEATURE",
                area_id="AREA::ORDERS_SERVICE",
                kind="feature",
                anchor="FEAT::OLD",
                title="Old feature",
                status="active",
                created_at=CREATED,
                updated_at=UPDATED,
            ),
        ),
    )
    snapshot = snapshot.model_copy(update={"current": persisted})

    retired = partition(snapshot, plan_rules(plan_ontology()), plan_ontology())
    assert (
        next(story for story in retired.stories if story.story_id == "STORY::ORDERS_SERVICE::OLD_FEATURE").status
        == "retired"
    )

    nodes = list(snapshot.plannable)
    feature = next(node for node in nodes if node.spec_id == "FEAT::PLACE_ORDER")
    nodes.append(feature.model_copy(update={"spec_id": "FEAT::OLD", "uuid": "node-0104", "name": "Old feature"}))
    snapshot = snapshot.model_copy(update={"plannable": tuple(nodes)})
    active = partition(snapshot, plan_rules(plan_ontology()), plan_ontology())

    assert (
        next(story for story in active.stories if story.story_id == "STORY::ORDERS_SERVICE::OLD_FEATURE").status
        == "active"
    )


async def test_chapter_story_ids_are_truncated_and_stable() -> None:
    snapshot = await shop_snapshot()
    document = "A Very Long Document Title That Definitely Exceeds Every Limit"
    heading = "A Very Long Chapter Heading That Also Exceeds Every Possible Limit"
    nodes = list(snapshot.plannable)
    requirement = next(node for node in nodes if node.spec_id == "REQ::FR::ORPHAN")
    requirement = requirement.model_copy(
        update={
            "chapter": ChapterRef(document=document, heading=heading),
            "provenance": (),
        }
    )
    nodes = [node for node in nodes if node.spec_id != "REQ::FR::ORPHAN"] + [requirement]
    snapshot = snapshot.model_copy(update={"plannable": tuple(nodes)})

    artifact = partition(snapshot, plan_rules(plan_ontology()), plan_ontology())

    chapter = next(story for story in artifact.stories if story.kind == "chapter")
    assert len(chapter.story_id) <= 60
    assert chapter.story_id == next(
        story.story_id
        for story in partition(snapshot, plan_rules(plan_ontology()), plan_ontology()).stories
        if story.kind == "chapter"
    )


async def test_core_and_chapter_stories_with_no_members_are_retired() -> None:
    snapshot = await shop_snapshot()
    persisted = CurrentPlan(
        plan=None,
        stories=(
            StoryInfo(
                story_id="STORY::ORDERS_SERVICE::CORE",
                area_id="AREA::ORDERS_SERVICE",
                kind="core",
                title="Core",
                status="active",
                created_at=CREATED,
                updated_at=UPDATED,
            ),
            StoryInfo(
                story_id="STORY::PROJECT::CH_SHOP_REQUIREMENTS_QUALITY",
                area_id="AREA::PROJECT",
                kind="chapter",
                chapter={"document": "Shop requirements", "heading": "Quality"},
                title="Quality",
                status="active",
                created_at=CREATED,
                updated_at=UPDATED,
            ),
        ),
    )
    nodes = tuple(
        node
        for node in snapshot.plannable
        if node.spec_id not in {"BR::RELEASE", "REQ::BR::ORDER_LIMIT", "CON::EU_REGION"}
    )
    snapshot = snapshot.model_copy(update={"current": persisted, "plannable": nodes})

    artifact = partition(snapshot, plan_rules(plan_ontology()), plan_ontology())

    assert (
        next(story for story in artifact.stories if story.story_id == "STORY::ORDERS_SERVICE::CORE").status == "retired"
    )
    assert (
        next(
            story for story in artifact.stories if story.story_id == "STORY::PROJECT::CH_SHOP_REQUIREMENTS_QUALITY"
        ).status
        == "retired"
    )


def _story(area: str, slug: str, size: int) -> StoryPlan:
    return StoryPlan(
        story_id=f"STORY::{area}::{slug}",
        area_id=f"AREA::{area}",
        kind="feature",
        anchor=f"FEAT::{slug}",
        title=slug,
        members=tuple(f"REQ::FR::{slug}_{index:03d}" for index in range(size)),
        ordinal=1,
        status="active",
    )


def test_small_stories_are_packed_and_only_a_large_story_is_split() -> None:
    stories = [_story("ORDERS", slug, size) for slug, size in (("A", 10), ("B", 15), ("C", 30), ("D", 90))]

    parts = planning_parts(stories, 40)

    assert [part.story_ids for part in parts] == [
        ("STORY::ORDERS::A", "STORY::ORDERS::B"),
        ("STORY::ORDERS::C",),
        ("STORY::ORDERS::D",),
        ("STORY::ORDERS::D",),
        ("STORY::ORDERS::D",),
    ]
    assert [len(part.members) for part in parts] == [25, 30, 40, 40, 10]
    assert [part.part_id for part in parts] == [f"AREA::ORDERS#{number}" for number in range(1, 6)]
    split = [member for part in parts[2:] for member in part.members]
    assert split == sorted(split)


def test_an_area_within_the_limit_is_one_part_and_parts_are_numbered_per_area() -> None:
    stories = [_story("ORDERS", "A", 10), _story("ORDERS", "B", 20), _story("WEB", "C", 5)]

    parts = planning_parts(stories, 40)

    assert [(part.part_id, part.story_ids) for part in parts] == [
        ("AREA::ORDERS#1", ("STORY::ORDERS::A", "STORY::ORDERS::B")),
        ("AREA::WEB#1", ("STORY::WEB::C",)),
    ]


async def test_shop_parts_cover_every_planned_story_and_never_setup_or_closing() -> None:
    artifact = await shop_partition()

    part_stories = {story_id for part in artifact.parts for story_id in part.story_ids}
    planned = {
        story.story_id
        for story in artifact.stories
        if story.status == "active" and story.members and story.kind not in {"setup", "closing"}
    }
    assert part_stories == planned
    assert not any(story_id.endswith(("::SETUP", "::CLOSING")) for story_id in part_stories)
    assert sorted(member for part in artifact.parts for member in part.members) == sorted(
        member for story in artifact.stories if story.story_id in planned for member in story.members
    )
