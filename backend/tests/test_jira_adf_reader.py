"""ADF to Markdown (``jira.adf_reader``) over fixtures shaped like what Jira Cloud returns for a description or a
comment: every node the reader knows, marks, nesting, and what it does with a node it does not know or a malformed
value. Pure functions: no I/O."""

from __future__ import annotations

from typing import Any

from jira.adf import comment_doc
from jira.adf_reader import adf_to_markdown


def text(value: str, *marks: dict[str, Any]) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": value}
    if marks:
        node["marks"] = list(marks)
    return node


def block(kind: str, *content: dict[str, Any], **attrs: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"type": kind, "content": list(content)}
    if attrs:
        node["attrs"] = attrs
    return node


def doc(*content: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "type": "doc", "content": list(content)}


def para(*content: dict[str, Any]) -> dict[str, Any]:
    return block("paragraph", *content)


DESCRIPTION = doc(
    block("heading", text("Goal"), level=2),
    para(
        text("Pay with "),
        text("card", {"type": "strong"}),
        text(" or "),
        text("wallet", {"type": "em"}),
        text(" see "),
        text("the spec", {"type": "link", "attrs": {"href": "https://example.atlassian.net/wiki/x"}}),
        text(" and "),
        text("old", {"type": "strike"}),
        text(" "),
        text("code()", {"type": "code"}),
        {"type": "hardBreak"},
        {"type": "mention", "attrs": {"id": "5b10", "text": "@Ada Lovelace"}},
        text(" "),
        {"type": "emoji", "attrs": {"shortName": ":smile:", "text": "🙂"}},
        text(" due "),
        {"type": "date", "attrs": {"timestamp": "1772323200000"}},
        text(" "),
        {"type": "status", "attrs": {"text": "READY", "color": "green"}},
        text(" "),
        {"type": "inlineCard", "attrs": {"url": "https://example.atlassian.net/browse/PAY-9"}},
    ),
    block(
        "bulletList",
        block("listItem", para(text("one")), block("orderedList", block("listItem", para(text("deep"))), order=3)),
        block("listItem", para(text("two"))),
    ),
    block("orderedList", block("listItem", para(text("first"))), block("listItem", para(text("second")))),
    block(
        "taskList",
        block("taskItem", text("shipped"), state="DONE", localId="1"),
        block("taskItem", text("tested"), state="TODO", localId="2"),
    ),
    block("codeBlock", text("SELECT 1;\n"), language="sql"),
    block("blockquote", para(text("as agreed"))),
    block("panel", para(text("mind the limit")), panelType="warning"),
    block("expand", para(text("hidden detail")), title="More"),
    {"type": "rule"},
    block(
        "table",
        block("tableRow", block("tableHeader", para(text("Tier"))), block("tableHeader", para(text("Limit")))),
        block("tableRow", block("tableCell", para(text("Gold"))), block("tableCell", para(text("1|2")))),
        block("tableRow", block("tableCell", para(text("Short")))),
    ),
    {"type": "blockCard", "attrs": {"url": "https://example.atlassian.net/wiki/y"}},
    block("mediaSingle", {"type": "media", "attrs": {"id": "f1", "type": "file", "collection": "c"}}),
)

_EXPECTED_LINES = [
    "## Goal",
    "",
    "Pay with **card** or *wallet* see [the spec](https://example.atlassian.net/wiki/x) and ~~old~~ `code()`  ",
    (
        "@Ada Lovelace 🙂 due 2026-03-01 [READY] "
        "[https://example.atlassian.net/browse/PAY-9](https://example.atlassian.net/browse/PAY-9)"
    ),
    "",
    "- one",
    "  3. deep",
    "- two",
    "",
    "1. first",
    "2. second",
    "",
    "- [x] shipped",
    "- [ ] tested",
    "",
    "```sql",
    "SELECT 1;",
    "```",
    "",
    "> as agreed",
    "",
    "> mind the limit",
    "",
    "**More**",
    "",
    "hidden detail",
    "",
    "---",
    "",
    "| Tier | Limit |",
    "| --- | --- |",
    "| Gold | 1\\|2 |",
    "| Short |  |",
    "",
    "[https://example.atlassian.net/wiki/y](https://example.atlassian.net/wiki/y)",
    "",
    "[attachment]",
]
EXPECTED = "\n".join(_EXPECTED_LINES)


def test_a_description_with_every_known_node_becomes_markdown() -> None:
    assert adf_to_markdown(DESCRIPTION) == EXPECTED


def test_a_comment_built_by_the_sync_reads_back_as_its_lead_and_bullets() -> None:
    markdown = adf_to_markdown(
        comment_doc("Specflow updated this issue:", ["Title changed", ("Text changed", "was x")])
    )

    assert markdown == "Specflow updated this issue:\n\n- Title changed\n- Text changed\n  > was x"


def test_a_value_that_is_not_a_document_is_empty() -> None:
    assert adf_to_markdown(None) == ""
    assert adf_to_markdown("plain") == ""
    assert adf_to_markdown({"type": "doc", "content": "oops"}) == ""
    assert adf_to_markdown(doc()) == ""


def test_an_unknown_block_renders_its_children_and_an_unknown_leaf_its_text() -> None:
    markdown = adf_to_markdown(
        doc(
            block("layoutSection", block("layoutColumn", para(text("left")), width=50)),
            block("futureBlock", para(text("kept"))),
            {"type": "futureLeaf", "attrs": {"text": "label"}},
            para(block("futureInline", text("inside")), {"type": "futureLeaf"}),
        )
    )

    assert markdown == "left\n\nkept\n\nlabel\n\ninside"


def test_marks_keep_the_spaces_outside_and_a_blank_text_is_not_marked() -> None:
    markdown = adf_to_markdown(doc(para(text(" padded ", {"type": "strong"}), text("  ", {"type": "em"}), text(""))))

    assert markdown == "**padded**"


def test_a_date_that_is_not_a_timestamp_is_dropped_and_a_mention_without_a_name_is_someone() -> None:
    markdown = adf_to_markdown(
        doc(
            para(
                {"type": "date", "attrs": {"timestamp": "soon"}},
                {"type": "mention", "attrs": {"id": "x"}},
                {"type": "emoji"},
            )
        )
    )

    assert markdown == "@someone"


def test_headings_are_clamped_and_empty_blocks_vanish() -> None:
    markdown = adf_to_markdown(
        doc(
            block("heading", text("Deep"), level=9),
            block("heading", level=1),
            block("table"),
            {"type": "blockCard", "attrs": {}},
            block("codeBlock"),
            block("expand", para(text("body"))),
        )
    )

    assert markdown == "###### Deep\n\n```\n\n```\n\nbody"
