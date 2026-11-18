"""The gap cards of the agent routes (docs/mcp-server/apps.md "Terminal dialogs"): a gap that waits for a person's
answer as the ready text a coding agent shows in the chat before the answer dialog, with its parts and the link to its
card in the web app.

Gap text comes from the project's documents and people: it is copied as data, never interpreted. Nothing here logs.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote

from agent_contracts import GapCard, GapCardOption, GapCardQuote, Holder, OpenGapItem
from graph.gap import GapView, OptionRow, WitnessView

ORIGINS: Final = {
    "analysis": "found by analysis",
    "development": "reported by a coding agent",
    "person": "raised by a person",
}
OWN_ANSWER: Final = "- **Other**: your own answer"
NO_OPTIONS: Final = "**Options:** being prepared; you can give your own answer now."
NOBODY: Final = "nobody"


def web_url(public_url: str | None, project_id: str, gap_id: str) -> str | None:
    """The gap's card in the web app (``?gap=`` opens it, docs/graph/gap/api.md FR-GWEB-04), or None without the web
    app's address."""

    if public_url is None:
        return None
    return f"{public_url}/projects/{project_id}/gaps?gap={quote(gap_id, safe=':')}"


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
        web_url=web_url(public_url, project_id, gap.gap_id),
        assignee=assignee,
        card="",
    )
    return card.model_copy(update={"card": card_text(card)})


def open_item(card: GapCard) -> OpenGapItem:
    """A card as ``list_open_gaps`` lists it: the text to show and what identifies the gap."""

    return OpenGapItem(
        gap_id=card.gap_id,
        severity=card.severity,
        kind=card.kind,
        title=card.title,
        assignee=card.assignee,
        card=card.card,
        web_url=card.web_url,
    )
