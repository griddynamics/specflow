"""Change set, fragment matching and dirty fragments offline (build.md Plan 2, 4 and 5; tech-specs 5.4;
FR-BLD-02, 03, 04, 05, 12).

Fragments are hand-built ``FragmentRec``s whose content hash is the sha256 of their text, so equal
text means equal content; persisted fragments are the same records as ``persisted_fragments`` returns
them (``change`` at its default).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import pytest

from graph.build.changes import (
    LINE_JACCARD,
    change_set,
    dirty_ids,
    line_jaccard,
    match_fragments,
    needs_retry,
    persisted_fragments,
)
from graph.build.models import DocumentRec, FragmentRec, FragmentStatus
from graph.build.ports import CorpusRevision
from graph.build.queries import READ_PERSISTED_FRAGMENTS
from graph_support import OfflineExecutor


def frag(
    fragment_id: str,
    lines: Sequence[str],
    *,
    heading: tuple[str, ...] | None = None,
    ordinal: int = 0,
    status: FragmentStatus = "active",
    extracted_hash: str | None = None,
    error_code: str | None = None,
) -> FragmentRec:
    text = "\n".join(lines)
    document_id = fragment_id.split("#", maxsplit=1)[0]
    return FragmentRec(
        fragment_id=fragment_id,
        document_id=document_id,
        heading_path=heading if heading is not None else (document_id, fragment_id.split("#")[1]),
        ordinal=ordinal,
        line_start=ordinal * 10 + 1,
        line_end=ordinal * 10 + len(lines),
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        tokens=len(lines),
        status=status,
        extracted_hash=extracted_hash,
        error_code=error_code,
    )


def stored(fragment: FragmentRec, **update: object) -> FragmentRec:
    """``fragment`` as persisted after a successful extraction (``extracted_hash`` = content hash)."""

    return fragment.model_copy(update={"extracted_hash": fragment.content_hash, **update})


def changes(fragments: Sequence[FragmentRec]) -> dict[str, tuple[str, str | None]]:
    return {fragment.fragment_id: (fragment.change, fragment.previous_id) for fragment in fragments}


INTRO = ["# Orders", "The orders service keeps every order."]
EXPORT = ["## Export", "Staff can export orders as CSV.", "Exports finish within 5 s.", "Exports are audited."]
EXPORT_EDITED = ["## Export", "Staff can export orders as CSV.", "Exports finish within 10 s.", "Exports are audited."]
AUDIT = ["## Audit", "Every change is logged.", "Logs are kept for a year."]


# Change set (build.md Plan 2, FR-BLD-02)


def revision(lineage: str, revision_id: str) -> CorpusRevision:
    return CorpusRevision(
        lineage_id=lineage, revision_id=revision_id, version=1, path=lineage, title=lineage, markdown=""
    )


def document(lineage: str, revision_id: str, status: str = "active") -> DocumentRec:
    return DocumentRec(
        document_id=lineage, revision_id=revision_id, version=1, path=lineage, title=lineage, status=status
    )


def test_change_set_holds_exactly_the_new_revised_and_removed_documents() -> None:
    result = change_set(
        [revision("new.md", "r1"), revision("same.md", "r1"), revision("edited.md", "r2"), revision("back.md", "r1")],
        [
            document("same.md", "r1"),
            document("edited.md", "r1"),
            document("gone.md", "r1"),
            document("back.md", "r1", "removed"),
        ],
        retry_lineages=["same.md", "edited.md", "gone.md"],
    )
    # a removed lineage that returns is new again; a retry of a revised or missing lineage is not a retry
    assert (result.new, result.revised, result.unchanged, result.removed, result.retried) == (
        ("new.md", "back.md"),
        ("edited.md",),
        ("same.md",),
        ("gone.md",),
        ("same.md",),
    )


# Line Jaccard (tech-specs 5.4)


def test_line_jaccard_compares_sets_of_stripped_non_blank_lines() -> None:
    assert line_jaccard("a\n b \n\nc", "c\na\nb\n") == 1.0
    assert line_jaccard("a\nb", "b\nc") == pytest.approx(1 / 3)
    assert line_jaccard("a\na\nb", "a\nb\nb") == 1.0  # sets, not multisets
    assert line_jaccard("", "\n  \n") == 0.0
    assert LINE_JACCARD == 0.5


# Fragment matching (build.md Plan 4, FR-BLD-04, FR-BLD-05)


def test_same_id_and_content_is_unchanged_and_keeps_its_persisted_state() -> None:
    intro = frag("o.md#intro#aaaaaaaa", INTRO)
    audit = frag("o.md#audit#cccccccc", AUDIT, ordinal=2)
    persisted = [stored(intro), stored(audit, status="skipped", error_code="llm_malformed", extracted_hash=None)]
    result, retired = match_fragments([intro, audit], persisted)
    assert changes(result) == {intro.fragment_id: ("unchanged", None), audit.fragment_id: ("unchanged", None)}
    assert [(f.status, f.extracted_hash, f.error_code) for f in result] == [
        ("active", intro.content_hash, None),
        ("skipped", None, "llm_malformed"),
    ]
    assert retired == ()


def test_same_content_under_another_id_is_unchanged_and_the_old_id_retires() -> None:
    old = frag("o.md#export#bbbbbbbb", EXPORT, ordinal=1)
    moved = frag("o.md#exports#bbbbbbbb", EXPORT, heading=("o.md", "exports"), ordinal=1)
    result, retired = match_fragments([moved], [stored(old)])
    assert changes(result) == {moved.fragment_id: ("unchanged", old.fragment_id)}
    assert result[0].extracted_hash == old.content_hash
    assert retired == (old.fragment_id,)


def test_same_heading_path_with_mostly_the_same_lines_is_changed() -> None:
    old = frag("o.md#export#bbbbbbbb", EXPORT, ordinal=1)
    edited = frag("o.md#export#bbbbbbbb", EXPORT_EDITED, ordinal=1)
    assert line_jaccard(old.text, edited.text) == pytest.approx(3 / 5)
    result, retired = match_fragments([edited], [stored(old)])
    # the same id: no previous_id, nothing retires; the last extracted version is kept (FR-BLD-12)
    assert changes(result) == {edited.fragment_id: ("changed", None)}
    assert (result[0].status, result[0].extracted_hash) == ("active", old.content_hash)
    assert retired == ()

    # an edit of the first body line gives another id: the match names it, and the old id retires
    reworded = frag("o.md#export#dddddddd", ["## Export", "Staff may export orders as CSV.", *EXPORT[2:]], ordinal=1)
    result, retired = match_fragments([reworded], [stored(old)])
    assert changes(result) == {reworded.fragment_id: ("changed", old.fragment_id)}
    assert result[0].extracted_hash == old.content_hash
    assert retired == (old.fragment_id,)


def test_a_changed_skipped_fragment_keeps_its_last_extracted_hash() -> None:
    old = frag("o.md#export#bbbbbbbb", EXPORT, ordinal=1)
    edited = frag("o.md#export#bbbbbbbb", EXPORT_EDITED, ordinal=1)
    result, _ = match_fragments([edited], [stored(old, status="skipped", error_code="llm_refused")])
    assert (result[0].change, result[0].extracted_hash) == ("changed", old.content_hash)
    # the error of the old version is not carried: the new content is extracted afresh
    assert (result[0].status, result[0].error_code) == ("active", None)


@pytest.mark.parametrize(
    ("new_lines", "heading", "expected"),
    [
        # Jaccard exactly 0.5 (2 shared of 4 distinct lines) is still a change
        (["## Export", "Staff can export orders as CSV.", "Exports are signed."], None, "changed"),
        # below 0.5 (2 shared of 5) is new
        (["## Export", "Staff can export orders as CSV.", "Exports are signed.", "Exports are zipped."], None, "new"),
    ],
)
def test_the_line_jaccard_threshold_and_heading_path_decide_changed_or_new(
    new_lines: list[str], heading: tuple[str, ...] | None, expected: str
) -> None:
    old = frag(
        "o.md#export#bbbbbbbb",
        ["## Export", "Staff can export orders as CSV.", "Exports finish within 5 s."],
        ordinal=1,
    )
    current = frag("o.md#export#eeeeeeee", new_lines, heading=heading, ordinal=1)
    result, retired = match_fragments([current], [stored(old)])
    assert result[0].change == expected
    assert result[0].previous_id == (old.fragment_id if expected == "changed" else None)
    assert retired == (old.fragment_id,)


HEADING_OLD = ["## Export", "Staff can export orders as CSV.", "Exports finish within 5 s.", "Exports are audited."]


@pytest.mark.parametrize(
    "new_lines",
    [
        # the same line set (Jaccard 1.0) in another text: indented and with a blank line
        ["## Export", "  Staff can export orders as CSV.", "", "Exports finish within 5 s.", "Exports are audited."],
        # near-identical: 3 of 5 distinct lines shared (Jaccard 0.6)
        ["## Export", "Staff can export orders as CSV.", "Exports finish within 10 s.", "Exports are audited."],
    ],
)
@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        (None, "changed"),  # the same heading path: a change of the old fragment
        (("o.md", "export", "details"), "new"),  # another heading path never matches as changed
        (("o.md", "Export"), "new"),  # the path compares exactly
    ],
)
def test_only_the_same_heading_path_matches_a_near_identical_fragment_as_changed(
    new_lines: list[str], heading: tuple[str, ...] | None, expected: str
) -> None:
    old = frag("o.md#export#bbbbbbbb", HEADING_OLD, ordinal=1)
    current = frag("o.md#export#eeeeeeee", new_lines, heading=heading, ordinal=1)
    assert current.content_hash != old.content_hash
    assert line_jaccard(current.text, old.text) >= LINE_JACCARD
    result, retired = match_fragments([current], [stored(old)])
    assert result[0].change == expected
    assert result[0].previous_id == (old.fragment_id if expected == "changed" else None)
    assert result[0].extracted_hash == (old.content_hash if expected == "changed" else None)
    assert retired == (old.fragment_id,)


def test_matching_is_one_to_one_and_best_first() -> None:
    far = frag(
        "o.md#export#22222222", ["## Export", "Staff can export orders as CSV.", "Exports are audited.", "Other."]
    )
    near = frag("o.md#export#11111111", [*EXPORT_EDITED, "Extra."], ordinal=1)
    # one current fragment, both persisted ones qualify: it takes the higher Jaccard, whatever the order
    current = frag("o.md#export#33333333", EXPORT_EDITED, ordinal=1)
    assert line_jaccard(current.text, far.text) == pytest.approx(0.6)
    assert line_jaccard(current.text, near.text) == pytest.approx(0.8)
    result, retired = match_fragments([current], [stored(far), stored(near)])
    assert changes(result) == {current.fragment_id: ("changed", near.fragment_id)}
    assert retired == (far.fragment_id, near.fragment_id)  # persisted order

    # two current fragments with one persisted content: the first is unchanged, the second new
    twin_a = frag("o.md#export#bbbbbbbb", EXPORT, ordinal=1)
    twin_b = frag("o.md#export#bbbbbbbb~2", EXPORT, ordinal=2)
    result, retired = match_fragments([twin_a, twin_b], [stored(twin_a)])
    assert changes(result) == {twin_a.fragment_id: ("unchanged", None), twin_b.fragment_id: ("new", None)}
    assert retired == ()


def test_changed_matches_take_the_best_pair_first_across_current_fragments() -> None:
    """The first current fragment would take the persisted one the second matches better; best pairs
    go first, so the first takes the other persisted fragment."""

    old_a = frag("o.md#export#aaaa0001", ["## Export", "a", "b", "c", "d"], ordinal=1)
    old_b = frag("o.md#export#bbbb0002", ["## Export", "a", "b", "x"], ordinal=2)
    first = frag("o.md#export#cccc0003", ["## Export", "a", "b", "x", "y"], ordinal=1)  # 3/7 to A, 0.8 to B
    second = frag("o.md#export#dddd0004", ["## Export", "a", "b", "x", "  "], ordinal=2)  # 1.0 to B, 0.5 to A
    assert line_jaccard(first.text, old_a.text) == pytest.approx(3 / 7)
    assert line_jaccard(first.text, old_b.text) == pytest.approx(0.8)
    assert line_jaccard(second.text, old_b.text) == 1.0
    assert line_jaccard(second.text, old_a.text) == pytest.approx(0.5)
    result, retired = match_fragments([first, second], [stored(old_a), stored(old_b)])
    assert changes(result) == {first.fragment_id: ("new", None), second.fragment_id: ("changed", old_b.fragment_id)}
    assert retired == (old_a.fragment_id, old_b.fragment_id)

    # with a second candidate above the threshold the first fragment is changed from it instead
    old_c = frag("o.md#export#eeee0005", ["## Export", "a", "b", "x", "z"], ordinal=3)
    result, _ = match_fragments([first, second], [stored(old_a), stored(old_b), stored(old_c)])
    assert changes(result) == {
        first.fragment_id: ("changed", old_c.fragment_id),
        second.fragment_id: ("changed", old_b.fragment_id),
    }


def test_dropped_fragments_and_every_fragment_of_a_removed_document_retire() -> None:
    intro = frag("o.md#intro#aaaaaaaa", INTRO)
    export = frag("o.md#export#bbbbbbbb", EXPORT, ordinal=1)
    audit = frag("o.md#audit#cccccccc", AUDIT, ordinal=2)
    gone_a = frag("gone.md#a#aaaaaaaa", INTRO)
    gone_b = frag("gone.md#b#bbbbbbbb", AUDIT, ordinal=1)
    new = frag("n.md#x#ffffffff", ["# New", "A new document."])
    persisted = [stored(gone_a), stored(gone_b), stored(intro), stored(export), stored(audit)]
    # o.md drops its export section; gone.md is removed; n.md is new
    result, retired = match_fragments([intro, audit, new], persisted)
    assert changes(result) == {
        intro.fragment_id: ("unchanged", None),
        audit.fragment_id: ("unchanged", None),
        new.fragment_id: ("new", None),
    }
    assert result[2].extracted_hash is None
    assert retired == (gone_a.fragment_id, gone_b.fragment_id, export.fragment_id)


def test_a_skipped_unchanged_fragment_of_a_retried_lineage_is_retried() -> None:
    skipped = frag("o.md#audit#cccccccc", AUDIT, ordinal=2)
    active = frag("o.md#intro#aaaaaaaa", INTRO)
    other = frag("p.md#audit#cccccccc", AUDIT, ordinal=2)
    persisted = [
        stored(active),
        stored(skipped, status="skipped", error_code="llm_malformed", extracted_hash=None),
        stored(other, status="skipped", error_code="llm_malformed", extracted_hash=None),
    ]
    result, _ = match_fragments([active, skipped, other], persisted, retry_lineages=["o.md"])
    assert changes(result) == {
        active.fragment_id: ("unchanged", None),  # only skipped fragments are retried
        skipped.fragment_id: ("retried", None),
        other.fragment_id: ("unchanged", None),  # its lineage was not asked for
    }
    assert [(f.status, f.error_code) for f in result[1:]] == [("skipped", "llm_malformed")] * 2


def test_needs_retry_is_skipped_or_active_without_its_content_extracted() -> None:
    fragment = frag("o.md#intro#aaaaaaaa", INTRO)
    assert needs_retry(stored(fragment, status="skipped", extracted_hash=None))
    assert needs_retry(fragment)  # active, never extracted
    assert needs_retry(stored(fragment, extracted_hash="an older version"))
    assert not needs_retry(stored(fragment))
    assert not needs_retry(stored(fragment, status="retired", extracted_hash=None))


async def test_unknown_status_and_never_extracted_fragments_of_a_retried_lineage_are_retried() -> None:
    """G8: a persisted status outside the data model is read as ``active`` and never extracted, so a
    retry of its lineage extracts it again, like an ``active`` fragment that was never extracted."""

    weird, never, done = (
        frag("o.md#a#1", INTRO),
        frag("o.md#b#2", EXPORT, ordinal=1),
        frag("o.md#c#3", AUDIT, ordinal=2),
    )
    rows = [
        {**stored(weird).model_dump(mode="json", exclude={"tokens", "change", "previous_id"}), "status": "weird"},
        never.model_dump(mode="json", exclude={"tokens", "change", "previous_id"}),
        stored(done).model_dump(mode="json", exclude={"tokens", "change", "previous_id"}),
    ]
    persisted = await persisted_fragments(OfflineExecutor({READ_PERSISTED_FRAGMENTS.name: rows}), "p")
    assert [(f.status, f.extracted_hash) for f in persisted] == [
        ("active", None),
        ("active", None),
        ("active", done.content_hash),
    ]
    retried, retired = match_fragments([weird, never, done], persisted, retry_lineages=["o.md"])
    assert [f.change for f in retried] == ["retried", "retried", "unchanged"] and retired == ()
    assert dirty_ids(retried, everything=False) == (weird.fragment_id, never.fragment_id)
    plain, _ = match_fragments([weird, never, done], persisted)
    assert [f.change for f in plain] == ["unchanged"] * 3 and dirty_ids(plain, everything=False) == ()


# Dirty fragments (build.md Plan 5, FR-BLD-03)


def test_dirty_fragments_are_new_changed_retried_and_stale_ones_in_document_order() -> None:
    fragments = [
        frag("o.md#a#1", INTRO).model_copy(update={"change": "unchanged"}),
        frag("o.md#b#2", EXPORT, ordinal=1).model_copy(update={"change": "changed"}),
        frag("o.md#c#3", AUDIT, ordinal=2).model_copy(update={"change": "retried"}),
        frag("o.md#d#4", INTRO, ordinal=3).model_copy(update={"change": "unchanged"}),
        frag("p.md#a#5", AUDIT).model_copy(update={"change": "new"}),
    ]
    assert dirty_ids(fragments, everything=False) == ("o.md#b#2", "o.md#c#3", "p.md#a#5")
    # an unchanged fragment without stored nodes for the current fingerprint is extracted again
    assert dirty_ids(fragments, everything=False, stale={"o.md#d#4", "ghost"}) == (
        "o.md#b#2",
        "o.md#c#3",
        "o.md#d#4",
        "p.md#a#5",
    )
    # first and rebuild extract everything
    assert dirty_ids(fragments, everything=True) == tuple(f.fragment_id for f in fragments)
