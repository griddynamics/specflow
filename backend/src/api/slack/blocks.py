"""Block Kit for answering gaps in Slack (docs/slack/gap-answering.md "The modal"), pure functions.

``answer_view`` renders up to ``PAGE_SIZE`` gaps as one modal: each gap's question, why and first quote, its options
as radio buttons with their edit text, "Other" with a text field (none for a contradiction: an own answer to it needs
edits, which the web card previews) and nothing chosen as "skip". ``read_answers`` turns the submitted state back
into answers and input errors. Gap text is untrusted data: it is cut first, then escaped (``&``, ``<``, ``>``), and
every ``mrkdwn`` text is ``verbatim``, so it cannot make links or mentions.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from agent_contracts import GapDetail, GapDetailOption
from api.gap.replies import parse_reply

CALLBACK_ID: Final = "specflow_gap_answers"
PAGE_SIZE: Final = 5
CHOICE: Final = "choice"
OWN: Final = "own"
APPLY_BLOCK: Final = "apply"
APPLY_VALUE: Final = "apply_now"
OWN_MAX: Final = 2999  # Slack: a text input's max_length is under 3000 (the web card takes 4000)
OPTION_TEXT_MAX: Final = 75  # Slack: an option's text and description
SECTION_MAX: Final = 2900  # Slack: 3000 per section text
CONTEXT_MAX: Final = 300
CONTRADICTION: Final = "contradiction"


@dataclass(frozen=True)
class SlackAnswer:
    """One answer read from the modal: an option by rank or own text (exactly one), checked against ``updated_at``."""

    gap_id: str
    option_rank: int | None
    own_text: str | None
    updated_at: datetime


@dataclass(frozen=True)
class Submitted:
    """What a submit holds: the project, the answers, whether to apply now, and the input errors by block id."""

    project_id: str
    answers: tuple[SlackAnswer, ...]
    apply_now: bool
    errors: Mapping[str, str]


def cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def escaped(text: str, limit: int) -> str:
    """Cut first, then escape: an escape is never cut in half."""

    return cut(text, limit).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plain(text: str, limit: int = OPTION_TEXT_MAX) -> dict[str, Any]:
    return {"type": "plain_text", "text": cut(text, limit) or " ", "emoji": False}


def _mrkdwn(text: str) -> dict[str, Any]:
    return {"type": "mrkdwn", "text": text, "verbatim": True}


def _choice_block(gap_id: str) -> str:
    return f"choice:{gap_id}"


def _own_block(gap_id: str) -> str:
    return f"own:{gap_id}"


def _edit_line(option: GapDetailOption) -> str:
    edit = option.edits[0] if option.edits else None
    if edit is None:
        return option.detail
    verb = "Replaces in" if edit.placement == "amend" else "Adds to"
    return f"{verb} {edit.where}: {edit.text}"


def _gap_blocks(gap: GapDetail) -> list[dict[str, Any]]:
    header = (
        f"*{escaped(gap.severity.upper(), 20)} · {escaped(gap.kind, 40)}*\n"
        f"*{escaped(gap.title, 200)}*\n{escaped(gap.question, 1200)}"
    )
    notes = [f"Why: {escaped(gap.why, CONTEXT_MAX)}"]
    if gap.quotes:
        quote = gap.quotes[0]
        notes.append(f"{escaped(quote.source, 120)}: “{escaped(quote.text, CONTEXT_MAX)}”")
    options = [
        {
            "text": _plain(f"{option.code}  {option.title}" + ("  (recommended)" if option.recommended else "")),
            "description": _plain(_edit_line(option)),
            "value": f"rank:{option.rank}",
        }
        for option in gap.options
    ]
    contradiction = gap.kind == CONTRADICTION
    if not contradiction:
        options.append({"text": _plain("Other: my own answer (below)"), "value": OWN})
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": _mrkdwn(header[:SECTION_MAX])},
        {"type": "context", "elements": [_mrkdwn(note) for note in notes]},
        {
            "type": "input",
            "block_id": _choice_block(gap.gap_id),
            "optional": True,
            "label": _plain("Your answer (leave empty to skip)"),
            "element": {"type": "radio_buttons", "action_id": CHOICE, "options": options},
        },
    ]
    if contradiction:
        link = f"<{gap.web_url}|on the web card>" if gap.web_url else "on the web card"
        blocks.append({"type": "context", "elements": [_mrkdwn(f"Your own answer to a contradiction: {link}.")]})
    else:
        blocks.append(
            {
                "type": "input",
                "block_id": _own_block(gap.gap_id),
                "optional": True,
                "label": _plain("Other"),
                "element": {"type": "plain_text_input", "action_id": OWN, "multiline": True, "max_length": OWN_MAX},
            }
        )
    blocks.append({"type": "divider"})
    return blocks


def answer_view(
    project_id: str, project_name: str, gaps: Sequence[GapDetail], *, staged_by_others: int, ai_staged: int
) -> dict[str, Any]:
    """The modal for ``gaps`` (at most ``PAGE_SIZE``) with the "Apply now" box, which says what else it applies."""

    shown = list(gaps)[:PAGE_SIZE]
    blocks: list[dict[str, Any]] = [
        {"type": "context", "elements": [_mrkdwn(f"*{escaped(project_name, 150)}* · {len(shown)} open questions")]}
    ]
    for gap in shown:
        blocks.extend(_gap_blocks(gap))
    also = f"It also applies {staged_by_others} answers staged by others and {ai_staged} AI answers."
    blocks.append(
        {
            "type": "input",
            "block_id": APPLY_BLOCK,
            "optional": True,
            "label": _plain("Resolve"),
            "element": {
                "type": "checkboxes",
                "action_id": APPLY_VALUE,
                "options": [{"text": _plain("Apply now: one update build"), "value": APPLY_VALUE}],
                "initial_options": [{"text": _plain("Apply now: one update build"), "value": APPLY_VALUE}],
            },
            "hint": _plain(also, 150),
        }
    )
    metadata = {"p": project_id, "g": {gap.gap_id: gap.updated_at.isoformat() for gap in shown}}
    return {
        "type": "modal",
        "callback_id": CALLBACK_ID,
        "title": _plain("Answer gaps", 24),
        "submit": _plain("Save answers", 24),
        "close": _plain("Cancel", 24),
        "private_metadata": json.dumps(metadata, separators=(",", ":")),
        "blocks": blocks,
    }


def read_answers(view: Mapping[str, Any]) -> Submitted:
    """The answers of a submitted ``answer_view``: an option, or own text with "Other" (or with nothing chosen);
    a gap with neither is skipped. Errors: "Other" without text, and an option together with own text."""

    metadata = json.loads(view.get("private_metadata") or "{}")
    values: Mapping[str, Any] = view.get("state", {}).get("values", {})
    answers: list[SlackAnswer] = []
    errors: dict[str, str] = {}
    for gap_id, stamp in dict(metadata.get("g", {})).items():
        selected = (values.get(_choice_block(gap_id), {}).get(CHOICE) or {}).get("selected_option") or {}
        choice = str(selected.get("value") or "")
        own = " ".join(str((values.get(_own_block(gap_id), {}).get(OWN) or {}).get("value") or "").split())
        updated_at = datetime.fromisoformat(stamp)
        if choice.startswith("rank:"):
            if own:
                errors[_own_block(gap_id)] = "Pick “Other” to send your own answer, or clear this field."
                continue
            answers.append(SlackAnswer(gap_id, int(choice.removeprefix("rank:")), None, updated_at))
        elif own:
            answers.append(SlackAnswer(gap_id, None, own, updated_at))
        elif choice == OWN:
            errors[_own_block(gap_id)] = "Write your answer, or pick an option."
    boxes = (values.get(APPLY_BLOCK, {}).get(APPLY_VALUE) or {}).get("selected_options") or []
    apply_now = any(box.get("value") == APPLY_VALUE for box in boxes)
    return Submitted(str(metadata.get("p", "")), tuple(answers), apply_now, errors)


def message_view(title: str, lines: Sequence[str]) -> dict[str, Any]:
    """A modal with text only (saving, the outcome)."""

    text = "\n".join(lines) or " "
    return {
        "type": "modal",
        "callback_id": f"{CALLBACK_ID}_done",
        "title": _plain(title, 24),
        "close": _plain("Close", 24),
        "blocks": [{"type": "section", "text": _mrkdwn(text[:SECTION_MAX])}],
    }


# The channel flow: one message per gap, answered in its thread --------------------------------------------------------

OPTION_LINE_MAX: Final = 300


def reply_answer(text: str, codes: Mapping[str, int]) -> tuple[int | None, str | None]:
    """A thread reply as an answer (``api.gap.replies.parse_reply``, cut at the length of a Slack text input)."""

    return parse_reply(text, codes, OWN_MAX)


SEVERITY_MARK: Final = {"blocker": "🔴", "major": "🟠", "minor": "🟡"}


def gap_headline(number: int, gap: GapDetail, *, answered: bool = False) -> tuple[str, list[dict[str, Any]]]:
    """The one line a gap shows in the channel: ``#number``, severity and title, so a channel of many questions stays
    readable; the details go in its thread (``gap_details``). Once answered the line is only struck through (who
    answered with what is said in the thread)."""

    title = escaped(gap.title, 200).replace("~", "-")  # a ~ would end the strike-through
    mark = SEVERITY_MARK.get(gap.severity, "⚪")
    line = f"#{number} · {escaped(gap.severity.upper(), 20)} · {title}"
    text = f"✅ ~{line}~" if answered else f"{mark} *{line}*"
    return f"#{number} · {title}", [{"type": "section", "text": _mrkdwn(text[:SECTION_MAX])}]


def gap_details(gap: GapDetail) -> tuple[str, list[dict[str, Any]]]:
    """The first message in a gap's thread: kind, question, why, first quote, the options with their edit text and how
    to answer. No web link: Slack is the whole surface."""

    header = f"_{escaped(gap.kind, 40)}_\n{escaped(gap.question, 1200)}"
    notes = [f"Why: {escaped(gap.why, CONTEXT_MAX)}"]
    if gap.quotes:
        quote = gap.quotes[0]
        notes.append(f"{escaped(quote.source, 120)}: “{escaped(quote.text, CONTEXT_MAX)}”")
    options = "\n".join(
        f"*{escaped(option.code, 2)}*  {escaped(option.title, 150)}{'  _(recommended)_' if option.recommended else ''}"
        f"\n      {escaped(_edit_line(option), OPTION_LINE_MAX)}"
        for option in gap.options
    )
    codes = ", ".join(option.code for option in gap.options)
    how = (
        f"Reply here with one letter ({codes}). An own answer to a contradiction goes on the web card."
        if gap.kind == CONTRADICTION
        else f"Reply here with one letter ({codes}), or write your own answer."
    )
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": _mrkdwn(header[:SECTION_MAX])},
        {"type": "context", "elements": [_mrkdwn(note) for note in notes]},
        {"type": "section", "text": _mrkdwn(options[:SECTION_MAX] or " ")},
        {"type": "context", "elements": [_mrkdwn(how)]},
    ]
    return escaped(gap.question, 150), blocks
