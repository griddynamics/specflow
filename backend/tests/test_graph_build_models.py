"""build models: the additive fields default so stored artifacts load, and round-trip."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args

from graph.build.models import (
    JUDGED_ITEM_OUTCOMES,
    BacklogQueue,
    BuildEstimate,
    BuildResult,
    ChangedEdge,
    ChangedNode,
    ChangeReport,
    DiffArtifact,
    EstimateStep,
    InferenceReport,
    JudgedItem,
    ProjectBuildStatus,
    RuleReport,
    Settlement,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
RESULT = {
    "project_id": "p",
    "build_id": "b1",
    "mode": "first",
    "status": "succeeded",
    "started_at": T0.isoformat(),
    "finished_at": T0.isoformat(),
}


def test_older_payloads_load_with_the_new_fields_n_a() -> None:
    report = InferenceReport.model_validate({"rules": [{"rule": "INF-CONT-01", "subjects": 3, "deferred": 1}]})
    (rule,) = report.rules
    assert (report.backlog, report.spine_unreachable_after) == (None, None)
    assert (rule.limit, rule.budget_tokens, rule.backlog, rule.read_edges) == (None, None, None, None)
    result = BuildResult.model_validate(RESULT)
    assert (result.settlement, result.change_report) == (None, None)
    assert DiffArtifact.model_validate({"operations": []}).edge_changes is None


def test_the_new_fields_round_trip() -> None:
    report = InferenceReport(
        rules=(RuleReport(rule="INF-CONT-01", limit="first_cap", budget_tokens=4000, backlog=2, read_edges=0),),
        judged_items=(
            JudgedItem(rule="INF-CONT-01", kind="owner", subject="FR::1", relation="PART_OF", outcome="declined"),
        ),
        backlog={"INF-CONT-01": BacklogQueue(spec_ids=("FR::2", "FR::3"), since_build="b0", age_builds=2)},
        spine_unreachable_after=5,
    )
    assert InferenceReport.model_validate_json(report.model_dump_json()) == report
    edge = ChangedEdge(ref="FR::1|PART_OF|FEAT::1", change="added", basis="inferred", rule="INF-CONT-01")
    change = ChangeReport(
        nodes=(ChangedNode(spec_id="FR::1", label="FunctionalRequirement", classes=("added",)),),
        edges=(edge,),
        counts={"added": 1},
        edge_counts={"added": 1},
    )
    settlement = Settlement(
        state="not_settled",
        reasons=("inference_backlog",),
        inference_backlog={"INF-CONT-01": 2},
        follow_up_builds=("b2",),
    )
    result = BuildResult.model_validate(RESULT).model_copy(update={"settlement": settlement, "change_report": change})
    assert BuildResult.model_validate_json(result.model_dump_json()) == result
    diff = DiffArtifact(edge_changes=(edge.model_copy(update={"basis": "stated", "rule": None}),))
    assert DiffArtifact.model_validate_json(diff.model_dump_json()) == diff
    status = ProjectBuildStatus(project_id="p", building=True, build_id="b2", step="infer", follow_up_attempt=1)
    assert ProjectBuildStatus.model_validate_json(status.model_dump_json()) == status


def test_the_estimate_has_the_inference_steps_and_items_have_a_declined_outcome() -> None:
    assert get_args(EstimateStep)[-3:] == ("inference", "cross_document_inference", "embeddings")
    estimate = BuildEstimate.model_validate({"project_id": "p", "mode": "first", "change_set": {}})
    assert estimate.inference_backlog is None
    queued = estimate.model_copy(update={"inference_backlog": {"INF-CONT-01": 1295}})
    assert BuildEstimate.model_validate_json(queued.model_dump_json()) == queued
    assert "declined" in JUDGED_ITEM_OUTCOMES
    assert Settlement().state == "settled"
