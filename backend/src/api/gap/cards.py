"""The gap cards of the agent routes (docs/mcp-server/apps.md "Terminal dialogs"): a gap that waits for a person's
answer as the ready text a coding agent shows in the chat before the answer dialog, with its parts and the link to its
card in the web app; a gap in any status whole, as ``open_gap`` shows it (``gap_detail``: the web card's parts with the
recorded answer, the edits of each option, the history and the reach); a gap as a row of ``browse_gaps``.

Gap text comes from the project's documents and people: it is copied as data, never interpreted. Nothing here logs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

from agent_contracts import (
    GAP_STAGES,
    GapAnswerView,
    GapCard,
    GapCardOption,
    GapCardQuote,
    GapDetail,
    GapDetailOption,
    GapEdit,
    GapFollowUp,
    GapHistoryItem,
    GapNode,
    GapRow,
    GapStage,
    Holder,
)
from api.agent_views import Holders
from graph.gap import ClarificationView, Edit, EventRow, GapView, NodeRef, OptionRow, WitnessView
from graph.gap.names import AI_ACTOR

ORIGINS: Final = {
    "analysis": "found by analysis",
    "development": "reported by a coding agent",
    "person": "raised by a person",
}
OWN_ANSWER: Final = "- **Other**: your own answer"
NO_OPTIONS: Final = "**Options:** being prepared; you can give your own answer now."
NOBODY: Final = "nobody"


def web_url(public_url: str | None, project_id: str) -> str | None:
    """The project's canvas in the web app — the gap query is gone with the legacy Gaps screen, and the canvas has no
    gap-card equivalent (D10) — or None without the web app's address."""

    if public_url is None:
        return None
    return f"{public_url}/projects/{project_id}"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _lines(start: int | None, end: int | None) -> str:
    if start is None:
        return ""
    if end is None or end == start:
        return f", line {start}"
    return f", lines {start}-{end}"


def _quote(witness: WitnessView) -> GapCardQuote:
    document = witness.document_path or witness.document_title or "a document"
    section = f', "{_one_line(witness.heading_path[-1])}"' if witness.heading_path else ""
    return GapCardQuote(
        source=f"{document}{section}{_lines(witness.line_start, witness.line_end)}", text=_one_line(witness.quote)
    )


def _option(index: int, option: OptionRow) -> GapCardOption:
    code = chr(ord("A") + index)
    recommended = " (recommended)" if option.recommended else ""
    amends = any(edit.placement == "amend" for edit in option.edits)
    return GapCardOption(
        code=code,
        rank=option.rank,
        title=f"{code}) {_one_line(option.label)}{recommended}",
        label=option.label,
        detail=_one_line(option.detail),
        recommended=option.recommended,
        placement="amend" if amends else "document",
    )


def assignee_text(assignee: Holder | None) -> str:
    """Who the gap is assigned to, in words: "you", the member's name, "a member" (a name that is an address), or
    "nobody"."""

    if assignee is None:
        return NOBODY
    if assignee.you:
        return "you"
    return _one_line(assignee.name) if assignee.name else "a member"


def card_text(card: GapCard) -> str:
    """The card as the chat shows it, nothing folded: title and chips, the assignee, question, why, the options with
    their details, the quoted lines and the web link."""

    origin = ORIGINS.get(card.origin, card.origin)
    parts = [
        f"**{_one_line(card.title)}** ({card.severity} {card.kind.replace('_', ' ')}, {origin}, `{card.gap_id}`)",
        f"**Assigned to:** {assignee_text(card.assignee)}",
        f"**Question:** {_one_line(card.question)}",
    ]
    if card.why.strip():
        parts.append(f"**Why it matters:** {_one_line(card.why)}")
    if card.options:
        lines = [
            f"- **{option.title}**: {option.detail}" if option.detail else f"- **{option.title}**"
            for option in card.options
        ]
        parts.append("**Options:**\n" + "\n".join([*lines, OWN_ANSWER]))
    else:
        parts.append(NO_OPTIONS)
    if card.quotes:
        parts.append("**Quoted lines:**\n" + "\n".join(f'> {quote.source}: "{quote.text}"' for quote in card.quotes))
    if card.web_url is not None:
        parts.append(f"Web card: {card.web_url}")
    return "\n\n".join(parts)


def gap_card(gap: GapView, project_id: str, public_url: str | None, assignee: Holder | None = None) -> GapCard:
    """The card of a gap read whole (``GapService.get_gap``: options, quoted lines with their documents); ``assignee``
    is the member the gap is assigned to, as the caller sees them."""

    options = [_option(index, option) for index, option in enumerate(sorted(gap.options, key=lambda row: row.rank))]
    card = GapCard(
        gap_id=gap.gap_id,
        severity=gap.severity,
        kind=gap.kind,
        origin=gap.origin,
        title=gap.title,
        question=gap.question,
        why=gap.why,
        quotes=[_quote(witness) for witness in gap.witness],
        options=options,
        updated_at=gap.updated_at,
        web_url=web_url(public_url, project_id),
        assignee=assignee,
        card="",
    )
    return card.model_copy(update={"card": card_text(card)})


def gap_row(gap: GapView, assignee: Holder | None) -> GapRow:
    """A gap as ``browse_gaps`` lists it: one row of the table."""

    return GapRow(
        gap_id=gap.gap_id,
        title=_one_line(gap.title),
        severity=gap.severity,
        kind=gap.kind,
        status=stage(gap.status),
        resolving=gap.status == APPLYING,
        assignee=assignee,
    )


def stage(status: str) -> GapStage:
    """What people see of a stored status: open, staged (answered or applying) or resolved."""

    return next(name for name, statuses in GAP_STAGES.items() if status in statuses)


# --- open_gap: one gap whole, as its web card shows it --------------------------------------------------------------

OPEN: Final = "open"
APPLYING: Final = "applying"
SPECFLOW: Final = "SpecFlow"
# The history in words, as the web card's history says it (frontend gaps-logic.ts "history").
EVENT_WORDS: Final = {
    "created": "Gap found",
    "raised": "Gap raised",
    "routed": "Routed",
    "answered": "Answered",
    "replaced": "Answer replaced",
    "applying": "Answer sent to the build",
    "applied": "Answer applied",
    "resolved": "Resolved",
    "reverted": "Answer withdrawn",
    "reconciled": "Checked against a new source revision",
    "dismissed": "Dismissed",
    "not_grounded": "The build did not find the answer",
    "cite_removed": "A quoted line left the source",
    "follow_up": "Follow-up gap opened",
    "reopened": "Reopened",
    "apply_failed": "Applying failed",
    "assigned": "Assigned",
    "unassigned": "Unassigned",
}
OUTCOME_WORDS: Final = {
    "applied": "applied to the new source revision",
    "reanchored": "moved to a changed line",
    "absorbed": "found in the source file",
    "conflict": "in conflict with the source file",
    "orphaned": "its section is gone from the source",
    "undecided": "not decided yet",
}
CLARIFICATION_WORDS: Final = {
    "pending": "waiting to be applied",
    "applied": "applied",
    "absorbed": "in the source file",
    "conflict": "in conflict with the source",
    "orphaned": "its section is gone",
    "reverted": "reverted",
    "superseded": "replaced by a newer answer",
}
OPERATION_WORDS: Final = {"replace": "replace it with", "insert_after": "add after it", "append_section": "add"}


def detail_user_ids(gap: GapView) -> list[str | None]:
    """The user ids the detail of ``gap`` names: the assignee, who answered, who did each event and whom an event
    assigned."""

    events = gap.history.events if gap.history is not None else ()
    answered_by = gap.clarification.answered_by if gap.clarification is not None else None
    assigned = [event.detail.get("user_id") for event in events]
    actors = [event.actor for event in events]
    return [gap.assigned_to, answered_by, *actors, *(user for user in assigned if isinstance(user, str))]


def _is_user(actor: str) -> bool:
    try:
        uuid.UUID(actor)
    except ValueError:
        return False
    return True


def _actor(actor: str, holders: Holders) -> str:
    """Who did it, in words: the AI, a member (``you``, the name, ``a member``), else SpecFlow (a run, a build, the
    reconcile step)."""

    if actor == AI_ACTOR:
        return "AI"
    return assignee_text(holders.of(actor)) if _is_user(actor) else SPECFLOW


def _event(event: EventRow, holders: Holders) -> GapHistoryItem:
    name = event.event
    words = EVENT_WORDS.get(name) or (name[:1].upper() + name[1:]).replace("_", " ")

    def detail(key: str) -> str | None:
        value = event.detail.get(key)
        return value if isinstance(value, str) and value else None

    user, outcome, route, reason = detail("user_id"), detail("outcome"), detail("route"), detail("reason")
    if name == "assigned" and user is not None:
        words = f"{words} to {assignee_text(holders.of(user))}"
    elif name == "reconciled" and outcome is not None:
        words = f"{words}: {OUTCOME_WORDS.get(outcome, outcome.replace('_', ' '))}"
    elif name == "routed" and route is not None:
        words = f"{words} to {'AI' if route == 'ai' else 'people'}"
    elif name in ("dismissed", "reverted") and reason is not None:
        words = f"{words}: {_one_line(reason)}"
    return GapHistoryItem(at=event.at, event=name, text=words, by=_actor(event.actor, holders))


def _edit(edit: Edit) -> GapEdit:
    target = edit.target
    if edit.placement == "amend":
        where = f'the line "{_one_line(target.anchor_quote)}"' if target.anchor_quote else "the source section"
    else:
        heading = f', under "{_one_line(target.heading)}"' if target.heading else ""
        where = f"the clarifications document{heading}"
    return GapEdit(placement=edit.placement, where=where, operation=edit.operation, text=edit.text)


def _answer(clarification: ClarificationView | None, holders: Holders) -> GapAnswerView | None:
    """The recorded answer in words, as the web card shows it: the chosen option's label, else the own text, else the
    answering lines of a pointer."""

    if clarification is None:
        return None
    lines = " / ".join(_one_line(quote.quote) for quote in clarification.basis)
    if clarification.mode == "option" or (clarification.mode == "ai" and clarification.option_label):
        answer = clarification.option_label or clarification.text or lines
    else:
        answer = clarification.text or clarification.option_label or lines
    return GapAnswerView(
        mode=clarification.mode,
        answer=answer,
        placement=clarification.placement,
        status=clarification.status,
        answered_by=_actor(clarification.answered_by, holders),
        answered_at=clarification.answered_at,
        confidence=clarification.confidence,
    )


def _node(node: NodeRef) -> GapNode:
    return GapNode(spec_id=node.spec_id, type=node.label, name=node.name)


def _when(at: datetime) -> str:
    return f"{at.astimezone(UTC):%Y-%m-%d %H:%M} UTC"


def _nodes_text(nodes: list[GapNode]) -> str:
    return "; ".join(f"`{node.spec_id}` {_one_line(node.name)} ({node.type})" for node in nodes)


def _option_lines(option: GapDetailOption) -> list[str]:
    head = f"- **{option.title}**: {option.detail}" if option.detail else f"- **{option.title}**"
    edits = [
        f'  - Changes {edit.where}: {OPERATION_WORDS.get(edit.operation, edit.operation)} "{_one_line(edit.text)}"'
        for edit in option.edits
    ]
    return [head, *edits]


def _answer_text(answer: GapAnswerView) -> str:
    confidence = f", {round(answer.confidence * 100)}% confident" if answer.confidence is not None else ""
    status = CLARIFICATION_WORDS.get(answer.status, answer.status)
    return (
        f"**Answer:** {_one_line(answer.answer)} (by {answer.answered_by}{confidence}, {_when(answer.answered_at)}; "
        f"{status})"
    )


def _head(detail: GapDetail) -> list[str]:
    """Title and chips, status and assignee, question, why, the nodes, the quoted lines and the reach."""

    origin = ORIGINS.get(detail.origin, detail.origin)
    blocks = ", blocks work" if detail.blocking else ""
    resolving = " (resolving: the rebuild that applies it runs)" if detail.resolving else ""
    kind = detail.kind.replace("_", " ")
    parts = [
        f"**{_one_line(detail.title)}** ({detail.severity} {kind}, {origin}, `{detail.gap_id}`)",
        f"**Status:** {detail.status}{resolving}{blocks} · **Assigned to:** {assignee_text(detail.assignee)}",
        f"**Question:** {_one_line(detail.question)}",
    ]
    if detail.why.strip():
        parts.append(f"**Why it matters:** {_one_line(detail.why)}")
    if detail.about:
        parts.append(f"**About:** {_nodes_text(detail.about)}")
    if detail.impacts:
        parts.append(f"**Impacts:** {_nodes_text(detail.impacts)}")
    if detail.quotes:
        parts.append("**Quoted lines:**\n" + "\n".join(f'> {quote.source}: "{quote.text}"' for quote in detail.quotes))
    if detail.reach_stories or detail.reach_tasks:
        parts.append(
            f"**Reach:** {len(detail.reach_stories)} stories ({', '.join(detail.reach_stories) or 'none'}), "
            f"{len(detail.reach_tasks)} tasks ({', '.join(detail.reach_tasks) or 'none'})"
        )
    return parts


def _tail(detail: GapDetail) -> list[str]:
    """The options with their edits (and Other while the gap is open), the answer, the history and the follow-ups."""

    parts = []
    lines = [line for option in detail.options for line in _option_lines(option)]
    if lines:
        parts.append("**Options:**\n" + "\n".join([*lines, *([OWN_ANSWER] if detail.answerable else [])]))
    elif detail.answerable:
        parts.append(NO_OPTIONS)
    if detail.answer is not None:
        parts.append(_answer_text(detail.answer))
    if detail.history:
        events = [f"- {_when(item.at)}: {item.text} ({item.by})" for item in detail.history]
        parts.append("**History:**\n" + "\n".join(events))
    if detail.follow_ups:
        chain = [
            f"{step.direction.replace('_', ' ')} `{step.gap_id}` ({step.reason.replace('_', ' ')})"
            for step in detail.follow_ups
        ]
        parts.append(f"**Follow-ups:** {'; '.join(chain)}")
    return parts


def detail_text(detail: GapDetail) -> str:
    """The whole gap as the chat shows it, nothing folded, in the order of the web card (``_head``, ``_tail``), then
    the web link."""

    link = [f"Web card: {detail.web_url}"] if detail.web_url is not None else []
    return "\n\n".join([*_head(detail), *_tail(detail), *link])


def gap_detail(gap: GapView, project_id: str, public_url: str | None, holders: Holders) -> GapDetail:
    """A gap read whole (``GapService.get_gap``: options, history, reach, quoted lines with their documents), with the
    members it names as the caller sees them (``holders`` of ``detail_user_ids``)."""

    ranked = sorted(gap.options, key=lambda row: row.rank)
    options = [
        GapDetailOption(**_option(index, option).model_dump(), edits=[_edit(edit) for edit in option.edits])
        for index, option in enumerate(ranked)
    ]
    history = gap.history.events if gap.history is not None else ()
    follow_ups = gap.history.follow_ups if gap.history is not None else ()
    detail = GapDetail(
        project_id=project_id,
        gap_id=gap.gap_id,
        title=gap.title,
        severity=gap.severity,
        kind=gap.kind,
        origin=gap.origin,
        status=stage(gap.status),
        resolving=gap.status == APPLYING,
        blocking=gap.blocking,
        question=gap.question,
        why=gap.why,
        about=[_node(node) for node in gap.about],
        impacts=[_node(node) for node in gap.impacts],
        quotes=[_quote(witness) for witness in gap.witness],
        options=options,
        answer=_answer(gap.clarification, holders),
        history=[_event(event, holders) for event in sorted(history, key=lambda event: event.at)],
        follow_ups=[
            GapFollowUp(gap_id=step.gap_id, reason=step.reason, direction=step.direction) for step in follow_ups
        ],
        reach_stories=list(gap.reach.stories) if gap.reach is not None else [],
        reach_tasks=list(gap.reach.tasks) if gap.reach is not None else [],
        assignee=holders.of(gap.assigned_to),
        answerable=gap.status == OPEN,
        updated_at=gap.updated_at,
        web_url=web_url(public_url, project_id),
        card="",
    )
    return detail.model_copy(update={"card": detail_text(detail)})
