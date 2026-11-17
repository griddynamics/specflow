"""Blocker holds and their release (``graph.gap.holds``; docs/graph/gap/lifecycle.md "Plan link", decision GD2)."""

from __future__ import annotations

from gap_port_fakes import MemoryStoryHolds
from graph.gap.gaps import StoredGap, StoredLink
from graph.gap.holds import aliases_of, hold, hold_blockers, hold_reason, release
from graph.plan.errors import TaskStateError


def test_the_block_reason_follows_the_gap_kind() -> None:
    assert [hold_reason(kind) for kind in ("missing", "contradiction", "inconsistency", "open_question", None)] == [
        "spec_missing",
        "spec_contradiction",
        "spec_contradiction",
        "spec_ambiguous",
        "spec_ambiguous",
    ]


def test_aliases_are_the_listed_ids_only() -> None:
    assert aliases_of({"aliases": ["FND::1", ""]}) == ("FND::1",)
    assert aliases_of({"aliases": "FND::1"}) == () and aliases_of({}) == ()


async def test_a_hold_asks_the_plan_with_the_kinds_reason_and_nothing_without_a_port_or_nodes() -> None:
    holds = MemoryStoryHolds({"REQ::A": ("TASK::1", "TASK::2")}, in_progress=("TASK::2",))

    held = await hold(holds, "p", "GAP::A", ["REQ::A"], "contradiction")

    assert (held.held, held.in_progress) == (("TASK::1",), ("TASK::2",))
    assert holds.reasons[("p", "GAP::A")] == "spec_contradiction"
    assert (await hold(None, "p", "GAP::A", ["REQ::A"], "missing")).held == ()
    assert (await hold(holds, "p", "GAP::B", [], "missing")).held == ()


async def test_a_release_ends_the_holds_of_the_gap_and_of_its_aliases() -> None:
    holds = MemoryStoryHolds()
    holds.holds[("p", "GAP::A")] = ("TASK::1",)
    holds.holds[("p", "FND::OLD")] = ("TASK::2",)  # a migrated finding's id the plan's blocked tasks still name

    released = await release(holds, "p", "GAP::A", ["FND::OLD", "GAP::A"])

    assert released == ("TASK::1", "TASK::2")
    assert holds.released == [("p", "GAP::A"), ("p", "FND::OLD")]
    assert await release(None, "p", "GAP::A") == ()


async def test_a_release_the_plan_refuses_is_logged_and_left(caplog) -> None:
    class Busy(MemoryStoryHolds):
        async def release_gap(self, project_id: str, gap_id: str) -> tuple[str, ...]:
            raise TaskStateError("plan_applying", project_id=project_id, gap_id=gap_id)

    assert await release(Busy(), "p", "GAP::A") == ()
    assert "graph.gap.release_failed" in caplog.text


async def test_every_open_blocker_holds_its_tasks_and_no_other_gap_does() -> None:
    def gap(gap_id: str, severity: str, status: str = "open") -> StoredGap:
        link = StoredLink("ABOUT", "REQ::A", "u", "Requirement", "A", f"{gap_id}|ABOUT|REQ::A")
        return StoredGap(gap_id, {"severity": severity, "status": status, "kind": "missing"}, about=(link,))

    holds = MemoryStoryHolds({"REQ::A": ("TASK::1",)})
    gaps = [gap("GAP::B", "blocker"), gap("GAP::M", "major"), gap("GAP::R", "blocker", "resolved")]

    held = await hold_blockers(holds, "p", gaps)

    assert held.held == ("TASK::1",) and set(holds.holds) == {("p", "GAP::B")}
    assert (await hold_blockers(holds, "p", gaps, ["REQ::OTHER"])).held == ()
    assert (await hold_blockers(None, "p", gaps)).held == ()
