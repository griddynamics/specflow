"""The blocking link of a finding ([gap.md](gap.md) "Blocking link"): ``Findings`` composed with the plan's story calls.

``graph.gap`` never imports ``graph.plan``: the host hands ``FindingFlow`` its ``PlanService``, which satisfies the
structural ``StoryBlocks`` below. Order of ``add_finding`` with ``blocking``: match, guard, add, ``block_story``; when
the block fails the finding is withdrawn and the block's own error is raised (a finding never lives without its block).
Closing a blocking finding is followed by ``unblock_finding``: tasks a plan run already reset are untouched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Protocol

from graph.core.errors import ConfigurationError, GraphError
from graph.gap.errors import FindingError
from graph.gap.findings import Findings
from graph.gap.models import AddedFinding, Finding, FindingRef, GapModel
from graph.gap.names import BLOCK_REASONS
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("graph.gap")


class Released(Protocol):
    @property
    def released(self) -> Sequence[str]: ...


class StoryBlocks(Protocol):
    """What the blocking link needs of the plan: the guard, the block and the unblock."""

    async def check_block(
        self, project_id: str, story_id: str, *, claimed_by: str, claim_token: str, spec_ids: Sequence[str]
    ) -> None: ...

    async def block_story(
        self, project_id: str, story_id: str, *, claim_token: str, reason: Any, finding_id: str
    ) -> Released: ...

    async def unblock_finding(self, project_id: str, finding_id: str) -> Sequence[str]: ...


class NewFinding(GapModel):
    """The arguments of ``add_finding`` that describe the finding itself (``Findings.add`` takes the same)."""

    created_by: str
    kind: str
    text: str
    about: tuple[str, ...]
    refers_to: tuple[FindingRef | tuple[str, str], ...] = ()
    story_id: str | None = None
    blocking: bool = False


class FlowAdded(GapModel):
    """``add_finding``: the finding, whether it was created, and the task ids it blocked (none for a repeat)."""

    finding: Finding
    created: bool
    blocked: tuple[str, ...] = ()


class FlowClosed(GapModel):
    """``close_finding``: the closed finding and the task ids that returned to ``pending``."""

    finding: Finding
    unblocked: tuple[str, ...] = ()


class FindingFlow:
    """``Findings`` and the plan's story calls as the agent tools use them."""

    def __init__(self, findings: Findings, plan: StoryBlocks) -> None:
        self._findings = findings
        self._plan = plan

    async def add_finding(self, project_id: str, new: NewFinding, *, claim_token: str | None = None) -> FlowAdded:
        """Add a finding; with ``new.blocking`` also block the caller's claimed story (``claim_token``). Errors: those
        of ``Findings.add``, and the plan's ``claim_mismatch`` from the guard or the block."""

        if not new.blocking:
            added = await self._add(project_id, new)
            return FlowAdded(finding=added.finding, created=added.created)
        repeat = await self._findings.match(
            project_id, created_by=new.created_by, kind=new.kind, about=new.about, text=new.text
        )
        if repeat is not None:
            return FlowAdded(finding=repeat, created=False)
        reason = BLOCK_REASONS.get(new.kind)
        story_id = new.story_id
        if reason is None or story_id is None:
            raise ConfigurationError("invalid_kind" if reason is None else "invalid_story")
        token = claim_token or ""  # no token never matches: the guard answers ``claim_mismatch``
        await self._plan.check_block(
            project_id, story_id, claimed_by=new.created_by, claim_token=token, spec_ids=new.about
        )
        added = await self._add(project_id, new)
        if not added.created:  # another caller wrote the same finding between the match and the add
            return FlowAdded(finding=added.finding, created=False)
        finding_id = added.finding.finding_id
        try:
            released = await self._plan.block_story(
                project_id, story_id, claim_token=token, reason=reason, finding_id=finding_id
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._withdraw(project_id, finding_id))
            raise
        except Exception:
            await self._withdraw(project_id, finding_id)
            raise
        log_event(logger, INFO, "graph.gap.finding_blocked", finding_id=finding_id, tasks=len(released.released))
        return FlowAdded(finding=added.finding, created=True, blocked=tuple(released.released))

    async def close_finding(
        self, project_id: str, finding_id: str, *, by: str, note: str | None = None, dismiss: bool = False
    ) -> FlowClosed:
        """Resolve (or dismiss) a finding; a blocking one is followed by ``unblock_finding``. Closing a finding that is
        already closed raises ``wrong_state`` after repeating the unblock, so a lost unblock heals."""

        close = self._findings.dismiss if dismiss else self._findings.resolve
        try:
            closed = await close(project_id, finding_id, by=by, note=note)
        except FindingError as error:
            if error.code == "wrong_state":
                known = await self._findings.finding(project_id, finding_id)
                if known is not None and known.blocking:
                    await self._plan.unblock_finding(project_id, finding_id)
            raise
        unblocked = tuple(await self._plan.unblock_finding(project_id, finding_id)) if closed.blocking else ()
        return FlowClosed(finding=closed, unblocked=unblocked)

    async def _add(self, project_id: str, new: NewFinding) -> AddedFinding:
        return await self._findings.add(
            project_id,
            created_by=new.created_by,
            kind=new.kind,
            text=new.text,
            about=new.about,
            refers_to=new.refers_to,
            story_id=new.story_id,
            blocking=new.blocking,
        )

    async def _withdraw(self, project_id: str, finding_id: str) -> None:
        try:
            await self._findings.withdraw(project_id, finding_id)
        except GraphError as error:  # the block's own error is the one the caller needs
            log_event(
                logger,
                WARNING,
                "graph.gap.withdraw_failed",
                finding_id=finding_id,
                error_code=error.code,
            )
