"""The answer dialog of ``answer_gap`` (docs/mcp-server/apps.md "Terminal dialogs", decision D6): the message and the
flat form a client shows on the server's request, and the check of what the person sent.

Claude Code shows the first three lines of the message and folds the rest, so the first lines are the question, why
it matters and a pointer to the card in the chat; the folded part repeats the details for clients that show it all.
A gap has one answer, and the form shows it as a flat list: one box per option, a box for "Other" and a text field
for the person's own answer. Claude Code 2.1.327 folds every list field, single choice or several choices, until the
right arrow key opens it, which people did not find; only boxes (booleans) show one line per option from the start.
Every box starts unticked (an unset field would read "not set"), except the recommended option of a ``major`` gap, as
on the web card. More than one ticked box is refused with a note. A gap with several answers at once does not exist
yet (later.md "Several choices per gap"). Gap text is data: it is shown, never interpreted. Pydantic only: the SDK
registration lives in ``registry.py``. Nothing here logs.

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
POINTER: Final = "Tick one box. The options' details and the quoted lines are in the chat above."


def _one_line(text: str) -> str:
    return " ".join(text.split())


OWN_FIELD: Final = "own_text"
OTHER: Final = "other"


def option_field(option: GapCardOption) -> str:
    """The form field of one option's box: ``option_a`` for option A."""

    return f"option_{option.code.lower()}"


def preset(card: GapCard) -> str | None:
    """The code of the option whose box starts ticked: the recommended one, for a major gap only."""

    if card.severity != "major":
        return None
    return next((option.code for option in card.options if option.recommended), None)


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
    """A box per option, a box for "Other" and the own answer; only the own answer when the options are being
    prepared."""

    if not card.options:
        own = Field(
            default=None,
            title="Your answer",
            description="The options are being prepared; write your own answer.",
            json_schema_extra={"maxLength": OWN_TEXT_MAX},
        )
        own_only: dict[str, Any] = {OWN_FIELD: (str | None, own)}
        model: type[BaseModel] = create_model("GapAnswer", **own_only)
        return model
    ticked = preset(card)
    # the box of the recommended option starts ticked on screen; a box a client leaves out (Backspace) is unticked
    fields: dict[str, Any] = {
        option_field(option): (
            bool,
            Field(
                default=False,
                title=option.title,
                json_schema_extra={"default": True} if option.code == ticked else None,
            ),
        )
        for option in card.options
    }
    fields[OTHER] = (bool, Field(default=False, title="Other: my own answer, written below"))
    fields[OWN_FIELD] = (
        str | None,
        Field(
            default=None,
            title="Your own answer (with Other)",
            description="Tick Other above, then write your answer here.",
            json_schema_extra={"maxLength": OWN_TEXT_MAX},
        ),
    )
    choice_model: type[BaseModel] = create_model("GapAnswer", **fields)
    return choice_model


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


def _own(card: GapCard, own: str) -> Checked:
    """Other, or no choice: the own answer, or what is missing."""

    if own:
        return Checked(own_text=own)
    if not card.options:
        return Checked(problem="Write your own answer: the options are being prepared.")
    return Checked(problem="Tick one box, or tick Other and write your own answer.")


def checked(card: GapCard, sent: BaseModel) -> Checked:
    """One ticked option, or Other (or no box) with an own answer, is an answer; anything else says what to change."""

    values = sent.model_dump()
    own = (values.get(OWN_FIELD) or "").strip()
    ticked = [option for option in card.options if values.get(option_field(option))]
    if len(ticked) + bool(values.get(OTHER)) > 1:
        return Checked(problem="Tick only one box.")
    if len(own) > OWN_TEXT_MAX:
        return Checked(problem=f"The own answer has {len(own)} characters; shorten it to at most {OWN_TEXT_MAX}.")
    if not ticked:
        return _own(card, own)
    if own:
        return Checked(problem=f"You ticked {ticked[0].code} and wrote an own answer: tick Other to send your text.")
    return Checked(option=ticked[0])
