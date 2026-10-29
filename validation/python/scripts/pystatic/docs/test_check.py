"""Self-test for the docs-bundle checker and index generator over a small neutral bundle."""

from __future__ import annotations

from pathlib import Path

import pytest

from pystatic import config, files
from pystatic.docs import check, indexes, settings
from pystatic.harness.docref import doc_anchor_exists

CONFIG = """\
[docs-bundle]
root = "docs"
root-title = "Bookshop documentation"
types = ["Guideline", "Feature", "Decision"]
tag-folder = "features"
extra-tags = ["general"]
reading-order-heading = "Reading order"

[[docs-bundle.folders]]
path = "guidelines"
title = "Guidelines"
types = ["Guideline"]
order = ["style"]

[[docs-bundle.folders]]
path = "features"
title = "Features"
types = ["Feature"]

[[docs-bundle.folders]]
path = "decisions"
title = "Decisions"
types = ["Decision"]
summary = "One file per decision record"

[[docs-bundle.numbered-records]]
folder = "decisions"
filename = '^adr-\\d{2,}-[a-z0-9-]+\\.md$'
heading = '^(ADR-\\d{2,}) (.+)$'

[[docs-bundle.id-placement]]
pattern = '^`?UI-\\d'
forbidden-folders = ["guidelines"]

[[docs-bundle.ephemeral-exempt]]
file = "guidelines/style.md"
after = '^## Retired'
"""


def _doc(doc_type: str, title: str, tags: str = "[general]", body: str = "") -> str:
    return (
        f"---\ntype: {doc_type}\ntitle: {title}\ndescription: About {title.lower()}\n"
        f"status: draft\ntags: {tags}\n---\n\n# {title}\n\n{body}"
    )


DOCS = {
    "guidelines/style.md": _doc(
        "Guideline",
        "Style",
        body="## Naming\n\n| Id | Rule |\n| --- | --- |\n| `ST-1` | Use nouns |\n\n"
        "## Retired\n\nOld marker ✅ kept for history.\n",
    ),
    "guidelines/testing.md": _doc(
        "Guideline", "Testing", body="See [naming](/guidelines/style.md#naming).\n"
    ),
    "features/checkout.md": _doc(
        "Feature",
        "Checkout",
        tags="[checkout]",
        body="## Reading order\n\n1. [Style](/guidelines/style.md)\n",
    ),
    "decisions/adr-01-use-markdown.md": _doc("Decision", "ADR-01 Use markdown"),
}


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A clean bundle with its generated indexes written."""
    docs = tmp_path / "docs"
    for rel, text in DOCS.items():
        (docs / rel).parent.mkdir(parents=True, exist_ok=True)
        (docs / rel).write_text(text, encoding="utf-8")
    (tmp_path / "pystatic.toml").write_text(CONFIG, encoding="utf-8")
    files.configure(tmp_path)
    config.configure(tmp_path / "pystatic.toml")
    assert indexes.write_or_check(settings.load(), docs, check=False) == 0
    return docs


def _rules(docs: Path) -> list[tuple[str, str]]:
    return [(f.path, f.rule) for f in check.check_bundle(settings.load(), docs)]


def test_doc_anchor_resolves() -> None:
    assert doc_anchor_exists(check.DOC) is True


def test_clean_bundle_has_no_findings(bundle: Path) -> None:
    assert _rules(bundle) == []
    assert check.run().exit_code == 0


def test_generated_root_index_lists_folders_in_order(bundle: Path) -> None:
    root = (bundle / "index.md").read_text(encoding="utf-8")
    assert root.startswith("# Bookshop documentation\n")
    assert root.index("guidelines/style.md") < root.index("guidelines/testing.md")
    assert "* [Decisions](decisions/index.md) - One file per decision record" in root
    assert indexes.write_or_check(settings.load(), bundle, check=True) == 0


def _guideline(title: str, body: str = "", tags: str = "[general]") -> str:
    return _doc("Guideline", title, tags=tags, body=body)


TABLE = "| Id | Rule |\n| --- | --- |\n"
DEFECTS = [
    ("guidelines/extra.md", _doc("Feature", "Extra"), "docs-frontmatter"),
    ("guidelines/untagged.md", "# No frontmatter\n", "docs-frontmatter"),
    ("guidelines/unknown-tag.md", _guideline("Tagged", tags="[nope]"), "docs-frontmatter"),
    ("decisions/adr-2-bad.md", _doc("Decision", "ADR-2 Bad"), "docs-numbered-record"),
    ("guidelines/numbered.md", _guideline("Numbered", "## 1. Intro\n"), "docs-heading"),
    ("guidelines/twice.md", _guideline("Twice", "## A\n\n## A\n"), "docs-heading"),
    ("guidelines/zz-dup.md", _guideline("Dup", f"{TABLE}| `ST-1` | x |\n"), "docs-duplicate-id"),
    ("guidelines/ui.md", _guideline("Ui", f"{TABLE}| `UI-1` | screen |\n"), "docs-id-placement"),
    ("guidelines/wip.md", _guideline("Wip", "Done \u2705\n"), "docs-ephemeral"),
    ("guidelines/cite.md", _guideline("Cite", "See src/app/x.py:12\n"), "docs-ephemeral"),
    ("guidelines/link.md", _guideline("Link", "[x](/guidelines/gone.md)\n"), "docs-link"),
    ("guidelines/anchor.md", _guideline("Anchor", "[x](/guidelines/style.md#gone)\n"), "docs-link"),
]


@pytest.mark.parametrize(("rel", "text", "rule"), DEFECTS, ids=[d[0] for d in DEFECTS])
def test_each_rule_reports_its_defect(bundle: Path, rel: str, text: str, rule: str) -> None:
    (bundle / rel).write_text(text, encoding="utf-8")
    indexes.write_or_check(settings.load(), bundle, check=False)
    assert (rel, rule) in _rules(bundle)


def test_fenced_markers_and_exempt_sections_are_not_ephemeral(bundle: Path) -> None:
    fenced = _doc("Guideline", "Fenced", body="```text\nDone ✅\n```\n")
    (bundle / "guidelines" / "fenced.md").write_text(fenced, encoding="utf-8")
    indexes.write_or_check(settings.load(), bundle, check=False)
    assert _rules(bundle) == []


def test_a_new_document_makes_the_index_stale(bundle: Path) -> None:
    (bundle / "guidelines" / "new.md").write_text(_doc("Guideline", "New"), encoding="utf-8")
    assert ("guidelines/index.md", "docs-index-stale") in _rules(bundle)
    assert indexes.write_or_check(settings.load(), bundle, check=True) == 1


def test_a_linked_document_needs_tags(bundle: Path) -> None:
    (bundle / "guidelines" / "style.md").write_text(
        DOCS["guidelines/style.md"].replace("tags: [general]", "tags: []"), encoding="utf-8"
    )
    assert ("guidelines/style.md", "docs-frontmatter") in _rules(bundle)


def test_missing_bundle_is_a_visible_skip(tmp_path: Path) -> None:
    files.configure(tmp_path)
    assert check.run().detail.startswith("skipped:")
