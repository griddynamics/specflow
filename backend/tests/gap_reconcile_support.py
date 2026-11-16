"""Re-uploads of ``orders-spec.md`` after an applied answer, for offline reconcile tests (docs/graph/gap/reconcile.md
"Worked examples"; README "Re-uploads of orders-spec.md").

``answered_and_resolved`` answers the cancel-paid-order gap with the worked example's text (a ``replace`` of the
question line), applies it, builds (the answer becomes ``REQ::FR::CANCEL_PAID_ORDER``) and runs the close check, so
the gap is ``resolved`` and its clarification ``applied``. ``reupload`` then builds a re-upload of the document with
reconcile in the corpus read (the judge is the recorded ``gap_judge`` gateway unless given) and runs the gap run that
follows it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from gap_acceptance_support import REUPLOADS, Planted, RecordedGapGateway
from gap_answer_support import MEMBER, ORDERS_LINEAGE, PLACE, AnswerWorld, amend, answer_world, service_of
from gap_close_support import CANCEL_EDGE, Rebuilt, answer_and_apply, apply_build, cancel_requirement, update_run
from graph.gap.gaps import read_gaps
from graph.gap.models import AnswerInput, EventRow, GapRunResult, GapView
from graph.gap.reconcile import Judge, gateway_judge

WORKED_ANSWER: Final = "A customer can cancel a paid order within 30 minutes of payment. The full amount is refunded."


@dataclass
class Reconciled:
    """A re-upload built and the gap run after it."""

    rig: AnswerWorld
    gap: GapView
    clr_id: str
    rebuilt: Rebuilt
    run: GapRunResult
    gateway: RecordedGapGateway | None

    def orders(self) -> str:
        return next(item.markdown for item in self.rebuilt.revisions if item.lineage_id == ORDERS_LINEAGE)

    async def events(self, event: str = "reconciled") -> list[EventRow]:
        found = await self.rig.world.records.events(self.rig.project_id, self.gap.gap_id)
        return [item for item in found if item.event == event]

    async def amendment_statuses(self) -> list[str]:
        rows = await self.rig.world.amendments.for_clarification(self.rig.project_id, self.clr_id)
        return [row.status for row in rows]

    async def clarification(self, clr_id: str | None = None) -> Mapping[str, Any]:
        """The clarification ``clr_id`` (default: the answer reconcile met) as the graph holds it."""

        wanted = clr_id or self.clr_id
        gaps = await read_gaps(self.rig.world.executor, self.rig.project_id, self.rig.world.ontology)
        return next(props for gap in gaps for props in gap.clarifications if props.get("clr_id") == wanted)

    async def follow_up(self) -> GapView:
        view = await self.rig.service.get_gap(self.rig.project_id, self.gap.gap_id)
        (link,) = [item for item in view.history.follow_ups if item.direction == "continued_by"]
        return await self.rig.service.get_gap(self.rig.project_id, link.gap_id)


async def answered_and_resolved() -> tuple[AnswerWorld, GapView, str]:
    """The cancel-paid-order gap answered with the worked example's text, applied, built and closed."""

    rig = await answer_world()
    gap = await rig.cancel_gap()
    edit = amend(PLACE, Planted.STATED_QUESTION_LINE, WORKED_ANSWER)
    result = await answer_and_apply(rig, gap, own_text=WORKED_ANSWER, edits=[edit])
    rebuilt = await apply_build(
        rig.world, remove=[Planted.STATED_QUESTION], add=[cancel_requirement(WORKED_ANSWER)], edges=[CANCEL_EDGE]
    )
    await update_run(rig.world, rebuilt)
    return rig, gap, result.clr_id


def upload_of(name: str) -> str:
    return next(case for case in REUPLOADS if case.name == name).markdown


async def reupload(
    name: str,
    *,
    judge: Judge | None = None,
    gateway: RecordedGapGateway | None = None,
    world: tuple[AnswerWorld, GapView, str] | None = None,
    **outcome: Any,
) -> Reconciled:
    """Build the re-upload ``name`` of orders-spec.md (reconciled; ``outcome`` is the extraction, as ``apply_build``
    takes it) and run the gap run after it."""

    rig, gap, clr = world or await answered_and_resolved()
    if judge is None:
        gateway = gateway or RecordedGapGateway()
        judge = gateway_judge(gateway, rig.world.records, rig.project_id, lambda: datetime.now(UTC))
    rebuilt = await apply_build(
        rig.world, build_id="build-3", uploads={ORDERS_LINEAGE: upload_of(name)}, judge=judge, **outcome
    )
    run = await update_run(rig.world, rebuilt)
    rig.service = service_of(rig.world, builds=rig.builds, holds=rig.holds, clock=rig.clock)[0]  # reads the new graph
    return Reconciled(rig, gap, clr, rebuilt, run, gateway)


async def answer_follow_up(done: Reconciled, follow_up: GapView, **answer: Any) -> Any:
    """Answer the follow-up gap as a member (``option_rank`` or ``own_text``)."""

    return await done.rig.service.answer_gap(
        done.rig.project_id, AnswerInput(gap_id=follow_up.gap_id, answered_by=MEMBER, **answer)
    )
