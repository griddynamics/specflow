"""The answer dialog of ``answer_gap`` (docs/mcp-server/apps.md "Terminal dialogs", D6): the message, the flat form and
the check of what the person sent (``mcp_server.gap_dialog``)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from mcp.server.elicitation import render_elicitation_schema

from agent_contracts import GapCard, GapCardOption, GapCardQuote
from mcp_server import gap_dialog


def option(code: str, label: str, detail: str = "", *, recommended: bool = False) -> GapCardOption:
    rank = ord(code) - ord("A") + 1
    title = f"{code}) {label}{' (recommended)' if recommended else ''}"
    return GapCardOption(
        code=code, rank=rank, title=title, label=label, detail=detail, recommended=recommended, placement="amend"
    )


def card(
    severity: str = "major", options: tuple[GapCardOption, ...] | None = None, why: str = "Two lines differ."
) -> GapCard:
    return GapCard(
        gap_id="GAP::7C2A91E04B3D",
        severity=severity,
        kind="contradiction",
        origin="analysis",
        title="Refund window: 14 days or 30 days?",
        question="How many days after delivery\nmay a customer ask for a refund?",
        why=why,
        quotes=[GapCardQuote(source='orders-spec.md, "Refunds", line 12', text="Refunds within 14 days.")],
        options=list(
            options
            if options is not None
            else (
                option("A", "14 days", "Keep the orders spec."),
                option("B", "30 days", "Keep the payments rule.", recommended=True),
            )
        ),
        updated_at=datetime(2026, 11, 18, 9, 0, tzinfo=UTC),
        web_url="https://app.example.test/projects/p/gaps?gap=GAP::7C2A91E04B3D",
        card="(the card)",
    )


def test_the_first_three_lines_are_the_question_why_and_the_pointer_to_the_card() -> None:
    lines = gap_dialog.message(card()).splitlines()

    assert lines[:3] == [
        "How many days after delivery may a customer ask for a refund?",
        "Why: Two lines differ.",
        "The options' details and the quoted lines are in the chat above.",
    ]
    text = "\n".join(lines)
    assert "B) 30 days (recommended): Keep the payments rule." in text
    assert 'orders-spec.md, "Refunds", line 12: "Refunds within 14 days."' in text
    assert lines[-1] == (
        "GAP::7C2A91E04B3D | major | contradiction | Refund window: 14 days or 30 days? "
        "| version 2026-11-18T09:00:00+00:00"
    )


def test_a_new_version_of_the_gap_is_a_new_dialog() -> None:
    """The SDK pins an answer to the dialog as shown: a gap changed since (a new ``updated_at``) is asked again."""

    later = card().model_copy(update={"updated_at": datetime(2026, 11, 18, 9, 5, tzinfo=UTC)})

    assert gap_dialog.message(later) != gap_dialog.message(card())


def test_a_gap_without_a_why_still_has_three_lines_that_stand_on_their_own() -> None:
    assert gap_dialog.message(card(why=" ")).splitlines()[1] == "Why: See the card in the chat."


@pytest.mark.parametrize(("severity", "ticked"), [("major", "option_b"), ("blocker", None), ("minor", None)])
def test_one_yes_no_field_per_option_and_the_recommended_one_ticked_for_a_major_gap_only(severity, ticked) -> None:
    schema = render_elicitation_schema(gap_dialog.form(card(severity)))
    fields = schema["properties"]

    assert list(fields) == ["option_a", "option_b", "own_text"]
    assert fields["option_a"]["title"] == "A) 14 days" and fields["option_a"]["description"] == "Keep the orders spec."
    assert fields["option_b"]["title"] == "B) 30 days (recommended)"
    assert [name for name in ("option_a", "option_b") if fields[name]["default"]] == ([ticked] if ticked else [])
    assert fields["own_text"]["title"] == "Or your own answer (untick A-B)"
    assert fields["own_text"]["maxLength"] == 4000


def test_a_gap_whose_options_are_being_prepared_asks_only_for_an_own_answer() -> None:
    schema = render_elicitation_schema(gap_dialog.form(card(options=())))

    assert list(schema["properties"]) == ["own_text"]
    assert schema["properties"]["own_text"]["title"] == "Your answer"


def sent(gap: GapCard, **values: object) -> gap_dialog.Checked:
    return gap_dialog.checked(gap, gap_dialog.form(gap).model_validate(values))


def test_exactly_one_ticked_option_is_an_answer() -> None:
    gap = card("blocker")

    chosen = sent(gap, option_b=True)

    assert chosen.option is not None and chosen.option.rank == 2 and chosen.choice == "B) 30 days"
    assert chosen.problem is None and chosen.own_text is None


def test_no_ticked_option_and_an_own_answer_is_an_answer() -> None:
    gap = card("blocker")

    own = sent(gap, own_text="  21 days for everyone. ")

    assert (own.own_text, own.option, own.choice) == ("21 days for everyone.", None, "your own answer")


@pytest.mark.parametrize(
    ("values", "problem"),
    [
        ({}, "Tick exactly one option, or write an own answer."),
        ({"option_a": True, "option_b": True}, "Tick exactly one option, or write an own answer."),
        ({"option_a": True, "own_text": "x"}, "untick the options or clear the text"),
        ({"own_text": "   "}, "Tick exactly one option, or write an own answer."),
        ({"own_text": "x" * 4001}, "shorten it to at most 4000"),
    ],
    ids=["nothing", "two options", "option and text", "blank text", "too long"],
)
def test_anything_else_says_what_to_change_and_records_nothing(values, problem) -> None:
    checked = sent(card("blocker"), **values)

    assert checked.problem is not None and problem in checked.problem
    assert checked.option is None and checked.own_text is None
