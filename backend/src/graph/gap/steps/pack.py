"""The context pack of one site, built with no model call (docs/graph/gap/investigation.md "Context pack"; FR-GINV-04).

Parts, in ``SitePack.context``:

- ``anchors``: every ``ABOUT`` and ``IMPACTS`` candidate of the site (the seeds' nodes, then their context nodes): spec
  id, label, name, attributes and the spine owners;
- ``evidence``: each anchor's source lines with two lines around them (from the run's graph snapshot);
- ``trace``: the core ``trace`` from the site's nodes in both directions, depth 2;
- ``gaps``: the open gaps on the anchors (id, kind, status, question);
- ``plan``: when the run has a ``PlanView``, the tasks implementing the anchors with their status;
- ``nearby``: the core ``expand`` from the anchors, within the pack budget the other parts left
  (``limits.pack_tokens`` minus the other parts, at least ``EXPAND_MIN_TOKENS``; none when they used it up).

Every id the pack shows is marked on the site's reads (``GraphSiteReads.show``), so the check step knows which ids the
investigator saw. A read the store refuses leaves its part empty (logged by code); it never fails the site.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from graph.core.errors import GraphError
from graph.core.llm.gateway import estimate_tokens
from graph.gap.gaps import StoredGap
from graph.gap.models import PackSeed, SitePack
from graph.gap.names import RunMode
from graph.gap.ports import PlanView
from graph.gap.settings import GapLimits
from graph.gap.steps.reads import GraphSiteReads
from graph.gap.steps.sites import Site
from graph.gap.steps.snapshot import GraphSnapshot, Line
from logger.logger import WARNING, get_logger, log_event

logger = get_logger(__name__)

CONTEXT_LINES: Final = 2
TRACE_DEPTH: Final = 2
TRACE_LIMIT: Final = 40
EXPAND_MIN_TOKENS: Final = 100
"""The core ``expand`` refuses a smaller budget."""
DIRECTIONS: Final = ("based_on", "affects")
OPEN_STATUSES: Final = frozenset({"open", "answered", "applying"})


def pack_seeds(site: Site) -> tuple[PackSeed, ...]:
    return tuple(
        PackSeed(
            detector=seed.detector,
            kind=seed.kind,
            strength=seed.strength,
            spec_ids=seed.about,
            context_ids=seed.context,
            witness=seed.witness,
        )
        for seed in site.seeds
    )


def _document_lines(snapshot: GraphSnapshot, document_id: str) -> dict[int, Line]:
    lines: dict[int, Line] = {}
    for fragment in snapshot.fragments.values():
        if fragment.document_id != document_id:
            continue
        for line in snapshot.fragment_lines(fragment.fragment_id):
            if line.line_start is not None:
                lines[line.line_start] = line
    return lines


def _evidence(snapshot: GraphSnapshot, anchors: Sequence[str]) -> list[dict[str, Any]]:
    """Each anchor's evidence lines with ``CONTEXT_LINES`` lines around them (document line numbers)."""

    by_document: dict[str, dict[int, Line]] = {}
    found: list[dict[str, Any]] = []
    for spec_id in anchors:
        node = snapshot.nodes[spec_id]
        for line in snapshot.lines(node):
            fragment = snapshot.fragments[line.fragment_id]
            if line.line_start is None:
                found.append({"spec_id": spec_id, "fragment_id": line.fragment_id, "lines": [[None, line.text]]})
                continue
            document = by_document.setdefault(fragment.document_id, _document_lines(snapshot, fragment.document_id))
            end = line.line_end if line.line_end is not None else line.line_start
            numbers = range(line.line_start - CONTEXT_LINES, end + CONTEXT_LINES + 1)
            around = [[number, document[number].text] for number in numbers if number in document]
            found.append({"spec_id": spec_id, "fragment_id": line.fragment_id, "lines": around or [[None, line.text]]})
    return found


def _anchor(snapshot: GraphSnapshot, spec_id: str, role: str) -> dict[str, Any]:
    node = snapshot.nodes[spec_id]
    return {
        "spec_id": spec_id,
        "role": role,
        "label": node.label,
        "name": node.name,
        "attributes": {name: value for name, value in sorted(node.attributes.items()) if value not in (None, "")},
        "owners": sorted(snapshot.spine_owners(spec_id)),
    }


async def _trace(reader: Any, project_id: str, anchors: Sequence[str], run_id: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for direction in DIRECTIONS:
        try:
            result = await reader.trace(project_id, list(anchors), direction=direction, depth=TRACE_DEPTH)
        except GraphError as error:
            log_event(logger, WARNING, "graph.gap.pack.read_failed", run_id=run_id, read="trace", code=error.code)
            continue
        found += [
            {
                "direction": direction,
                "from": node.parent,
                "relation": node.relation,
                "role": node.role,
                "spec_id": node.spec_id,
                "label": node.label,
                "name": node.name,
                "hops": node.hops,
            }
            for node in list(result.nodes)[:TRACE_LIMIT]
        ]
    return found


async def _nearby(run: PackRun, anchors: Sequence[str], *, exclude: Sequence[str], budget: int) -> list[dict[str, Any]]:
    if budget < EXPAND_MIN_TOKENS or not anchors:
        return []
    try:
        expansion = await run.reader.expand(run.project_id, list(anchors), exclude=list(exclude), budget_tokens=budget)
    except GraphError as error:
        log_event(logger, WARNING, "graph.gap.pack.read_failed", run_id=run.run_id, read="expand", code=error.code)
        return []
    return [
        {"spec_id": item.spec_id, "label": item.label, "name": item.name, "text": item.text} for item in expansion.items
    ]


def _gaps(gaps: Sequence[StoredGap], anchors: Sequence[str]) -> list[dict[str, Any]]:
    wanted = set(anchors)
    return [
        {
            "gap_id": gap.gap_id,
            "kind": gap.props.get("kind"),
            "status": gap.status,
            "question": str(gap.props.get("question") or ""),
            "about": sorted(set(gap.about_ids) & wanted),
        }
        for gap in gaps
        if gap.status in OPEN_STATUSES and set(gap.about_ids) & wanted
    ]


async def _plan(plan: PlanView | None, project_id: str, anchors: Sequence[str]) -> list[dict[str, Any]]:
    if plan is None or not anchors:
        return []
    tasks = await plan.tasks_for(project_id, list(anchors))
    return [
        {
            "spec_id": spec_id,
            "tasks": [{"task_id": ref.task_id, "story_id": ref.story_id, "status": ref.status} for ref in refs],
        }
        for spec_id, refs in sorted(tasks.items())
        if refs
    ]


def pack_tokens(context: Mapping[str, Any], seeds: Sequence[PackSeed]) -> int:
    """The estimated tokens of a pack (its parts as compact JSON)."""

    payload = {"seeds": [seed.model_dump(mode="json") for seed in seeds], **context}
    return estimate_tokens(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))


@dataclass(frozen=True)
class PackRun:
    """What the pack of a site needs of the run: the core reader, the ids, the limits, the gaps and the plan."""

    reader: Any
    project_id: str
    run_id: str
    mode: RunMode
    limits: GapLimits
    gaps: Sequence[StoredGap] = ()
    plan: PlanView | None = None


async def build_pack(site: Site, snapshot: GraphSnapshot, reads: GraphSiteReads, run: PackRun) -> SitePack:
    """The context pack of ``site`` (module docstring); marks every id it shows on ``reads``."""

    seeds = pack_seeds(site)
    about = [spec_id for spec_id in dict.fromkeys(site.about) if spec_id in snapshot.nodes]
    context_ids = [
        spec_id for spec_id in dict.fromkeys(site.context) if spec_id in snapshot.nodes and spec_id not in about
    ]
    anchors = [*about, *context_ids]
    context: dict[str, Any] = {
        "anchors": [_anchor(snapshot, spec_id, "about") for spec_id in about]
        + [_anchor(snapshot, spec_id, "context") for spec_id in context_ids],
        "evidence": _evidence(snapshot, anchors),
        "trace": await _trace(run.reader, run.project_id, about, run.run_id) if about else [],
        "gaps": _gaps(run.gaps, anchors),
        "plan": await _plan(run.plan, run.project_id, anchors),
    }
    shown = {*anchors, *(item["spec_id"] for item in context["trace"])}
    budget = run.limits.pack_tokens - pack_tokens(context, seeds)
    context["nearby"] = await _nearby(run, anchors, exclude=sorted(shown), budget=budget)
    shown |= {item["spec_id"] for item in context["nearby"]}
    fragments = {item["fragment_id"] for item in context["evidence"]} | {
        item.fragment_id for seed in seeds for item in seed.witness if item.fragment_id
    }
    reads.show(spec_ids=shown, fragment_ids=fragments)
    return SitePack(
        project_id=run.project_id,
        site_id=site.site_id,
        input_hash=site.input_hash,
        mode=run.mode,
        seeds=seeds,
        context=context,
    )
