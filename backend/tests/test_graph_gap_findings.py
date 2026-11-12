"""The findings library over the in-memory executor: add, dedupe, limits, close, withdraw, reads and refresh.

Each scenario states its steps in a comment; the live suite (``test_graph_gap_live.py``) runs the same contract on the
real stores.
"""

from __future__ import annotations

from typing import Any

import pytest

from gap_support import Clock, MemoryGapExecutor, spec_graph
from graph.core.errors import ConfigurationError, GraphWriteError
from graph.core.ontology import load_ontology
from graph.gap import FindingError, FindingRef, Findings

PROJECT = "shop"
CARD, FEATURE, MODULE, REFUND = (
    "REQ::FR::CARD_PAYMENT",
    "FEAT::CARD_PAYMENT",
    "MOD::PAYMENTS_SERVICE",
    "REQ::FR::REFUND",
)


@pytest.fixture
def memory() -> MemoryGapExecutor:
    return spec_graph(MemoryGapExecutor())


@pytest.fixture
def findings(memory: MemoryGapExecutor) -> Findings:
    return Findings(memory, load_ontology(), clock=Clock())


async def add(findings: Findings, **values: Any) -> Any:
    arguments: dict[str, Any] = {
        "created_by": "u1",
        "kind": "gap",
        "text": "The spec is silent.",
        "about": [CARD],
    }
    return await findings.add(PROJECT, **{**arguments, **values})


async def test_add_writes_a_finding_with_its_about_edges_and_one_node_per_reference(
    findings: Findings, memory: MemoryGapExecutor
) -> None:
    # Scenario: u1 adds a finding about two nodes citing a commit and a PR; u2 cites the same commit in another finding.
    first = await add(findings, about=[FEATURE, CARD], refers_to=[("commit", "ABCDEF1"), ("pr", "42")])
    second = await add(findings, created_by="u2", text="Another", refers_to=[("commit", "abcdef1")])

    assert first.created and first.finding.status == "open" and first.finding.origin == "agent"
    assert first.finding.about_spec_ids == (FEATURE, CARD)
    assert [(ref.type, ref.value) for ref in first.finding.refers_to] == [
        ("commit", "abcdef1"),
        ("pr", "42"),
    ]
    assert first.finding.finding_id.startswith("FND::") and len(first.finding.finding_id) == 17
    assert memory.edge_keys("ABOUT") == {
        f"{first.finding.finding_id}|ABOUT|{CARD}",
        f"{first.finding.finding_id}|ABOUT|{FEATURE}",
        f"{second.finding.finding_id}|ABOUT|{CARD}",
    }
    assert len(memory.refs()) == 2  # the commit is one node cited by both findings
    assert len(memory.edge_keys("REFERS_TO")) == 3
    stored = memory.findings()[first.finding.finding_id]
    assert stored["dedupe_key"] and stored["orphaned"] is False and "story_id" not in stored


async def test_add_stores_the_ids_an_alias_resolves_to(findings: Findings, memory: MemoryGapExecutor) -> None:
    # Scenario: REFUND was merged into CARD; a finding about REFUND is stored about CARD.
    memory.merge_spec(REFUND, CARD)

    added = await add(findings, about=[REFUND, CARD])

    assert added.finding.about_spec_ids == (CARD,)
    assert memory.edge_keys("ABOUT") == {f"{added.finding.finding_id}|ABOUT|{CARD}"}


async def test_an_unknown_id_is_refused_naming_it_and_nothing_is_written(
    findings: Findings, memory: MemoryGapExecutor
) -> None:
    with pytest.raises(FindingError) as caught:
        await add(findings, about=[CARD, "REQ::FR::NOPE", "REQ::FR::ALSO_NOPE"])

    assert caught.value.code == "node_not_found"
    assert caught.value.spec_ids == ("REQ::FR::NOPE", "REQ::FR::ALSO_NOPE")
    assert not memory.nodes and not memory.edges


async def test_a_repeat_returns_the_open_finding_and_a_closed_one_does_not(
    findings: Findings,
) -> None:
    # Scenario: u1 adds, repeats with the ids in another order and the text in another case and spacing (same finding,
    # nothing written), another author and another kind add new findings, and after resolving the same add creates one.
    first = await add(findings, about=[CARD, FEATURE], text="Refunds  are\nUNDEFINED")
    again = await add(findings, about=[FEATURE, CARD], text=" refunds are undefined ")
    other_author = await add(findings, created_by="u2", about=[CARD, FEATURE], text="Refunds are undefined")
    other_kind = await add(findings, kind="question", about=[CARD, FEATURE], text="Refunds are undefined")

    assert again.finding.finding_id == first.finding.finding_id and not again.created
    assert other_author.created and other_kind.created
    assert (
        len(
            {
                first.finding.finding_id,
                other_author.finding.finding_id,
                other_kind.finding.finding_id,
            }
        )
        == 3
    )

    await findings.resolve(PROJECT, first.finding.finding_id, by="lead")
    reopened = await add(findings, about=[CARD, FEATURE], text="Refunds are undefined")
    assert reopened.created and reopened.finding.finding_id != first.finding.finding_id


async def test_the_repeat_writes_nothing(findings: Findings, memory: MemoryGapExecutor) -> None:
    await add(findings)
    memory.calls.clear()

    again = await add(findings)

    assert not again.created
    assert not [name for name, _ in memory.calls if name.startswith(("upsert", "gap_set", "gap_delete"))]


async def test_match_finds_the_open_finding_of_a_tuple_and_none_for_another(
    findings: Findings,
) -> None:
    added = await add(findings, kind="question", about=[FEATURE, CARD])

    found = await findings.match(
        PROJECT,
        created_by="u1",
        kind="question",
        about=[CARD, FEATURE],
        text="the SPEC is  silent.",
    )
    assert found is not None and found.finding_id == added.finding.finding_id
    assert (
        await findings.match(
            PROJECT,
            created_by="u1",
            kind="gap",
            about=[CARD, FEATURE],
            text="The spec is silent.",
        )
        is None
    )
    with pytest.raises(FindingError) as unknown:
        await findings.match(PROJECT, created_by="u1", kind="question", about=[CARD, "REQ::GONE"], text="x")
    assert (unknown.value.code, unknown.value.spec_ids) == ("node_not_found", ("REQ::GONE",))


async def test_the_limit_of_open_findings_counts_per_author_and_story(
    findings: Findings, memory: MemoryGapExecutor
) -> None:
    # Scenario: 50 open findings of u1 in story S; the 51st in S is refused, one in another story, one without a story,
    # and one of u2 in S are accepted; closing one makes room.
    for number in range(50):
        await add(findings, text=f"finding {number}", story_id="STORY::A::S")
    writes = memory.called("upsert_node_Finding")

    with pytest.raises(FindingError) as caught:
        await add(findings, text="one more", story_id="STORY::A::S")
    assert caught.value.code == "finding_limit"
    assert memory.called("upsert_node_Finding") == writes

    await add(findings, text="other story", story_id="STORY::A::T")
    await add(findings, text="no story")
    await add(findings, text="other author", created_by="u2", story_id="STORY::A::S")
    first = next(iter(memory.findings()))
    await findings.dismiss(PROJECT, first, by="lead")
    assert (await add(findings, text="room again", story_id="STORY::A::S")).created


@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"kind": "idea"}, "invalid_kind"),
        ({"kind": "note", "blocking": True, "story_id": "S"}, "invalid_kind"),
        ({"text": ""}, "invalid_text"),
        ({"text": "   "}, "invalid_text"),
        ({"text": "x" * 2001}, "invalid_text"),
        ({"about": []}, "invalid_about"),
        ({"about": ["A"] * 0}, "invalid_about"),
        ({"about": [f"REQ::{n}" for n in range(21)]}, "invalid_about"),
        ({"about": "REQ::FR::CARD_PAYMENT"}, "invalid_about"),
        ({"about": [""]}, "invalid_about"),
        ({"created_by": ""}, "invalid_author"),
        ({"created_by": "a" * 201}, "invalid_author"),
        ({"created_by": "tab\there"}, "invalid_author"),
        ({"story_id": ""}, "invalid_story"),
        ({"blocking": True}, "invalid_story"),
        ({"refers_to": [("file", f"src/f{n}.py") for n in range(11)]}, "invalid_ref"),
        ({"refers_to": [("issue", "12")]}, "invalid_ref"),
        ({"refers_to": ["pr"]}, "invalid_ref"),
    ],
)
async def test_arguments_are_checked_before_any_query(
    findings: Findings, memory: MemoryGapExecutor, values: dict[str, Any], code: str
) -> None:
    with pytest.raises(ConfigurationError) as caught:
        await add(findings, **values)

    assert caught.value.code == code
    assert memory.calls == []


async def test_a_finding_about_20_ids_and_with_10_references_is_accepted(
    findings: Findings, memory: MemoryGapExecutor
) -> None:
    ids = [f"REQ::FR::N{n}" for n in range(20)]
    for spec_id in ids:
        memory.spec(spec_id)

    added = await add(findings, about=ids, refers_to=[("file", f"src/f{n}.py") for n in range(10)])

    assert len(added.finding.about_spec_ids) == 20 and len(added.finding.refers_to) == 10


async def test_a_failed_write_leaves_nothing_behind(findings: Findings, memory: MemoryGapExecutor) -> None:
    memory.fail_on = "upsert_relationship_ABOUT"

    with pytest.raises(GraphWriteError):
        await add(findings, refers_to=[("commit", "abcdef1")])

    assert not memory.nodes and not memory.edges


async def test_resolve_and_dismiss_close_an_open_finding_once(
    findings: Findings,
) -> None:
    # Scenario: resolve with a note; the second close of either kind is wrong_state and changes nothing; dismiss
    # another.
    first = (await add(findings)).finding
    second = (await add(findings, text="second")).finding

    closed = await findings.resolve(PROJECT, first.finding_id, by="lead", note="Decided: refunds follow card rules.")
    assert (closed.status, closed.resolved_by, closed.resolution_note) == (
        "resolved",
        "lead",
        "Decided: refunds follow card rules.",
    )
    assert closed.resolved_at is not None
    for close in (findings.resolve, findings.dismiss):
        with pytest.raises(FindingError) as caught:
            await close(PROJECT, first.finding_id, by="someone")
        assert caught.value.code == "wrong_state"
    assert (await findings.finding(PROJECT, first.finding_id)) == closed

    dismissed = await findings.dismiss(PROJECT, second.finding_id, by="lead")
    assert (dismissed.status, dismissed.resolution_note) == ("dismissed", None)
    with pytest.raises(FindingError) as unknown:
        await findings.resolve(PROJECT, "FND::000000000000", by="lead")
    assert unknown.value.code == "unknown_finding"


@pytest.mark.parametrize(
    ("by", "note", "code"),
    [("", None, "invalid_author"), ("lead", "n" * 501, "invalid_note")],
)
async def test_close_arguments_are_checked(findings: Findings, by: str, note: str | None, code: str) -> None:
    first = (await add(findings)).finding

    with pytest.raises(ConfigurationError) as caught:
        await findings.resolve(PROJECT, first.finding_id, by=by, note=note)

    assert caught.value.code == code


async def test_an_empty_note_is_no_note(findings: Findings) -> None:
    first = (await add(findings)).finding

    assert (await findings.resolve(PROJECT, first.finding_id, by="lead", note="")).resolution_note is None


async def test_a_close_that_loses_the_race_is_wrong_state(
    findings: Findings, memory: MemoryGapExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Scenario: the finding is read open, another caller closes it, then the conditional update writes nothing.
    first = (await add(findings)).finding
    real = memory._q_gap_set_open_finding

    def closed_meanwhile(params: Any) -> Any:
        next(iter(memory.nodes.values()))["status"] = "resolved"
        return real(params)

    monkeypatch.setattr(memory, "_q_gap_set_open_finding", closed_meanwhile)

    with pytest.raises(FindingError) as caught:
        await findings.dismiss(PROJECT, first.finding_id, by="lead")
    assert caught.value.code == "wrong_state"


async def test_withdraw_deletes_an_open_finding_and_only_the_references_nobody_else_cites(
    findings: Findings, memory: MemoryGapExecutor
) -> None:
    keep = (await add(findings, text="keep", refers_to=[("commit", "abcdef1")])).finding
    drop = (
        await add(
            findings,
            text="drop",
            refers_to=[("commit", "abcdef1"), ("url", "https://example.com/a")],
        )
    ).finding

    assert await findings.withdraw(PROJECT, drop.finding_id) is True

    assert set(memory.findings()) == {keep.finding_id}
    assert [(ref["type"], ref["value"]) for ref in memory.refs()] == [("commit", "abcdef1")]
    assert memory.edge_keys("ABOUT") == {f"{keep.finding_id}|ABOUT|{CARD}"}
    assert await findings.withdraw(PROJECT, drop.finding_id) is False
    await findings.resolve(PROJECT, keep.finding_id, by="lead")
    assert await findings.withdraw(PROJECT, keep.finding_id) is False  # closed findings are history


async def test_findings_list_blocking_first_then_newest_first_and_filter(
    findings: Findings,
) -> None:
    # Scenario: three findings in time order a, b (blocking), c; a is about FEATURE, the others about CARD.
    a = (await add(findings, text="a", about=[FEATURE])).finding
    b = (await add(findings, text="b", kind="question", story_id="STORY::A::S", blocking=True)).finding
    c = (await add(findings, text="c", created_by="u2", kind="note")).finding

    page = await findings.findings(PROJECT)
    assert [item.finding_id for item in page.findings] == [
        b.finding_id,
        c.finding_id,
        a.finding_id,
    ] and page.next is None
    assert [i.finding_id for i in (await findings.findings(PROJECT, spec_ids=[FEATURE])).findings] == [a.finding_id]
    assert [i.finding_id for i in (await findings.findings(PROJECT, story_id="STORY::A::S")).findings] == [b.finding_id]
    assert [i.finding_id for i in (await findings.findings(PROJECT, created_by="u2")).findings] == [c.finding_id]
    assert [i.finding_id for i in (await findings.findings(PROJECT, blocking=False)).findings] == [
        c.finding_id,
        a.finding_id,
    ]

    await findings.resolve(PROJECT, a.finding_id, by="lead")
    assert [i.finding_id for i in (await findings.findings(PROJECT, status="resolved")).findings] == [a.finding_id]
    assert len((await findings.findings(PROJECT, status="all")).findings) == 3


async def test_findings_page_with_an_opaque_cursor_that_survives_a_close(
    findings: Findings,
) -> None:
    ids = [(await add(findings, text=f"t{n}")).finding.finding_id for n in range(5)]  # oldest first
    newest_first = ids[::-1]

    first = await findings.findings(PROJECT, limit=2)
    assert [i.finding_id for i in first.findings] == newest_first[:2] and first.next
    await findings.resolve(PROJECT, newest_first[1], by="lead")  # the last item of page one is closed meanwhile
    second = await findings.findings(PROJECT, limit=2, after=first.next)
    assert [i.finding_id for i in second.findings] == newest_first[2:4] and second.next
    third = await findings.findings(PROJECT, limit=2, after=second.next)
    assert [i.finding_id for i in third.findings] == newest_first[4:] and third.next is None


@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"limit": 0}, "invalid_limit"),
        ({"limit": 201}, "invalid_limit"),
        ({"limit": True}, "invalid_limit"),
        ({"after": "not a cursor"}, "invalid_limit"),
        ({"after": "e30"}, "invalid_limit"),
        ({"status": "closed"}, "invalid_status"),
        ({"spec_ids": "REQ::FR::CARD_PAYMENT"}, "invalid_about"),
    ],
)
async def test_list_arguments_are_checked(findings: Findings, values: dict[str, Any], code: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        await findings.findings(PROJECT, **values)
    assert caught.value.code == code


async def test_open_counts_count_open_findings_per_asked_id_through_aliases(
    findings: Findings, memory: MemoryGapExecutor
) -> None:
    # Scenario: two open findings and a resolved one about CARD, one about FEATURE; REFUND was merged into CARD.
    await add(findings, text="one")
    await add(findings, text="two", about=[CARD, FEATURE])
    closed = (await add(findings, text="three")).finding
    await findings.resolve(PROJECT, closed.finding_id, by="lead")
    await add(findings, text="four", about=[FEATURE])
    memory.merge_spec(REFUND, CARD)

    counts = await findings.open_counts(PROJECT, [CARD, REFUND, FEATURE, MODULE, "REQ::GONE"])

    assert counts == {CARD: 2, REFUND: 2, FEATURE: 2, MODULE: 0, "REQ::GONE": 0}
    assert await findings.open_counts(PROJECT, []) == {}


async def test_refresh_resolves_aliases_orphans_a_lost_node_and_restores_it(
    findings: Findings, memory: MemoryGapExecutor
) -> None:
    # Scenario: findings about REFUND (merged into CARD by a build), about FEATURE (deleted by a build) and untouched
    # MODULE; refresh renames, orphans and keeps them; a later build restores FEATURE and the next refresh clears it.
    merged = (await add(findings, text="m", about=[REFUND])).finding
    lost = (await add(findings, text="l", about=[FEATURE, MODULE])).finding
    fine = (await add(findings, text="f", about=[MODULE])).finding
    memory.merge_spec(REFUND, CARD)
    memory.delete_spec(FEATURE)

    result = await findings.refresh(PROJECT)

    assert (result.refreshed, result.orphaned) == (3, 1)
    after = {item.finding_id: item for item in (await findings.findings(PROJECT)).findings}
    assert after[merged.finding_id].about_spec_ids == (CARD,) and not after[merged.finding_id].orphaned
    assert after[lost.finding_id].about_spec_ids == (FEATURE, MODULE) and after[lost.finding_id].orphaned
    assert not after[fine.finding_id].orphaned
    assert memory.edge_keys("ABOUT") == {
        f"{merged.finding_id}|ABOUT|{CARD}",
        f"{lost.finding_id}|ABOUT|{MODULE}",
        f"{fine.finding_id}|ABOUT|{MODULE}",
    }
    repeat = await add(findings, text="m", about=[CARD])  # the dedupe key follows the new ids
    assert not repeat.created and repeat.finding.finding_id == merged.finding_id

    memory.spec(FEATURE, "Feature")
    again = await findings.refresh(PROJECT)

    assert (again.refreshed, again.orphaned) == (3, 0)
    current = await findings.finding(PROJECT, lost.finding_id)
    assert current is not None and not current.orphaned
    assert f"{lost.finding_id}|ABOUT|{FEATURE}" in memory.edge_keys("ABOUT")


async def test_refresh_leaves_closed_findings_alone(findings: Findings, memory: MemoryGapExecutor) -> None:
    closed = (await add(findings)).finding
    await findings.resolve(PROJECT, closed.finding_id, by="lead")
    memory.delete_spec(CARD)

    result = await findings.refresh(PROJECT)

    assert (result.refreshed, result.orphaned) == (0, 0)
    current = await findings.finding(PROJECT, closed.finding_id)
    assert current is not None and not current.orphaned


async def test_reference_models_are_accepted_as_entries(findings: Findings) -> None:
    added = await add(
        findings,
        refers_to=[FindingRef(type="file", value="src/a.py"), ("file", "src/a.py")],
    )

    assert [(r.type, r.value) for r in added.finding.refers_to] == [("file", "src/a.py")]


async def test_an_id_that_clashes_every_time_is_the_finding_limit(
    findings: Findings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Scenario: the id generator repeats the id of an existing finding; five attempts later add refuses.
    await add(findings)
    existing = next(iter((await findings.findings(PROJECT)).findings)).finding_id
    monkeypatch.setattr("graph.gap.findings.new_finding_id", lambda: existing)

    with pytest.raises(FindingError) as caught:
        await add(findings, text="another finding")

    assert caught.value.code == "finding_limit"
