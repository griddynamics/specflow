"""Models, settings, errors and ports of ``graph.gap`` (docs/graph/gap/api.md "Models", "Errors", "Ports";
architecture.md "Settings"; FR-GOWN-02, FR-GOWN-04)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast, get_args

import pytest
from pydantic import ValidationError

import graph.gap
from gap_port_fakes import (
    MemoryAmendments,
    MemoryBuildRequests,
    MemoryGapRecords,
    MemoryPlanView,
    MemoryStoryHolds,
    ScriptedInvestigator,
)
from graph.core import load_ontology
from graph.core.errors import ConfigurationError, GraphError
from graph.gap import (
    AmendmentRow,
    Amendments,
    AnswerInput,
    AnswerInvalidError,
    BuildRequests,
    EventRow,
    GapChangeReport,
    GapContext,
    GapLimits,
    GapNotFoundError,
    GapRecords,
    GapReport,
    GapRunResult,
    GapSettings,
    GapStateError,
    GapView,
    InvestigationResult,
    Investigator,
    NewGap,
    NoteRow,
    OptionRow,
    PlanView,
    RelevanceRow,
    RunRow,
    SectionKey,
    SiteBudget,
    SitePack,
    SiteReads,
    StaleGapError,
    StoryHolds,
    SuppressionRow,
    TaskRef,
    Witness,
    gap_settings_from_env,
    load_gap_rules,
)
from graph.gap.names import GAP_RELATIONS, VOCABULARY_LITERALS, gap_vocabularies

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
CLAIM = "claim-1"


def test_the_gap_literals_equal_the_vocabularies_of_the_shipped_ontology() -> None:
    vocabularies = gap_vocabularies()

    for name, literal in VOCABULARY_LITERALS.items():
        assert get_args(literal) == vocabularies[name], name
    ontology = load_ontology()
    assert set(GAP_RELATIONS) == {name for name in ontology.relations if ontology.owner_of(name) == "gap"} - {
        "REFERS_TO"
    }


def test_every_name_of_the_package_root_is_exported() -> None:
    for name in graph.gap.__all__:
        assert getattr(graph.gap, name) is not None, name
    for name in (
        "load_gap_rules",
        "ensure_gap_schema",
        "GapContext",
        "GapSettings",
        "GapChangeReport",
        "GapRunResult",
        "NoiseReport",
        "GapEstimate",
        "GapView",
        "AnswerInput",
        "NewGap",
        "ReconcileResult",
        "GapNotFoundError",
        "GapStateError",
        "AnswerInvalidError",
        "StaleGapError",
        "GapRecords",
        "Amendments",
        "Investigator",
        "StoryHolds",
        "BuildRequests",
        "PlanView",
    ):
        assert name in graph.gap.__all__, name


def test_models_are_frozen_and_forbid_unknown_fields() -> None:
    report = GapChangeReport(build_id="b1", added=("REQ::A",), rekeyed={"REQ::OLD": "REQ::NEW"})

    assert report.model_config.get("frozen") is True
    with pytest.raises(ValidationError):
        GapChangeReport.model_validate({"build_id": "b1", "carried_over": []})
    assert report.rekeyed["REQ::OLD"] == "REQ::NEW"


def test_an_answer_names_exactly_one_of_an_option_and_own_text() -> None:
    assert AnswerInput(gap_id="GAP::A", option_rank=1, answered_by="u1").own_text is None
    assert AnswerInput(gap_id="GAP::A", own_text="Never.", answered_by="u1").option_rank is None
    for fields in ({}, {"option_rank": 1, "own_text": "x"}):
        with pytest.raises(ValidationError, match="exactly one"):
            AnswerInput(gap_id="GAP::A", answered_by="u1", **fields)
    with pytest.raises(ValidationError):
        AnswerInput(gap_id="GAP::A", option_rank=0, answered_by="u1")


def test_a_blocking_new_gap_is_a_development_gap_with_a_claim() -> None:
    base = {
        "kind": "missing",
        "question": "Who owns refunds?",
        "about": ("REQ::A",),
        "created_by": "agent",
    }

    assert NewGap(**base, origin="person").blocking is False
    assert NewGap(
        **base,
        origin="development",
        blocking=True,
        story_id="STORY::A",
        claim_token=CLAIM,
    ).blocking
    for fields in (
        {
            "origin": "person",
            "blocking": True,
            "story_id": "STORY::A",
            "claim_token": CLAIM,
        },
        {"origin": "development", "blocking": True, "story_id": "STORY::A"},
    ):
        with pytest.raises(ValidationError, match="blocking gap"):
            NewGap(**base, **fields)


def test_a_witness_is_a_quote_or_a_fact() -> None:
    assert Witness(fragment_id="FRG::a", quote="x", line_start=3, line_end=3).fact is None
    assert Witness(fact="no provider").quote is None
    for fields in (
        {},
        {"fragment_id": "FRG::a"},
        {"fragment_id": "FRG::a", "quote": "x", "fact": "y"},
    ):
        with pytest.raises(ValidationError, match="fragment quote or a fact"):
            Witness(**fields)


def test_the_report_model_parses_the_spec_example() -> None:
    report = GapReport.model_validate(
        {
            "verdict": "gap",
            "kind": "open_question",
            "severity": "major",
            "title": "Cancelling a paid order is not specified",
            "question": "Can a customer cancel an order after paying, and what happens to the payment?",
            "why": "Payment is captured when an order is placed.",
            "about": ["QST::CANCEL_PAID_ORDER", "REQ::FR::PLACE_ORDER"],
            "impacts": ["FEAT::PLACE_ORDER"],
            "witness": [
                {
                    "fragment_id": "FRG::1",
                    "quote": "Can a customer cancel a paid order?",
                }
            ],
            "options": [
                {
                    "label": "Allow within 30 minutes, full refund",
                    "recommended": True,
                    "detail": "Uses the refund interface.",
                    "edits": [
                        {
                            "placement": "amend",
                            "target": {
                                "fragment_id": "FRG::1",
                                "anchor_quote": "Can a customer cancel a paid order?",
                            },
                            "operation": "replace",
                            "text": "A customer can cancel a paid order within 30 minutes of payment.",
                        }
                    ],
                    "basis": [
                        {
                            "fragment_id": "FRG::2",
                            "quote": "Refunds are issued through the Payments refund API.",
                        }
                    ],
                },
                {
                    "label": "Not allowed after payment",
                    "recommended": False,
                    "edits": [
                        {
                            "placement": "document",
                            "target": {"heading": "Orders: cancellation"},
                            "operation": "append_section",
                            "text": "A paid order cannot be cancelled.",
                        }
                    ],
                },
            ],
            "ai_answerable": False,
            "ai_reason": "Changes customer-visible behaviour.",
        }
    )

    assert report.options[0].edits[0].target.anchor_quote == "Can a customer cancel a paid order?"
    with pytest.raises(ValidationError):
        GapReport.model_validate({"verdict": "gap", "confidence": 1})


def test_rows_mirror_the_relational_tables() -> None:
    row = RelevanceRow(
        lineage="orders",
        heading_path=("Orders", "Place order"),
        text_hash="h",
        relevance="spec",
        by="rules",
    )
    amendment = AmendmentRow(
        amendment_id="AMD::1",
        clr_id="CLR::1",
        lineage="orders",
        heading_path=("Orders",),
        anchor_text="Can a customer cancel a paid order?",
        anchor_hash="h",
        operation="replace",
        text="Within 30 minutes.",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )

    assert row.key == SectionKey(lineage="orders", heading_path=("Orders", "Place order"), text_hash="h")
    assert hash(row.key) == hash(SectionKey(lineage="orders", heading_path=("Orders", "Place order"), text_hash="h"))
    assert amendment.last_revision_id is None
    with pytest.raises(ValidationError):
        AmendmentRow.model_validate({**amendment.model_dump(), "status": "gone"})
    with pytest.raises(ValidationError):
        RunRow(
            run_id="r",
            mode="update",
            status="running",
            started_at=datetime.fromisoformat("2026-01-01T00:00:00"),
            rules_version="1",
        )


def test_a_run_result_may_be_degraded_and_a_view_carries_no_history_by_default() -> None:
    result = GapRunResult(run_id="r", mode="update", status="degraded")
    view = GapView(
        gap_id="GAP::A",
        kind="missing",
        severity="major",
        status="open",
        origin="analysis",
        route="ask",
        title="No provider",
        question="Who provides it?",
        detector="MS-PROVIDER",
        signature="s",
        created_by="gap",
        created_at=NOW,
        updated_at=NOW,
    )

    assert result.noise.sections == ()
    assert (view.history, view.reach, view.clarification) == (None, None, None)


def test_settings_default_and_map_the_environment(tmp_path: Path) -> None:
    assert GapSettings() == gap_settings_from_env({})
    settings = gap_settings_from_env(
        {
            "GAP_ENABLED": "false",
            "GAP_AUTO_RUN_AFTER_BUILD": "0",
            "GAP_INVESTIGATOR": "agents",
            "GAP_AI_ANSWERS": "no",
            "GAP_RULES_PATH": str(tmp_path / "rules.yaml"),
            "GAP_APPLY_QUIET_SECONDS": "30",
            "OTHER": "x",
        }
    )

    assert settings == GapSettings(
        enabled=False,
        auto_run_after_build=False,
        ai_answers=False,
        investigator="agents",
        rules_path=tmp_path / "rules.yaml",
    )
    assert gap_settings_from_env({"GAP_ENABLED": " ", "GAP_RULES_PATH": ""}) == GapSettings()
    assert gap_settings_from_env({"GAP_APPLY_QUIET_SECONDS": "soon"}) == GapSettings()


@pytest.mark.parametrize(
    ("environ", "variable"),
    [
        ({"GAP_ENABLED": "maybe"}, "GAP_ENABLED"),
        ({"GAP_INVESTIGATOR": "crew"}, "GAP_INVESTIGATOR"),
    ],
)
def test_invalid_settings_name_the_variable(environ: dict[str, str], variable: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        gap_settings_from_env(environ)

    assert (caught.value.code, caught.value.fields["variable"]) == (
        "invalid_gap_settings",
        variable,
    )


def test_limit_overrides_apply_over_the_rules_and_are_checked() -> None:
    base = load_gap_rules(ontology=load_ontology()).limits

    assert GapSettings().effective_limits(base) is base
    limits = GapSettings(limits={"sites_full_run": 10, "options_max": 3}).effective_limits(base)
    assert (
        limits.sites_full_run,
        limits.options_max,
        limits.sites_update_run,
    ) == (10, 3, base.sites_update_run)
    with pytest.raises(ValidationError, match="unknown limits: sites"):
        GapSettings(limits={"sites": 1})
    with pytest.raises(ValidationError, match="unknown limits: apply_quiet_seconds"):
        GapSettings(limits={"apply_quiet_seconds": 5})
    with pytest.raises(ConfigurationError, match="invalid_gap_settings"):
        GapSettings(limits={"options_max": 9}).effective_limits(base)
    with pytest.raises(ValidationError, match="options_min must not exceed"):
        GapLimits.model_validate({**base.model_dump(), "options_min": 4, "options_max": 3})


def test_errors_carry_stable_codes_and_no_text() -> None:
    errors = [
        (GapNotFoundError(gap_id="GAP::A"), "gap_not_found"),
        (
            GapStateError(status="resolved", allowed=("open", "answered"), gap_id="GAP::A"),
            "gap_wrong_state",
        ),
        (AnswerInvalidError(field="own_text", reason="empty"), "answer_invalid"),
        (StaleGapError(gap_id="GAP::A"), "gap_stale"),
    ]

    for error, code in errors:
        assert isinstance(error, GraphError)
        assert error.code == code
    state = errors[1][0]
    assert isinstance(state, GapStateError)
    assert (state.status, state.allowed, state.fields["allowed"]) == (
        "resolved",
        ("open", "answered"),
        "open,answered",
    )


def test_the_fakes_satisfy_the_ports() -> None:
    assert isinstance(MemoryGapRecords(), GapRecords)
    assert isinstance(MemoryAmendments(), Amendments)
    assert isinstance(ScriptedInvestigator(), Investigator)
    assert isinstance(MemoryStoryHolds(), StoryHolds)
    assert isinstance(MemoryBuildRequests(), BuildRequests)
    assert isinstance(MemoryPlanView(), PlanView)
    assert not isinstance(object(), SiteReads)


async def test_fr_gown_02_the_record_fakes_keep_projects_apart() -> None:
    records, amendments = MemoryGapRecords(), MemoryAmendments()
    option = OptionRow(gap_id="GAP::A", rank=1, label="Yes", recommended=True)
    row = AmendmentRow(
        amendment_id="AMD::1",
        clr_id="CLR::1",
        lineage="orders",
        heading_path=("Orders",),
        anchor_text="a",
        anchor_hash="h",
        operation="insert_after",
        text="t",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )

    await records.put_options("a", "GAP::A", [option])
    await records.suppress(
        "a",
        SuppressionRow(signature="s", question="q", reason="r", dismissed_by="u", dismissed_at=NOW),
    )
    await records.log("a", [EventRow(gap_id="GAP::A", at=NOW, actor="u", event="opened")])
    await records.put_notes("a", "r1", [NoteRow(detector="OQ-STATED", spec_ids=("QST::A",), text="n")])
    await amendments.put("a", [row])
    await amendments.set_status("a", ["AMD::1"], "active", revision_id="rev-2")

    assert await records.options("a", "GAP::A") == (option,)
    assert await records.options("b", "GAP::A") == ()
    assert await records.suppressed("a", ["s", "t"]) == frozenset({"s"})
    assert await records.suppressed("b", ["s"]) == frozenset()
    assert records.notes["a"][0].run_id == "r1"
    assert await amendments.pending_lineages("a") == ("orders",)
    assert await amendments.pending_lineages("b") == ()
    assert (await amendments.for_lineage("a", "orders"))[0].last_revision_id == "rev-2"
    assert await amendments.for_clarification("b", "CLR::1") == ()


async def test_the_plan_fakes_hold_and_release() -> None:
    holds = MemoryStoryHolds({"REQ::A": ("TASK::1", "TASK::2")}, in_progress=("TASK::2",))
    plan = MemoryPlanView({"REQ::A": (TaskRef(task_id="TASK::1", story_id="STORY::A", status="pending"),)})
    builds = MemoryBuildRequests()
    investigator = ScriptedInvestigator({"SITE::1": InvestigationResult(rounds_used=1)})
    pack = SitePack(project_id="a", site_id="SITE::1", input_hash="h", mode="update", seeds=())
    budget = SiteBudget(rounds=4, reads_per_round=4, context_tokens=24000)

    result = await holds.hold_tasks("a", "GAP::A", ["REQ::A"])
    assert (result.held, result.in_progress) == (("TASK::1",), ("TASK::2",))
    assert await holds.release_gap("a", "GAP::A") == ("TASK::1",)
    assert await plan.stories_for("a", ["REQ::A", "REQ::B"]) == {"REQ::A": ("STORY::A",)}
    assert await builds.request_update_build("a", "answers", immediate=True) == "build-1"
    assert (await investigator.investigate(pack, cast(SiteReads, object()), budget)).rounds_used == 1


def test_a_context_holds_the_ports_and_defaults() -> None:
    ontology = load_ontology()
    context = GapContext(
        project_id="a",
        executor=None,
        reader=None,
        gateway=None,
        ontology=ontology,
        rules=load_gap_rules(ontology=ontology),
        records=MemoryGapRecords(),
        amendments=MemoryAmendments(),
        investigator=ScriptedInvestigator(),
    )

    assert (context.changes, context.plan, context.run_id, context.settings) == (
        (),
        None,
        None,
        GapSettings(),
    )
