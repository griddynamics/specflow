"""Notion Markdown converter: the sandbox fixtures, every rule of the discovery table, hostile input."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import pytest

from notion.convert import ConvertedPage, MarkupTooDeepError, render_row, scrub_original, to_markdown
from notion.convert.inline import MAX_DEPTH
from notion.convert.render import MAX_CHARS, TRUNCATED

PRD_ID = "423ed336-0645-8163-a8dd-e12275fc660d"
SNAPSHOTS = Path(__file__).parent / "snapshots"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")
NFM_TAG = re.compile(
    r"</?(?:callout|toggle|columns?|tabs?|synced-block(?:-reference)?|quote|bookmark|embed|pdf|file|audio|video|image|page|"
    r"database|meeting-notes|summary|notes|transcript|custom-block|table|tr|td|th|table-of-contents|p|link-preview|mention|"
    r"date|b|i|s|u|code|span|br)\b[^>]*>"
)


def outside_code(markdown: str) -> str:
    """The Markdown without its fenced code (a tag in code is text)."""

    return re.sub(r"^(?:> ?)*(```|~~~).*?^(?:> ?)*\1[^\n]*$", "", markdown, flags=re.DOTALL | re.MULTILINE)


def clean(nfm: str) -> str:
    md = to_markdown(nfm).markdown
    assert not NFM_TAG.search(outside_code(md)), md
    assert not EMAIL.search(md), md
    return md


# Fixtures ---------------------------------------------------------------------------------------------------------


def test_prd_snapshot(markdown: dict[str, str]) -> None:
    page = to_markdown(markdown[PRD_ID])
    assert page.markdown == (SNAPSHOTS / "prd.md").read_text(encoding="utf-8")
    assert not NFM_TAG.search(outside_code(page.markdown))
    assert not EMAIL.search(page.markdown)
    assert "Sample Author" in page.markdown


def test_every_fixture_converts_clean(markdown: dict[str, str]) -> None:
    for nfm in markdown.values():
        clean(nfm)
        assert to_markdown(nfm) == to_markdown(nfm)  # deterministic


def test_wiki_root_collects_sub_pages_in_order(markdown: dict[str, str]) -> None:
    page = to_markdown(markdown["423ed336-0645-8137-b924-dd97aa16e0a9"])
    assert [p.title for p in page.sub_pages] == ["Onboarding", "Runbooks"]
    assert page.sub_pages[0].id == "423ed336-0645-8143-b422-d74e5eb9926c"
    assert page.sub_pages[0].url == "https://app.notion.com/p/423ed33606458143b422d74e5eb9926c"
    assert page.databases == ()
    assert page.markdown.startswith("Root of a nested wiki. Children follow.\n\n- Sub-page: [Onboarding](https://")


def test_sandbox_root_collects_pages_and_databases_with_data_source(markdown: dict[str, str]) -> None:
    page = to_markdown(markdown["423ed336-0645-81f3-b5ea-ff40471c93e5"])
    assert [p.title for p in page.sub_pages] == [
        "PRD - Checkout Loyalty Points",
        "ADR-007 Use event sourcing for points ledger",
        "Engineering Wiki",
    ]
    assert [(d.title, d.data_source_id) for d in page.databases] == [
        ("Requirements DB", "423ed336-0645-819d-aec9-000b2cecb51b"),
        ("Specflow Plan (trial)", "423ed336-0645-8162-9b9a-000bbce2d0e4"),
    ]
    assert "table-of-contents" not in page.markdown


def test_adr_tabs_synced_block_mention_and_date(markdown: dict[str, str]) -> None:
    md = clean(markdown["423ed336-0645-816a-aff4-ffe7862445e1"])
    assert md.index("#### Context") < md.index("#### Decision") < md.index("#### Consequences")
    assert "Glossary: **Ledger** - the append-only list" in md
    assert "Decided on 2026-11-20 by Sample Author." in md
    assert "$$\nbalance" in md


def test_runbook_callout_after_numbered_list(markdown: dict[str, str]) -> None:
    md = clean(markdown["423ed336-0645-81b9-83b2-f33eaa8f36e5"])
    assert md.startswith("1. Pause the `points-consumer` deployment.\n2. Run `make backfill FROM=2026-01-01`.\n3.")
    assert md.endswith("\n\n> 🚨 Never run the backfill during the Black Friday freeze.\n")


def test_empty_pages_convert_to_nothing(markdown: dict[str, str]) -> None:
    assert to_markdown(markdown["423ed336-0645-81ec-b3ae-e35ee7c4de54"]) == ConvertedPage("")
    assert to_markdown("") == ConvertedPage("")
    assert to_markdown("\n \n") == ConvertedPage("")


# Rules ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("nfm", "expected"),
    [
        ('<callout icon="💡" color="x">\nText <b>b</b>\n</callout>', "> 💡 Text **b**\n"),
        ('<callout icon="💡">\n\tTabbed text\n\n\tsecond\n</callout>', "> 💡 Tabbed text\n>\n> second\n"),
        ('<callout icon="💡">\n- item\n</callout>', "> 💡\n>\n> - item\n"),
        ('<callout icon="notion://custom_emoji/a/b">\nx\n</callout>', "> x\n"),
        ('<callout>\nx\n</callout>', "> x\n"),
        ('<toggle heading="h3">\nTitle\n\nbody\n</toggle>', "### Title\n\nbody\n"),
        ('<toggle>\nTitle\n\tchild\n</toggle>', "**Title**\n\nchild\n"),
        ('<tabs>\n<tab icon="x">\nOne\n\ntext\n</tab>\n<tab>\nTwo\n</tab>\n</tabs>', "#### One\n\ntext\n\n#### Two\n"),
        ('<columns>\n<column>\nA\n</column>\n<column>\nB\n</column>\n</columns>', "A\n\nB\n"),
        ('<synced-block url="u">\ncontent\n</synced-block>', "content\n"),
        ('<synced-block-reference url="u">\n</synced-block-reference>', ""),
        ('<mention url="user://id/a@b.com">Name</mention>', "Name\n"),
        ('<mention url="user://id/a@b.com"/>', "a user\n"),
        ('<mention url="https://www.notion.so/x-1">Title</mention>', "[Title](https://www.notion.so/x-1)\n"),
        ('<mention url="https://www.notion.so/x-1"/>', "[https://www.notion.so/x-1](https://www.notion.so/x-1)\n"),
        ('<mention url="javascript:alert(1)">Evil</mention>', "Evil\n"),
        ('<date start="2027-02-15" />', "2027-02-15\n"),
        ('<date start="2027-02-15" end="2027-02-20"/>', "2027-02-15 to 2027-02-20\n"),
        (
            '<b>b</b> <i>i</i> <s>s</s> <u>u</u> <code>c</code> <span color="red">sp</span>',
            "**b** *i* ~~s~~ u `c` sp\n",
        ),
        ("<b>bold </b>next", "**bold** next\n"),
        ("<b><b>twice</b></b>", "**twice**\n"),
        ("<b>x<i>y</b>z</i>", "**x*y***z\n"),
        ("<code>a`b</code>", "``a`b``\n"),
        ("a<br>b<br/>c", "a b c\n"),
        ('<bookmark source="https://x.test/a">\nCaption\n</bookmark>', "[Caption](https://x.test/a)\n"),
        ('<bookmark source="https://x.test/a"></bookmark>', "[https://x.test/a](https://x.test/a)\n"),
        ('<pdf source="https://x.test/a.pdf">\nSpec\n</pdf>', "[Spec](https://x.test/a.pdf)\n"),
        ('<video source="javascript:x">\nClip\n</video>', "Clip\n"),
        ('<image source="https://x.test/i.png">\nSketch\n</image>', "Image: Sketch\n"),
        ('<image source="https://x.test/i.png">\n</image>', ""),
        ("![Sketch](https://x.test/i.png)", "Image: Sketch\n"),
        ("![](https://x.test/i.png)", ""),
        ("[![alt](https://x.test/i.png)](https://x.test/page)", "[Image: alt](https://x.test/page)\n"),
        ("<table-of-contents />\n<p />\n<link-preview url=\"x\"/>\ntext", "text\n"),
        ("Literal \\<div> and `<div>` and <https://x.test/a>", "Literal &lt;div> and `<div>` and <https://x.test/a>\n"),
        ("```py\nx = 1\n\n\n\ny = '<b>'\n```", "```py\nx = 1\n\n\n\ny = '<b>'\n```\n"),
        ("$$\n<b>a</b> < b\n$$", "$$\n<b>a</b> < b\n$$\n"),
        (
            "- [x] done\n- [ ] open\n\n| a | b |\n| --- | --- |\n| 1 | 2 |",
            "- [x] done\n- [ ] open\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n",
        ),
        ("1\n\n\n\n2", "1\n\n2\n"),
    ],
)  # fmt: skip
def test_rule(nfm: str, expected: str) -> None:
    assert to_markdown(nfm).markdown == expected


def test_page_and_database_blocks_become_items_and_refs() -> None:
    page = to_markdown(
        '<page url="https://app.notion.com/p/423ed33606458143b422d74e5eb9926c">\nOnboarding\n</page>\n'
        '<page url="https://app.notion.com/p/423ed336064581599753d8419c8ae458">Runbooks</page>\n'
        '<database url="https://www.notion.so/x-423ed3360645819b8a30d73a847eb5e6?v=1" data-source-url="collection://423ED336-0645-819D-AEC9-000B2CECB51B">\nReqs\n</database>\n'
        '<database url="https://app.notion.com/p/423ed33606458136a09fe7d3e03939ab" data-source-url="evil">'
        "Other</database>"
    )
    assert page.markdown == (
        "- Sub-page: [Onboarding](https://app.notion.com/p/423ed33606458143b422d74e5eb9926c)\n"
        "- Sub-page: [Runbooks](https://app.notion.com/p/423ed336064581599753d8419c8ae458)\n"
        "- Sub-database: [Reqs](https://www.notion.so/x-423ed3360645819b8a30d73a847eb5e6?v=1)\n"
        "- Sub-database: [Other](https://app.notion.com/p/423ed33606458136a09fe7d3e03939ab)\n"
    )
    assert [p.title for p in page.sub_pages] == ["Onboarding", "Runbooks"]
    assert [(d.title, d.data_source_id) for d in page.databases] == [
        ("Reqs", "423ed336-0645-819d-aec9-000b2cecb51b"),
        ("Other", None),
    ]


def test_page_with_an_unparsable_url_is_text_without_a_reference() -> None:
    page = to_markdown('<page url="https://evil.test/x">\nT\n</page>')
    assert page.sub_pages == ()
    assert page.markdown == "- Sub-page: [T](https://evil.test/x)\n"


def test_styled_table_becomes_a_gfm_table() -> None:
    md = clean(
        '<table fit-page-width="true" header-row="true">\n<colgroup>\n<col color="gray"/>\n<col/>\n</colgroup>\n'
        '<tr color="gray_bg">\n<td>Name</td>\n<td color="red">Role | team</td>\n</tr>\n'
        "<tr>\n<td><b>Jane</b>\nOwner</td>\n"
        '<td><mention url="user://i/e@x.com">J</mention> <code>a|b</code></td>\n</tr>\n'
        "<tr>\n<td>short</td>\n</tr>\n</table>\nAfter the table"
    )
    assert md == (
        "| Name | Role \\| team |\n| --- | --- |\n| **Jane** Owner | J `a\\|b` |\n| short |  |\n\nAfter the table\n"
    )


def test_table_edge_cases() -> None:
    assert to_markdown("<table>\n</table>").markdown == ""
    assert to_markdown("<table><tr><td>a</td><td/></tr></table>").markdown == "| a |  |\n| --- | --- |\n"
    assert clean("<table>\n<tr><td>unclosed").startswith("| unclosed |")


def test_meeting_notes_keep_summary_and_notes_not_transcript() -> None:
    md = clean(
        "<meeting-notes>\n\tSprint sync\n"
        "\t<summary>\n\t\tWe shipped it.\n\t</summary>\n"
        "\t<notes>\n\t\t- Decision A\n\t\t- Decision B\n\t</notes>\n"
        "\t<transcript>\n\t\tJane: the secret plan\n"
        '\t\t<page url="https://app.notion.com/p/423ed33606458143b422d74e5eb9926c">x</page>\n'
        "\t</transcript>\n</meeting-notes>\nTail"
    )
    assert md == "**Sprint sync**\n\nWe shipped it.\n\n- Decision A\n- Decision B\n\nTail\n"
    assert to_markdown("<transcript>\nx\n</transcript>").sub_pages == ()


def test_nested_callout_in_list_item_stays_in_the_item() -> None:
    md = clean('- item\n\t<callout icon="⚠️">\n\t\tcareful\n\n\t\t- inner\n\t</callout>\n- next')
    assert md == "- item\n\n\t> ⚠️ careful\n\t>\n\t> - inner\n\n- next\n"


def test_callout_nested_in_toggle_in_callout() -> None:
    md = clean("<callout>\n<toggle>\nT\n<callout>\ndeep\n</callout>\n</toggle>\n</callout>")
    assert md == "> **T**\n>\n> > deep\n"


def test_custom_block_is_dropped_with_its_content() -> None:
    assert (
        clean(
            'before\n<custom-block type="x">\n\thidden\n\t<b>more</b>\n</custom-block>\nafter\n<custom-block a="1" />'
        )
        == "before\nafter\n"
    )


def test_escaped_and_unknown_tags_become_text() -> None:
    md = to_markdown("\\<callout> <script>x</script> <!-- c --> <?php ?> <https://a.test/b> 1 < 2 > 0").markdown
    assert md == "&lt;callout> &lt;script>x&lt;/script> &lt;!-- c --> &lt;?php ?> <https://a.test/b> 1 < 2 > 0\n"


def test_images_are_named_never_linked() -> None:
    md = clean('![a](https://x.test/1.png)\n<image source="https://x.test/2.png">b</image>')
    assert "x.test" not in md
    assert md == "Image: a\n\nImage: b\n"


# Hostile input ----------------------------------------------------------------------------------------------------


def test_tags_injected_in_text_never_open_blocks() -> None:
    page_tag = '<page url="https://app.notion.com/p/423ed33606458143b422d74e5eb9926c">P</page>'
    nfm = f'Intro <callout icon="x"> text </callout> and </toggle> {page_tag} end'
    page = to_markdown(nfm)
    assert page.sub_pages == ()
    assert page.markdown == "Intro  text  and  P end\n"


def test_tag_split_across_lines_is_text() -> None:
    md = clean("<call\nout>\nx\n</callout")
    assert "x" in md


def test_unclosed_and_stray_tags() -> None:
    assert clean("</callout>\n</toggle>\ntext") == "text\n"
    assert clean("<callout>\nopen\n<toggle>\nTitle\nchild") == "> open\n>\n> **Title**\n>\n> child\n"
    assert clean("<b>a <i>b") == "**a *b***\n"
    assert clean("</b></i> x") == "x\n"
    assert to_markdown("```py\nunclosed <b>").markdown == "```py\nunclosed <b>\n"


def test_closing_tag_in_code_does_not_close_the_block() -> None:
    assert clean("<callout>\n```\n</callout>\n```\nstill\n</callout>") == "> ```\n> </callout>\n> ```\n> still\n"


def test_block_depth_is_bounded() -> None:
    ok = "<callout>\n" * (MAX_DEPTH - 1) + "x"
    assert "x" in to_markdown(ok).markdown
    with pytest.raises(MarkupTooDeepError):
        to_markdown("<callout>\n" * (MAX_DEPTH + 1) + "x")
    with pytest.raises(MarkupTooDeepError):
        to_markdown("<toggle>\nTitle\n" * 5000)


def test_inline_depth_is_bounded() -> None:
    with pytest.raises(MarkupTooDeepError):
        to_markdown('<mention url="https://a.test/">' * (MAX_DEPTH + 1))
    with pytest.raises(MarkupTooDeepError):
        to_markdown("<b>" * 100_000)
    assert "x" in to_markdown("<b><i><s>" * 3 + "x").markdown


def test_huge_input_is_cut_and_fast() -> None:
    start = time.monotonic()
    page = to_markdown("word " * (MAX_CHARS // 5 + 1000) + "\ntail")
    assert page.markdown.rstrip().endswith(TRUNCATED)
    assert len(page.markdown) <= MAX_CHARS + 100
    assert "tail" not in page.markdown
    assert time.monotonic() - start < 5


def test_single_giant_line_is_cut() -> None:
    page = to_markdown("x" * (MAX_CHARS + 10))
    assert page.markdown.endswith(f"\n\n{TRUNCATED}\n")


@pytest.mark.parametrize(
    "nfm",
    [
        "`" * 400_000,
        '<a "' * 100_000,
        "<p/>" * 100_000,
        "<tr>" * 100_000,
        "![x" * 100_000,
        "<table>" + "<td>" * 100_000,
        "a@" * 100_000,
        "<code>" * 100_000,
        "\\<" * 100_000,
        "<mention " * 50_000,
        "<<<<>>>>" * 50_000,
        "\x00\ud800 <b>\r\n</b>",
    ],
)
def test_pathological_lines_stay_linear(nfm: str) -> None:
    start = time.monotonic()
    page = to_markdown(nfm)
    assert isinstance(page.markdown, str)
    assert "\x00" not in page.markdown
    assert time.monotonic() - start < 5


def test_output_is_utf8_encodable_and_newline_normalised() -> None:
    md = to_markdown("a\r\nb\rc\x00\ud800").markdown
    md.encode("utf-8")
    assert "\r" not in md


# E-mail and user ids ----------------------------------------------------------------------------------------------


def test_scrub_original_drops_user_id_and_email_from_mentions(markdown: dict[str, str]) -> None:
    raw = markdown[PRD_ID]
    assert "person@example.com" in raw
    scrubbed = scrub_original(raw)
    assert not EMAIL.search(scrubbed)
    assert "00000000-0000-4000-8000-0000000000a1" not in scrubbed
    assert '<mention url="user://">Sample Author</mention>' in scrubbed
    assert scrubbed.count("<mention") == raw.count("<mention")
    assert scrub_original(scrubbed) == scrubbed


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('<mention url="user://a-b/x.y+z@corp.example.com">N</mention>', '<mention url="user://">N</mention>'),
        ("<mention url='user://a/e@x.io'/>", "<mention url='user://'/>"),
        ('<mention url="user://a/e@x.io">e@x.io</mention>', '<mention url="user://">(e-mail hidden)</mention>'),
        ("mail a@b.co, c.d@e-f.org. Done", "mail (e-mail hidden), (e-mail hidden). Done"),
        ("[w](mailto:a@b.co)", "[w](mailto:(e-mail hidden))"),
        ("@handle and a@b and x @ y.com and @@", "@handle and a@b and x @ y.com and @@"),
        ("no at sign", "no at sign"),
        ("a@" * 3, "a@" * 3),
    ],
)
def test_scrub_original_emails(text: str, expected: str) -> None:
    assert scrub_original(text) == expected


def test_to_markdown_hides_emails_in_every_place() -> None:
    md = to_markdown(
        'Contact boss@corp.com.\n\n```\ncode@corp.com\n```\n\n[l](mailto:x@corp.com)\n\n<mention url="user://i/m@corp.com">m@corp.com</mention>\n\n'
        "<table><tr><td>t@corp.com</td></tr></table>"
    ).markdown
    assert "@" not in md
    assert md.count("(e-mail hidden)") == 5


def test_user_id_in_mentions_does_not_reach_the_markdown(markdown: dict[str, str]) -> None:
    for nfm in markdown.values():
        assert "user://" not in to_markdown(nfm).markdown
        assert "00000000-0000-4000-8000-0000000000a1" not in to_markdown(nfm).markdown


# Rows -------------------------------------------------------------------------------------------------------------


def test_render_row_of_a_fixture_row(rows: list[dict[str, Any]], markdown: dict[str, str]) -> None:
    row = next(r for r in rows if r["id"] == "423ed336-0645-8138-9d24-d7f3c76b496d")
    title = row["properties"]["Name"]["title"][0]["plain_text"]
    doc = render_row(title, row["properties"], to_markdown(markdown[row["id"]]))
    assert doc == (
        "# Render points line on checkout summary\n\n"
        "| Property | Value |\n| --- | --- |\n"
        "| Points | 3 |\n| Specflow ID | T-1.1.2 |\n| Status | To do |\n| Type | Task |\n| Work area | frontend |\n\n"
        'Brief: call the estimate endpoint and render "You earn N points" under the total.\n'
    )
    assert "00000000-0000-4000-8000-0000000000a1" not in doc


def test_render_row_of_every_fixture_row_is_clean(rows: list[dict[str, Any]], markdown: dict[str, str]) -> None:
    assert rows
    for row in rows:
        props = row["properties"]
        title = next(p["title"][0]["plain_text"] for p in props.values() if p["type"] == "title")
        doc = render_row(title, props, to_markdown(markdown.get(row["id"], "")))
        assert doc.startswith(f"# {title}\n\n| Property | Value |")
        assert not NFM_TAG.search(doc)
        assert not EMAIL.search(doc)
        assert row["id"] not in doc


def test_render_row_property_types() -> None:
    user = {
        "object": "user",
        "id": "00000000-0000-4000-8000-0000000000a1",
        "name": "Ann Author",
        "person": {"email": "ann@corp.com"},
    }
    props: dict[str, Any] = {
        "Name": {"type": "title", "title": [{"plain_text": "ignored"}]},
        "Owner": {"type": "people", "people": [user, {"object": "user", "id": "u2", "name": "Bob"}, {"id": "u3"}]},
        "Contact": {"type": "email", "email": "ann@corp.com"},
        "Empty mail": {"type": "email", "email": None},
        "Tags": {"type": "multi_select", "multi_select": [{"name": "a"}, {"name": "b|c"}]},
        "Due": {"type": "date", "date": {"start": "2027-01-01", "end": "2027-02-01"}},
        "Day": {"type": "date", "date": {"start": "2027-01-01", "end": None}},
        "Cost": {"type": "number", "number": 2.5},
        "Count": {"type": "number", "number": 4.0},
        "Done": {"type": "checkbox", "checkbox": False},
        "Link": {"type": "url", "url": "https://x.test/a"},
        "Ref": {"type": "unique_id", "unique_id": {"prefix": "REQ", "number": 7}},
        "Files": {"type": "files", "files": [{"name": "spec.pdf", "file": {"url": "https://s3.test/secret?sig=1"}}]},
        "Related": {"type": "relation", "relation": [{"id": "page-id"}], "has_more": False},
        "Named related": {"type": "relation", "relation": [{"id": "page-id", "title": "Other row"}]},
        "By": {"type": "created_by", "created_by": user},
        "Calc": {"type": "formula", "formula": {"type": "string", "string": "x < y <b>"}},
        "Sum": {"type": "rollup", "rollup": {"type": "number", "number": 9, "function": "sum"}},
        "Notes": {"type": "rich_text", "rich_text": [{"plain_text": "mail me at ann@corp.com\nplease"}]},
        "Odd": {"type": "button", "button": {}},
        "Unknown": {"type": "something_new", "something_new": {}},
        "Garbage": "text",
    }
    doc = render_row("A <b>Title</b> | x", props, ConvertedPage("Body line\n"))
    assert doc == (
        "# A &lt;b>Title&lt;/b> | x\n\n"
        "| Property | Value |\n| --- | --- |\n"
        "| By | Ann Author |\n| Calc | x &lt; y &lt;b> |\n| Contact | (e-mail hidden) |\n"
        "| Cost | 2.5 |\n| Count | 4 |\n"
        "| Day | 2027-01-01 |\n| Done | No |\n| Due | 2027-01-01 to 2027-02-01 |\n| Files | spec.pdf |\n"
        "| Link | https://x.test/a |\n| Named related | Other row |\n| Notes | mail me at (e-mail hidden) please |\n"
        "| Owner | Ann Author, Bob |\n| Ref | REQ-7 |\n| Sum | 9 |\n| Tags | a, b\\|c |\n\n"
        "Body line\n"
    )
    for secret in ("ann@corp.com", "00000000-0000-4000-8000-0000000000a1", "s3.test", "page-id"):
        assert secret not in doc


def test_render_row_without_properties_or_body() -> None:
    assert render_row("", {}, ConvertedPage("")) == "# Untitled\n"
    assert render_row("T", {"Name": {"type": "title", "title": []}}, ConvertedPage("b\n")) == "# T\n\nb\n"


# Review fixes -----------------------------------------------------------------------------------------------------


def test_render_row_real_people_files_and_rollup_shapes(rows: list[dict[str, Any]]) -> None:
    owner = next(r["properties"]["Owner"] for r in rows if r["properties"].get("Owner", {}).get("people"))
    person = {"object": "user", "id": "u1", "name": "Pat", "type": "person", "person": {"email": "pat@corp.com"}}
    bot = {"object": "user", "id": "b1", "name": "Sync Bot", "type": "bot", "bot": {}}
    props = {
        "Owner": owner,
        "Bots": {"type": "people", "people": [bot, person]},
        "Spec": {
            "type": "files",
            "files": [
                {"name": "a.pdf", "type": "file", "file": {"url": "https://s3.test/x?sig=1"}},
                {"name": "b.png", "type": "external", "external": {"url": "https://x.test/b.png"}},
            ],
        },
        "All": {"type": "rollup", "rollup": {"type": "array", "array": [{"type": "people", "people": [person]}]}},
    }
    doc = render_row("T", props, ConvertedPage(""))
    assert "| Owner | Sample Author |" in doc
    assert "| Bots | Sync Bot, Pat |" in doc
    assert "| Spec | a.pdf, b.png |" in doc
    assert "| All | Pat |" in doc
    assert "s3.test" not in doc
    assert "@" not in doc


@pytest.mark.parametrize(
    "text",
    [
        "[Jo](user://abc/e@x.com)",
        "`user://abc/e@x.com`",
        "USER://abc-def/e@x.com",
        '<span data-x="user://abc/e@x.com">t</span>',
        "see User://abc",
    ],
)
def test_user_ids_never_survive_outside_mention_tags(text: str) -> None:
    assert "abc" not in scrub_original(text)
    assert "user://abc" not in scrub_original(text).lower()
    md = to_markdown(text).markdown
    assert "abc" not in md
    assert "user://" not in md.lower()
    assert "@" not in md
    doc = render_row("T", {"N": {"type": "rich_text", "rich_text": [{"plain_text": text}]}}, ConvertedPage(""))
    assert "abc" not in doc
    assert "user://" not in doc.lower()


@pytest.mark.parametrize(
    "address",
    ["jö@x.com", "o'brien@x.com", "first.last+tag@sub.example.co.uk", "a" * 120 + "@x.com", "名前@x.com"],
)
def test_scrub_hides_unusual_addresses_fully(address: str) -> None:
    assert scrub_original(f"mail {address}, ok") == "mail (e-mail hidden), ok"


def test_autolinks_are_kept_only_when_safe() -> None:
    assert (
        to_markdown("<javascript:alert(1)> <https://x.test/a>").markdown
        == "&lt;javascript:alert(1)> <https://x.test/a>\n"
    )


def test_database_refs_are_validated_like_pages() -> None:
    good = "https://app.notion.com/p/423ed3360645819b8a30d73a847eb5e6"
    page = to_markdown(
        '<database url="javascript:alert(1)">A</database>\n'
        f'<database url="{good}" data-source-url="collection://----">B</database>\n'
        f'<database url="{good}" data-source-url="collection://zzzzzzzz-0645-819d-aec9-000b2cecb51b">C</database>'
    )
    assert [(d.title, d.data_source_id) for d in page.databases] == [("B", None), ("C", None)]
    assert "javascript" not in page.markdown
