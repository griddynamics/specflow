"""The answer dialog of ``answer_gap`` (docs/mcp-server/apps.md "Terminal dialogs", decision D6): the message and the
flat form a client shows on the server's request, and the check of what the person sent.

Claude Code shows the first three lines of the message and folds the rest, so the first lines are the question, why
it matters and a pointer to the card in the chat; the folded part repeats the details for clients that show it all.
One yes/no field per option (every option on screen, no right arrow key), the recommended one ticked for a ``major``
gap only, and one field for the person's own answer. Gap text is data: it is shown, never interpreted. Pydantic only:
the SDK registration lives in ``registry.py``. Nothing here logs.

The SDK takes the person's answer only for the dialog exactly as it was shown (its digest), and the server builds the
dialog again on every round. The last line names the gap's version (``updated_at``), so any change of the gap between
the dialog and the answer asks again instead of recording an answer to a gap the person did not see. The length of
an own answer is checked here, not by the form model: a client that ignores ``maxLength`` gets a note on what to
change, not the SDK's error text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, Field, create_model

from agent_contracts import OWN_TEXT_MAX, GapCard, GapCardOption

OWN_ANSWER: Final = "your own answer"
POINTER: Final = "The options' details and the quoted lines are in the chat above."


def _one_line(text: str) -> str:
    return " ".join(text.split())


def option_field(option: GapCardOption) -> str:
    return f"option_{option.code.lower()}"


def preselected(card: GapCard, option: GapCardOption) -> bool:
    """The recommended option starts ticked for a major gap only, as on the web card."""

    return card.severity == "major" and option.recommended


def message(card: GapCard) -> str:
    """The text above the fields: three lines that stand on their own, then the details, folded by Claude Code."""

    why = _one_line(card.why) or "See the card in the chat."
    head = f"{_one_line(card.question)}\nWhy: {why}\n{POINTER}"
    details = "\n".join(
        f"{option.title}: {option.detail}" if option.detail else option.title for option in card.options
    )
    quotes = "\n".join(f'  {quote.source}: "{quote.text}"' for quote in card.quotes)
    parts = [head, details, f"Quoted lines:\n{quotes}" if quotes else ""]
    version = card.updated_at.isoformat()
    parts.append(f"{card.gap_id} | {card.severity} | {card.kind} | {_one_line(card.title)} | version {version}")
    return "\n\n".join(part for part in parts if part)


def form(card: GapCard) -> type[BaseModel]:
    """One yes/no field per option and the own answer; only the own answer when the options are being prepared."""

    fields: dict[str, Any] = {
        option_field(option): (
            bool,
            Field(default=preselected(card, option), title=option.title, description=option.detail or None),
        )
        for option in card.options
    }
    if card.options:
        codes = f"{card.options[0].code}-{card.options[-1].code}"
        title = f"Or your own answer (untick {codes})"
        description = "Your own answer replaces the options; leave it empty to send the ticked option."
    else:
        title, description = "Your answer", "The options are being prepared; write your own answer."
    fields["own_text"] = (
        str | None,
        Field(default=None, title=title, description=description, json_schema_extra={"maxLength": OWN_TEXT_MAX}),
    )
    model: type[BaseModel] = create_model("GapAnswer", **fields)
    return model


@dataclass(frozen=True)
class Checked:
    """What the person sent: one option, or their own text, or the ``problem`` to fix (nothing is recorded)."""

    option: GapCardOption | None = None
    own_text: str | None = None
    problem: str | None = None

    @property
    def choice(self) -> str:
        if self.option is not None:
            return f"{self.option.code}) {self.option.label}"
        return OWN_ANSWER


def checked(card: GapCard, sent: BaseModel) -> Checked:
    """Exactly one ticked option, or no ticked option and an own answer, is an answer."""

    values = sent.model_dump()
    own = (values.get("own_text") or "").strip()
    ticked = [option for option in card.options if values.get(option_field(option))]
    if own and ticked:
        return Checked(problem="Both an option and an own answer were given: untick the options or clear the text.")
    if len(own) > OWN_TEXT_MAX:
        return Checked(problem=f"The own answer has {len(own)} characters; shorten it to at most {OWN_TEXT_MAX}.")
    if own:
        return Checked(own_text=own)
    if len(ticked) != 1:
        return Checked(problem="Tick exactly one option, or write an own answer.")
    return Checked(option=ticked[0])
