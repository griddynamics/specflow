"""References between units for the edge catalog offline."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from graph.build.models import CanonicalNode, FragmentRec, Unit
from graph.build.references import REFERENCED_CAP, ReferenceFinder, requirement_labels, section_hub_labels
from graph_support import frozen_ontology, shipped_ontology

ONT = shipped_ontology()
FR = "FunctionalRequirement"
TITLES = {"a": "Checkout Specification", "b": "Billing Guide", "c": "Operations Handbook", "z": "Appendix Tables"}
HEADINGS: dict[str, tuple[str, ...]] = {
    "a#0": ("Checkout Specification", "1 Overview"),
    "a#1": ("Checkout Specification", "2 Cart"),
    "a#2": ("Checkout Specification", "4.2 Payments"),
    "a#3": ("Checkout Specification", "7 Shipping"),
    "b#0": ("Billing Guide", "4.2 Invoices"),
    "b#1": ("Billing Guide", "4.2 Invoices", "4.2.1 Taxes"),
    "b#2": ("Billing Guide", "5. Refunds"),
    "c#0": ("Operations Handbook", "4.2 Hosting"),
    "c#1": ("Operations Handbook", "Payments API"),
    "c#2": ("Operations Handbook", "4.21 Backups"),
    "z#0": ("Appendix Tables", "§9 Limits"),
}
UNITS = {"a1": ("a#0", "a#1"), "a2": ("a#2", "a#3"), "b": ("b#0", "b#1", "b#2"), "c": ("c#0", "c#1", "c#2")}
UNITS["z"] = ("z#0",)
# requirement per fragment (r-<fragment>), a hub in b#0 (never added by a section), an id-keyed requirement
NODES = [
    *(
        CanonicalNode(
            cid=f"r-{fid}", identity_key=f"k-{fid}", name=f"Rule {fid}", label=FR, member_ids=(), fragment_ids=(fid,)
        )
        for fid in HEADINGS
        if fid not in {"c#1", "z#0"}
    ),
    CanonicalNode(
        cid="m-b", identity_key="k-m-b", name="Billing", label="Module", member_ids=(), fragment_ids=("b#0",)
    ),
    CanonicalNode(
        cid="r-fr7",
        identity_key="FR|id|FR-7",
        name="FR-7: refunds are logged",
        label=FR,
        member_ids=(),
        explicit_ids=("FR-7",),
        fragment_ids=("c#1",),
    ),
    CanonicalNode(
        cid="r-fr1",
        identity_key="FR|id|FR-1",
        name="FR-1: cart totals",
        label=FR,
        member_ids=(),
        explicit_ids=("FR-1",),
        fragment_ids=("a#1",),
    ),
]


def fragment_of(fragment_id: str, heading_path: tuple[str, ...], text: str) -> FragmentRec:
    document_id, ordinal = fragment_id.split("#")
    return FragmentRec(
        fragment_id=fragment_id,
        document_id=document_id,
        heading_path=heading_path,
        ordinal=int(ordinal),
        line_start=1,
        line_end=1,
        text=text,
        content_hash=f"h-{fragment_id}",
        tokens=10,
    )


def fragment(fragment_id: str, text: str) -> FragmentRec:
    return fragment_of(fragment_id, HEADINGS[fragment_id], text)


def unit_of(fragment_ids: Sequence[str]) -> Unit:
    return Unit(
        unit_id=f"unit:{fragment_ids[0]}",
        document_id=fragment_ids[0].split("#")[0],
        fragment_ids=tuple(fragment_ids),
        tokens=10,
        content_hash="u",
    )


def unit(name: str) -> Unit:
    return unit_of(UNITS[name])


def make_finder(
    fragments: Sequence[FragmentRec],
    units: Sequence[Unit],
    titles: dict[str, str],
    nodes: Sequence[CanonicalNode],
    *,
    cap: int = REFERENCED_CAP,
    hub_labels: Sequence[str] = (),
) -> ReferenceFinder:
    return ReferenceFinder(
        units=units,
        fragments_by_id={item.fragment_id: item for item in fragments},
        titles=titles,
        canonical=nodes,
        labels=requirement_labels(ONT),
        id_families=sorted({family for node_type in ONT.node_types.values() for family in node_type.id_families}),
        hub_labels=hub_labels,
        cap=cap,
    )


def finder(
    texts: dict[str, str], nodes: Sequence[CanonicalNode] = NODES, hub_labels: Sequence[str] = ()
) -> ReferenceFinder:
    fragments = [fragment(fid, texts.get(fid, "Plain text.")) for fid in HEADINGS]
    return make_finder(fragments, [unit(name) for name in UNITS], TITLES, nodes, hub_labels=hub_labels)


@pytest.mark.parametrize(
    ("text", "entries", "rule"),
    [
        # 2 scoped by 1: the Billing Guide's 4.2 holds b#0 and its subsection b#1 (not the hub m-b)
        ("See section 4.2 of the Billing Guide for totals.", ["r-b#0", "r-b#1"], "section_number"),
        # 2 unscoped: 4.2 in two other documents (b, c) is ambiguous; the same document's 4.2 is never reached
        ("As in §4.2, totals are rounded.", [], None),
        ("§ 5 applies to refunds.", ["r-b#2"], "section_number"),  # "5. Refunds": one document has a 5
        ("Taxes follow section 4.2.1.", ["r-b#1"], "section_number"),  # any depth, exact number
        ("Section 4.2 of the Operations Handbook covers hosting.", ["r-c#0"], "section_number"),  # not 4.21
        ("§9 caps the cart.", [], "section_number"),  # found ("§9 Limits"), but it holds no requirement
        ("Shipping is in section 7.", ["r-a#3"], "section_number"),  # only another unit of this document
        ("Section 1 says so.", [], None),  # "1 Overview" is in this unit: ignored
        ("Section 3 is missing.", [], None),
        # 1 alone adds nothing
        ("The Billing Guide is long.", [], None),
        # 3: a titled section, unique, by heading in hub form; a document title scopes as 1
        ("This is described in the Payments API section.", ["r-fr7"], "section_title"),
        ("Hosting is defined in the Operations Handbook document, section 4.2.", ["r-c#0"], "section_number"),
        ("See the Invoices section.", ["r-b#0", "r-b#1"], "section_title"),
        ("See the next section.", [], None),
        # 4: an explicit id of a node grounded in another unit; one grounded in this unit is ignored
        ("Refunds follow FR-7 and FR-1.", ["r-fr7"], "explicit_id"),
    ],
)
def test_reference_patterns(text: str, entries: list[str], rule: str | None) -> None:
    found = finder({"a#0": text}).for_unit(unit("a1"))
    assert list(found.cids) == entries
    assert [reference.rule for reference in found.found.get("a#0", ())][:1] == ([rule] if rule else [])
    assert found.capped == 0


def test_sections_rank_before_ids_each_once_and_fragments_list_theirs_in_text_order() -> None:
    found = finder({"a#0": "See FR-7 and § 5.", "a#1": "Again § 5; and section 7."}).for_unit(unit("a1"))
    assert found.cids == ("r-b#2", "r-a#3", "r-fr7")
    assert dict(found.by_fragment) == {"a#0": ("r-fr7", "r-b#2"), "a#1": ("r-b#2", "r-a#3")}


def test_each_entry_keeps_the_first_sentence_that_refers_to_it() -> None:
    """The catalog line of a referenced entry shows the unit's sentence that refers to it (
    a name alone gave the model nothing to link on)."""

    texts = {"a#0": "Totals are rounded. See FR-7 and § 5.", "a#1": "Again § 5 applies; and section 7.\nLast line."}
    found = finder(texts).for_unit(unit("a1"))
    assert dict(found.sentences) == {
        "r-fr7": "See FR-7 and § 5.",
        "r-b#2": "See FR-7 and § 5.",
        "r-a#3": "and section 7.",
    }
    assert set(found.sentences) == set(found.cids)
    assert [reference.sentence for reference in found.found["a#1"]] == ["Again § 5 applies;", "and section 7."]


def test_a_unit_takes_at_most_the_cap_and_counts_the_rest() -> None:
    extra = [
        CanonicalNode(
            cid=f"r-z{index:02}",
            identity_key=f"kz{index}",
            name=f"Z {index}",
            label=FR,
            member_ids=(),
            fragment_ids=("z#0",),
        )
        for index in range(REFERENCED_CAP + 5)
    ]
    found = finder({"a#0": "§9 caps the cart."}, [*NODES, *extra]).for_unit(unit("a1"))
    assert REFERENCED_CAP == 30
    assert found.cids == tuple(f"r-z{index:02}" for index in range(REFERENCED_CAP))
    assert found.capped == 5
    assert len(found.by_fragment["a#0"]) == REFERENCED_CAP + 5
    assert set(found.sentences) == set(found.cids)  # the capped entries keep none


def test_a_reference_into_the_unit_itself_adds_nothing() -> None:
    # c's own text names its own section and its own node: both are in the unit already
    found = finder({"c#0": "See the Payments API section and FR-7."}).for_unit(unit("c"))
    assert found.cids == ()


def hub(cid: str, label: str, fragment_id: str) -> CanonicalNode:
    return CanonicalNode(
        cid=cid, identity_key=f"k-{cid}", name=cid, label=label, member_ids=(), fragment_ids=(fragment_id,)
    )


def test_the_section_hub_labels_are_the_hubs_a_reference_can_link() -> None:
    # RELATES_TO pairs a requirement with a module, an interface or a data entity; DEPENDS_ON a feature
    # with a feature; a capability links a requirement by neither (the frozen v1.3 ontology has no requirement)
    assert section_hub_labels(ONT) == frozenset({"DataEntity", "Feature", "Interface", "Module"})
    assert section_hub_labels(frozen_ontology()) == frozenset()


def test_a_section_without_a_requirement_offers_its_hubs_ranked_after_every_requirement() -> None:
    """ "§" recall: a section that holds hubs only (a module's description, an API chapter intro) adds
    them, after the requirement entries of every other rank and within the cap."""

    nodes = [*NODES, hub("m-z", "Module", "z#0"), hub("i-z", "Interface", "z#0"), hub("cap-z", "Capability", "z#0")]
    hubs = sorted(section_hub_labels(ONT))
    texts = {"a#0": "§9 caps the cart.", "a#1": "Refunds follow FR-7; see Chapter 5."}
    found = finder(texts, nodes, hubs).for_unit(unit("a1"))
    # the requirement ranks (section, id, chapter) first; z#0's module and interface last; never the capability
    assert found.cids == ("r-fr7", "r-b#2", "i-z", "m-z")
    assert found.by_fragment["a#0"] == ("i-z", "m-z")
    assert found.sentences["m-z"] == "§9 caps the cart."
    # without hub labels, or when the section holds a requirement, no hub is added
    assert finder(texts, nodes).for_unit(unit("a1")).cids == ("r-fr7", "r-b#2")
    held = finder({"a#0": "See section 4.2 of the Billing Guide."}, nodes, hubs).for_unit(unit("a1"))
    assert held.cids == ("r-b#0", "r-b#1")  # m-b in b#0 stays out: the section has requirements
    capped = make_finder(
        [fragment(fid, texts.get(fid, "Plain text.")) for fid in HEADINGS],
        [unit(name) for name in UNITS],
        TITLES,
        nodes,
        cap=2,
        hub_labels=hubs,
    ).for_unit(unit("a1"))
    assert capped.cids == ("r-fr7", "r-b#2") and capped.capped == 2


def test_a_section_offers_the_requirements_closest_to_the_referring_sentence_first() -> None:
    """ "§" recall: a big section's requirements are ranked by the content words they share with the
    referring sentence (rarer words weigh more) before the cap, so the one the sentence is about gets in."""

    first = CanonicalNode(
        cid="r-x1", identity_key="k-x1", name="Invoices are numbered", label=FR, member_ids=(), fragment_ids=("b#0",)
    )
    second = CanonicalNode(
        cid="r-x2",
        identity_key="k-x2",
        name="Invoice PDFs are archived for ten years",
        label=FR,
        member_ids=(),
        fragment_ids=("b#1",),
    )
    nodes = [node for node in NODES if node.cid not in {"r-b#0", "r-b#1"}] + [first, second]
    text = "Archived PDFs follow section 4.2 of the Billing Guide."
    assert finder({"a#0": text}, nodes).for_unit(unit("a1")).cids == ("r-x2", "r-x1")
    # no shared word: plan order
    assert finder({"a#0": "See section 4.2 of the Billing Guide."}, nodes).for_unit(unit("a1")).cids == ("r-x1", "r-x2")
    capped = make_finder(
        [fragment(fid, {"a#0": text}.get(fid, "Plain text.")) for fid in HEADINGS],
        [unit(name) for name in UNITS],
        TITLES,
        nodes,
        cap=1,
    ).for_unit(unit("a1"))
    assert capped.cids == ("r-x2",) and capped.capped == 1


def test_the_same_text_gives_the_same_references() -> None:
    texts = {"a#0": "See section 4.2 of the Billing Guide; FR-7 too.", "a#1": "Described in the Invoices section."}
    first = finder(texts).for_unit(unit("a1"))
    again = finder(texts, list(reversed(NODES))).for_unit(unit("a1"))
    assert first == again


# The findings corpus: the source unit s, a functional document f whose subsections 7.1, 9.5 and
# 9.7 are merged into their chapter fragments (heading lines in the text), a quality handbook q, and an
# appendix g with a glossary and trace tables.
SCOPE_TITLES = {
    "s": "Scope Document",
    "f": "Platform Functional Requirements",
    "q": "Quality Handbook",
    "g": "Reference Appendix",
}
SCOPE_FRAGMENTS = {
    "s#0": (("Scope Document", "1. Scope"), "Plain text."),
    "f#0": (("Platform Functional Requirements", "7. Documents"), "Intro.\n### 7.1 Upload Rules\nRules apply."),
    "f#1": (("Platform Functional Requirements", "7. Documents", "7.2 Limits"), "### 7.2 Limits\nLimits."),
    "f#2": (("Platform Functional Requirements", "7. Documents", "7.3 Duplicates"), "Duplicates."),
    "f#3": (
        ("Platform Functional Requirements", "9. Graph"),
        "### 9.5 Contested Items\nText.\n```\n# 9.6 Not a heading\n```\n9.7 Ready State\nText.\n### 12.1 Shared",
    ),
    "q#0": (("Quality Handbook", "26. Quality"), "Intro.\n### 12.1 Shared"),
    "q#1": (("Quality Handbook", "26. Quality", "26.5 Usability"), "Usability."),
    "q#2": (("Quality Handbook", "7. Testing"), "Testing."),
    "g#0": (("Reference Appendix", "Appendix A. Glossary"), "| Gate | the lock | Section 7.2 |"),
    "g#1": (("Reference Appendix", "Appendix B. Trace"), "| BUS-1 | FR-1, FR-2 |\n| BUS-2 | FR-3 |"),
    "g#2": (("Reference Appendix", "Appendix C. Use Case Trace"), "| UC-9 | FR-3, FR-2 | Chapter 26 |"),
}
SCOPE_UNITS = [("s#0",), ("f#0", "f#1", "f#2", "f#3"), ("q#0", "q#1", "q#2"), ("g#0", "g#1", "g#2")]
SCOPE_NODES = [
    *(
        CanonicalNode(
            cid=f"r-{fid}", identity_key=f"k-{fid}", name=f"Rule {fid}", label=FR, member_ids=(), fragment_ids=(fid,)
        )
        for fid in SCOPE_FRAGMENTS
        if fid[0] in "fq"
    ),
    *(
        CanonicalNode(
            cid=f"r-fr{number}",
            identity_key=f"FR|id|FR-{number}",
            name=f"FR-{number}",
            label=FR,
            member_ids=(),
            explicit_ids=(f"FR-{number}",),
            fragment_ids=(fid,),
        )
        for number, fid in ((1, "q#1"), (2, "q#2"), (3, "q#2"))
    ),
]


def scope_finder(texts: dict[str, str], cap: int = REFERENCED_CAP) -> ReferenceFinder:
    fragments = [fragment_of(fid, path, texts.get(fid, text)) for fid, (path, text) in SCOPE_FRAGMENTS.items()]
    return make_finder(fragments, [unit_of(ids) for ids in SCOPE_UNITS], SCOPE_TITLES, SCOPE_NODES, cap=cap)


@pytest.mark.parametrize(
    ("text", "entries", "rules"),
    [
        # merged subsections (R09, R11-R21): the heading line inside the chapter fragment's text
        ("The KPIs are computed in the install (see Section 7.1).", ["r-f#0"], ["section_number"]),
        ("Contested items follow Section 9.5.", ["r-f#3"], ["section_number"]),
        ("Ready state is recorded (see Section 9.7).", ["r-f#3"], ["section_number"]),  # numbered heading line
        ("Section 9.6 is code.", [], []),  # a heading line inside a code fence is no heading
        ("Section 12.1 is shared.", [], []),  # merged in two documents: not unique
        # plural lists and ranges (R10): each section
        ("The API applies the rules of Sections 7.1 to 7.3.", ["r-f#0", "r-f#1", "r-f#2"], ["section_number"] * 3),
        ("Limits are in Sections 7.2 and 7.3.", ["r-f#1", "r-f#2"], ["section_number"] * 2),
        ("See §§ 7.1\u20137.2.", ["r-f#0", "r-f#1"], ["section_number"] * 2),
        ("Sections 7.3 to 7.1 give the ends only.", ["r-f#2", "r-f#0"], ["section_number"] * 2),
        ("Section 7.2 and 7.3 is singular: one section.", ["r-f#1"], ["section_number"]),
        # chapters (R22, R24): the top-level heading N in another document, unique or scoped
        (
            "WCAG applies (see the usability requirements in Chapter 26).",
            ["r-q#0", "r-fr1", "r-q#1"],
            ["chapter_number"],
        ),
        ("Chapter 7 holds the rules.", [], []),  # f and q both have a chapter 7
        ("Chapter 7 of the Quality Handbook covers tests.", ["r-fr2", "r-fr3", "r-q#2"], ["chapter_number"]),
        ("Chapters 9 and 26 apply.", ["r-f#3", "r-q#0", "r-fr1", "r-q#1"], ["chapter_number"] * 2),
        ("Chapter 1 is this document's own.", [], []),
        # a named document (N01, N02): a corpus title's words scope; any other name resolves nothing
        ("Uploads follow section 7.2 of the Functional Requirements document.", ["r-f#1"], ["section_number"]),
        ("Checkout specification, section 7.2.", [], []),
        ("Section 7.2 of the example checkout spec abandons payment.", [], []),
        ("The timeout is in `checkout-spec.pdf`, section 7.2.", [], []),
        # literals (N01, N03): inline code and double quotes are illustrations
        ('{"label": "30 seconds", "rationale": "Limits, section 7.2."}', [], []),
        ("| Requirement | `<PREFIX>-<NNN>` | `FR-1` | See Table 1.3 |", [], []),
        ("| Requirement | FR-1 | See Table 1.3 |", ["r-fr1"], ["explicit_id"]),
    ],
)
def test_reference_findings(text: str, entries: list[str], rules: list[str]) -> None:
    found = scope_finder({"s#0": text}).for_unit(unit_of(("s#0",)))
    assert list(found.cids) == entries
    assert [reference.rule for reference in found.found.get("s#0", ())] == rules


# Two documents, each with a section 4.2: a file name next to the number scopes it to the document of
# that file name, never to the citing document. Billing's title is "Invoicing Guide" (not in the sentences)
# or "Billing" (its title also scopes the sentence: rule 1).
def file_finder(text: str, billing_title: str) -> tuple[ReferenceFinder, Unit]:
    fragments = [
        fragment_of("specs/checkout.md#0", ("Checkout", "Intro"), text),
        fragment_of("specs/checkout.md#1", ("Checkout", "4.2 Payments"), "Pay."),
        fragment_of("specs/billing.md#0", (billing_title, "4.2 Invoices"), "Invoice."),
    ]
    units = [unit_of((item.fragment_id,)) for item in fragments]
    nodes = [
        CanonicalNode(
            cid=f"r-{fid}", identity_key=f"k-{fid}", name=f"R {fid}", label=FR, member_ids=(), fragment_ids=(fid,)
        )
        for fid in ("specs/checkout.md#1", "specs/billing.md#0")
    ]
    titles = {"specs/checkout.md": "Checkout", "specs/billing.md": billing_title}
    return make_finder(fragments, units, titles, nodes), units[0]


@pytest.mark.parametrize("billing_title", ["Invoicing Guide", "Billing"])
@pytest.mark.parametrize(
    ("text", "entries"),
    [
        ("Totals: see section 4.2 in `notes.md`.", []),  # a file outside the corpus: nothing
        ("Totals: see section 4.2 in `billing.md`.", ["r-specs/billing.md#0"]),  # inline code still names a file
        ("Totals: see section 4.2 in billing.md.", ["r-specs/billing.md#0"]),
        ("Totals: billing.md, section 4.2.", ["r-specs/billing.md#0"]),  # named before the number
        ("Totals: see section 4.2 in checkout.md.", ["r-specs/checkout.md#1"]),  # the citing document's own file
    ],
)
def test_a_file_name_scopes_the_number_to_that_file(text: str, entries: list[str], billing_title: str) -> None:
    finder_, source = file_finder(text, billing_title)
    assert list(finder_.for_unit(source).cids) == entries


def test_an_illustration_has_no_references() -> None:
    finder_ = scope_finder({})
    example = fragment_of("s#0", ("Scope Document", "Appendix E. Worked Example"), "Section 7.2 abandons it; FR-1.")
    assert finder_.in_fragment(example, "unit:s#0") == []


def test_ids_rank_before_glossary_and_chapters_and_fragments_take_turns_before_the_cap() -> None:
    # unit g (R28-R30): the glossary's Section 7.2 comes first in text but ranks after every trace id; the two
    # trace tables take turns, so the second table's first id is in before the first table's second
    finder_ = scope_finder({}, cap=4)
    found = finder_.for_unit(unit_of(("g#0", "g#1", "g#2")))
    assert found.cids == ("r-fr1", "r-fr3", "r-fr2", "r-f#1")
    assert found.capped == 2  # chapter 26 adds r-q#0 and r-q#1; its r-fr1 is in already
    assert dict(found.by_fragment) == {
        "g#0": ("r-f#1",),
        "g#1": ("r-fr1", "r-fr2", "r-fr3"),
        "g#2": ("r-fr3", "r-fr2", "r-fr1", "r-q#0", "r-q#1"),  # chapter 26 by overlap: "FR" shared with r-fr1
    }
