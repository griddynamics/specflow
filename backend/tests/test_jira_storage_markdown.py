"""Confluence storage format to Markdown (``jira.storage_markdown``) over fixtures shaped like a real page body: the
elements the importer keeps (headings, marks, nested lists, task lists, tables, code macros, panels, links, quotes) and
what it drops (images, attachments, macros without text). Pure functions: no I/O."""

from __future__ import annotations

from jira.storage_markdown import parse_storage, to_markdown

_PAGE_LINES = [
    "<h1>Order limits</h1>",
    (
        "<p>Orders above <strong>500 EUR</strong> need a <em>second</em> approval &amp; a note.<br />"
        "See <code>limits.yaml</code>.</p>"
    ),
    (
        '<ul><li>First<ul><li>Nested <a href="https://example.atlassian.net/wiki/spaces/ORD/pages/42/Refunds">'
        "Refunds</a></li></ul></li><li><p>Second</p></li></ul>"
    ),
    "<ol><li>One</li><li>Two</li></ol>",
    (
        "<ac:task-list>"
        "<ac:task><ac:task-id>1</ac:task-id><ac:task-status>complete</ac:task-status>"
        "<ac:task-body>Done thing</ac:task-body></ac:task>"
        "<ac:task><ac:task-id>2</ac:task-id><ac:task-status>incomplete</ac:task-status>"
        "<ac:task-body>Open thing</ac:task-body></ac:task>"
        "</ac:task-list>"
    ),
    ("<table><tbody><tr><th>Tier</th><th>Limit</th></tr><tr><td>Gold</td><td>1000 | 2000</td></tr></tbody></table>"),
    (
        '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">python</ac:parameter>'
        '<ac:plain-text-body><![CDATA[if a < b:\n    print("x")]]></ac:plain-text-body></ac:structured-macro>'
    ),
    (
        '<ac:structured-macro ac:name="info"><ac:parameter ac:name="title">Heads up</ac:parameter>'
        "<ac:rich-text-body><p>Read this.</p></ac:rich-text-body></ac:structured-macro>"
    ),
    (
        "<p>Linked: "
        '<ac:link><ri:page ri:content-title="Refund policy" ri:space-key="ORD" /></ac:link> and '
        '<ac:link><ri:page ri:content-title="Other" />'
        "<ac:plain-text-link-body><![CDATA[the other page]]></ac:plain-text-link-body></ac:link> "
        '<ac:link><ri:url ri:value="https://example.org/x" /><ac:link-body>ext</ac:link-body></ac:link></p>'
    ),
    (
        '<p>Ticket <ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">PAY-12</ac:parameter>'
        '</ac:structured-macro> <ac:emoticon ac:name="smile" ac:emoji-fallback="🙂" /> '
        '<time datetime="2026-03-01" /></p>'
    ),
    '<ac:image><ri:attachment ri:filename="a.png" /></ac:image>',
    "<blockquote><p>Quoted</p></blockquote><hr />",
    "<pre>raw\n  text</pre>",
]
PAGE = "\n".join(_PAGE_LINES)

_EXPECTED_LINES = [
    "# Order limits",
    "",
    "Orders above **500 EUR** need a *second* approval & a note.  ",
    "See `limits.yaml`.",
    "",
    "- First",
    "  - Nested [Refunds](https://example.atlassian.net/wiki/spaces/ORD/pages/42/Refunds)",
    "- Second",
    "",
    "1. One",
    "2. Two",
    "",
    "- [x] Done thing",
    "- [ ] Open thing",
    "",
    "| Tier | Limit |",
    "| --- | --- |",
    "| Gold | 1000 \\| 2000 |",
    "",
    "```python",
    "if a < b:",
    '    print("x")',
    "```",
    "",
    "**Heads up**",
    "",
    "Read this.",
    "",
    "Linked: Refund policy and the other page [ext](https://example.org/x)",
    "",
    "Ticket PAY-12 🙂 2026-03-01",
    "",
    "> Quoted",
    "",
    "---",
    "",
    "```",
    "raw",
    "  text",
    "```",
]
EXPECTED = "\n".join(_EXPECTED_LINES)


def test_a_page_body_becomes_markdown_with_every_kept_element() -> None:
    assert to_markdown(PAGE) == EXPECTED


def test_an_empty_body_is_empty_markdown() -> None:
    assert to_markdown("") == ""
    assert to_markdown("<p> </p><ac:image><ri:attachment ri:filename='a.png' /></ac:image>") == ""


def test_a_table_without_a_header_row_takes_its_first_row_as_the_header_and_pads_short_rows() -> None:
    markdown = to_markdown("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table><table></table>")

    assert markdown == "| a | b |\n| --- | --- |\n| c |  |"


def test_a_code_macro_without_a_language_or_body_is_an_empty_fence() -> None:
    assert to_markdown('<ac:structured-macro ac:name="noformat"></ac:structured-macro>') == "```\n\n```"


def test_text_outside_a_block_and_a_block_inside_a_list_item_are_kept() -> None:
    markdown = to_markdown("loose <b>text</b><div><p>inside</p></div><ul><li><p>one</p><p>two</p></li><li></li></ul>")

    assert markdown == "loose **text**\n\ninside\n\n- one two\n-"


def test_an_unclosed_element_and_an_unknown_end_tag_do_not_lose_the_text() -> None:
    assert to_markdown("<p>one<p>two</span>") == "one two"


def test_an_anchor_without_text_or_href_and_empty_marks_render_plainly() -> None:
    assert to_markdown(
        '<p><a href="https://x.example/a">x</a> <a>plain</a> <a href="https://x.example"></a><b></b></p>'
    ) == ("[x](https://x.example/a) plain")


def test_the_elements_below_a_node_come_in_document_order() -> None:
    tree = parse_storage("<div><p>a</p><ul><li>b</li></ul></div>")

    assert [node.tag for node in tree.elements()] == ["#root", "div", "p", "ul", "li"]
    assert tree.text() == "ab"
    assert tree.named("div") is not None and tree.named("p") is None
