"""Offline tests of the pure parts of light inference."""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
import yaml
from pydantic import ValidationError

from graph.build import infer as inference
from graph.build import infer_reads, infer_xdoc
from graph.build.files import FileExtractionStore
from graph.build.infer import (
    VERDICT_TYPES,
    AttachOrphanVerdict,
    EndpointsVerdict,
    RealizationVerdict,
    _evaluate,
    _judgement,
    _memory_entry,
    _Node,
    _recalled,
    _Subject,
)
from graph.build.infer_xdoc import CrossDocumentVerdict
from graph.build.models import BacklogQueue, RuleReport
from graph.build.spine import SpineEdge, SpineGraph, SpineNode
from graph.build.validator import InMemoryGraphView, ProposalChecker, ViewEdge, ViewNode
from graph.core.db import Executor
from graph.core.errors import ConformanceError, LLMError, OntologyError
from graph.core.llm import JudgeRunner, Verdict
from graph.core.llm.judge import EVIDENCE_RULE
from graph.core.ontology import InferenceRule, InferenceSpec, SpinePath, load_ontology
from graph.core.ontology.model import CROSS_DOCUMENT, TEMPLATE_PARAMETERS
from graph.stores._bolt.cypher import render_cypher
from graph_support import (
    FROZEN_ONTOLOGY_FILE,
    ScriptedGateway,
    copy_frozen_prompts,
    frozen_ontology,
    outside_untrusted,
    shipped_ontology,
    untrusted_blocks,
)

# Mechanics tests: pinned to the frozen v1.3 ontology (graph_support.FROZEN_ONTOLOGY_FILE), not the shipped one.
ONTOLOGY = frozen_ontology()
RULES: dict[str, InferenceRule] = {rule.template: rule for rule in ONTOLOGY.inference.rules}
INT, CAP, ORPH = RULES["endpoints"], RULES["realization"], RULES["attach_orphan"]
MODEL = "judge-test"
LOCAL_ID = re.compile(r"^[TMC][1-9][0-9]*$")


def node(uuid: str, label: str, name: str, *fragments: tuple[str, str], statements: tuple[str, ...] = ()) -> _Node:
    return _Node(
        uuid=uuid,
        spec_id=f"SPEC::{uuid.upper()}",
        name=name,
        label=label,
        identity_key=f"key|{uuid}",
        statements=statements or (f"{name} statement.",),
        fragments=fragments or ((f"frag-{uuid}", f"hash-{uuid}"),),
    )


# Endpoints: int-1 with targets mod-a (T1), mod-b (T2), mod-c (T3) by spec_id; mod-b/mod-c linked.
NODES: dict[str, _Node] = {
    n.uuid: n
    for n in (
        node("int-1", "IntegrationRequirement", "Checkout hands orders over", ("frag-int", "hash-int")),
        node("mod-a", "Module", "Checkout service"),
        node("mod-b", "Module", "Orders service"),
        node("mod-c", "Module", "Stripe"),
        node("cap-1", "Capability", "Authentication"),
        node("feat-1", "Feature", "Staff login"),
        node("req-1", "FunctionalRequirement", "Customer downloads orders", ("frag-req", "hash-req")),
        node("feat-2", "Feature", "Order export"),
    )
}
TEXTS = {
    "frag-int": "The Checkout service hands each paid order to the Orders service and charges cards through Stripe.",
    "frag-req": "Customer can download each order as a CSV file.",
    **{f"frag-{uuid}": f"Text about {n.name}." for uuid, n in NODES.items() if uuid not in ("int-1", "req-1")},
}
ENDPOINT_SUBJECT = _Subject(
    "int-1",
    "SPEC::INT-1",
    candidates=("mod-a", "mod-b", "mod-c"),
    pairs=(("mod-a", "mod-b"), ("mod-a", "mod-c")),
    targets=("mod-a", "mod-b", "mod-c"),
)
REALIZATION_SUBJECT = _Subject(
    "cap-1", "SPEC::CAP-1", candidates=("mod-b",), members=("feat-1",), via={"mod-b": ("feat-1",)}
)
ORPHAN_SUBJECT = _Subject("req-1", "SPEC::REQ-1", candidates=("feat-2", "mod-a"))
SUBJECTS = {INT.id: (INT, ENDPOINT_SUBJECT), CAP.id: (CAP, REALIZATION_SUBJECT), ORPH.id: (ORPH, ORPHAN_SUBJECT)}

QUOTE_INT = [{"fragment_id": "frag-int", "quote": "hands each paid order to the Orders service"}]
QUOTE_REQ = [{"fragment_id": "frag-req", "quote": "download each order as a CSV file"}]


def judgement(rule: InferenceRule, subject: _Subject, nodes: dict[str, _Node] = NODES, model: str = MODEL) -> Any:
    found = _judgement(rule, subject, nodes, model)
    assert found is not None
    return found


def evaluate(rule: InferenceRule, subject: _Subject, verdict: Verdict) -> tuple[str, list[tuple[str, str, str]]]:
    shown = judgement(rule, subject)
    texts = {fragment_id: TEXTS[fragment_id] for fragment_id in shown.fragments}
    evaluation = _evaluate(ONTOLOGY, rule, subject, shown, verdict, nodes=NODES, texts=texts)
    return evaluation.outcome, [(p.source, p.target, p.edge_key) for p in evaluation.proposals]


def endpoints(*pairs: tuple[str, str], confidence: float = 0.9, evidence: Any = None) -> EndpointsVerdict:
    return EndpointsVerdict.model_validate(
        {
            "confidence": confidence,
            "evidence": QUOTE_INT if evidence is None else evidence,
            "links": [{"source": source, "target": target} for source, target in pairs],
        }
    )


# Verdict models


def test_every_template_has_a_verdict_model_on_the_judge_base() -> None:
    assert set(VERDICT_TYPES) == set(TEMPLATE_PARAMETERS)
    assert all(issubclass(model, Verdict) for model in VERDICT_TYPES.values())
    assert {
        "endpoints": EndpointsVerdict,
        "realization": RealizationVerdict,
        "attach_orphan": AttachOrphanVerdict,
        CROSS_DOCUMENT: CrossDocumentVerdict,
    } == VERDICT_TYPES


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (EndpointsVerdict, "links", [{"source": "T1", "target": "T2"}]),
        (RealizationVerdict, "targets", ["C1"]),
        (AttachOrphanVerdict, "target", "C1"),
    ],
    ids=["endpoints", "realization", "attach_orphan"],
)
def test_verdict_models_require_their_field_and_bound_confidence(model: type[Verdict], field: str, value: Any) -> None:
    good = model.model_validate({"confidence": 0.8, "evidence": [], field: value})
    assert good.model_dump()[field] == value
    with pytest.raises(ValidationError):
        model.model_validate({"confidence": 0.8, "evidence": []})  # the template field is required
    for confidence in (-0.1, 1.1):
        with pytest.raises(ValidationError):
            model.model_validate({"confidence": confidence, "evidence": [], field: value})
    schema = model.model_json_schema()
    assert {"confidence", "evidence", field} <= set(schema["required"])
    assert schema["properties"][field].get("description")


def test_an_orphan_verdict_may_name_no_target_and_links_need_both_ends() -> None:
    assert AttachOrphanVerdict.model_validate({"confidence": 0.9, "evidence": [], "target": None}).target is None
    with pytest.raises(ValidationError):
        EndpointsVerdict.model_validate({"confidence": 0.9, "evidence": [], "links": [{"source": "T1"}]})
    link_schema = EndpointsVerdict.model_json_schema()["$defs"]["EndpointLink"]
    assert set(link_schema["required"]) == {"source", "target"}


# Acceptance


def test_a_valid_link_at_or_above_the_floor_is_accepted_in_the_judged_direction() -> None:
    assert ONTOLOGY.inference.floor == 0.7
    assert evaluate(INT, ENDPOINT_SUBJECT, endpoints(("T1", "T2"), confidence=0.7)) == (
        "accepted",
        [("mod-a", "mod-b", "key|mod-a|INTEGRATES_WITH|key|mod-b")],
    )
    assert evaluate(INT, ENDPOINT_SUBJECT, endpoints(("T2", "T1"))) == (
        "accepted",
        [("mod-b", "mod-a", "key|mod-b|INTEGRATES_WITH|key|mod-a")],
    )


@pytest.mark.parametrize(
    ("verdict", "outcome"),
    [
        (endpoints(("T1", "T2"), confidence=0.69), "rejected"),
        (
            endpoints(("T1", "T2"), evidence=[{"fragment_id": "frag-int", "quote": "not in the fragment at all"}]),
            "rejected",
        ),
        (
            endpoints(("T1", "T2"), evidence=[{"fragment_id": "frag-other", "quote": "hands each paid order"}]),
            "rejected",
        ),
        (endpoints(("T1", "T2"), evidence=[]), "rejected"),
        (endpoints(("T1", "T2"), evidence=[{"fragment_id": "frag-int", "quote": "order"}]), "rejected"),  # too short
        (endpoints(), "declined"),
        (endpoints(confidence=0.1, evidence=[]), "declined"),  # a "no link" needs no floor or quote
        (endpoints(("T1", "T9")), "invalid"),
        (endpoints(("T1", "T1")), "invalid"),
        (endpoints(("T2", "T3")), "invalid"),  # not a candidate pair (already linked)
        (endpoints(("T1", "T2"), ("T2", "T1")), "invalid"),  # both ways, nothing else
        (endpoints(("T1", "T2"), ("T1", "M1")), "invalid"),  # one unknown id spoils the verdict
    ],
    ids=[
        "below-floor",
        "quote-not-found",
        "fragment-not-shown",
        "no-evidence",
        "quote-too-short",
        "no-link",
        "no-link-unsure",
        "unknown-id",
        "self-link",
        "not-a-candidate-pair",
        "both-ways-only",
        "one-unknown-id",
    ],
)
def test_endpoints_outcomes(verdict: EndpointsVerdict, outcome: str) -> None:
    found, proposals = evaluate(INT, ENDPOINT_SUBJECT, verdict)
    assert found == outcome
    assert proposals == []


def test_a_both_ways_pair_is_dropped_and_the_other_links_are_judged_without_it() -> None:
    verdict = endpoints(("T1", "T2"), ("T2", "T1"), ("T1", "T3"), ("T1", "T3"))
    assert evaluate(INT, ENDPOINT_SUBJECT, verdict) == (
        "accepted",
        [("mod-a", "mod-c", "key|mod-a|INTEGRATES_WITH|key|mod-c")],
    )


@pytest.mark.parametrize(
    ("targets", "outcome", "edges"),
    [
        (["C1"], "accepted", [("cap-1", "mod-b", "key|cap-1|REALIZED_BY|key|mod-b")]),
        (["C1", "C1"], "accepted", [("cap-1", "mod-b", "key|cap-1|REALIZED_BY|key|mod-b")]),
        ([], "declined", []),
        (["M1"], "invalid", []),  # a member's id is no candidate
        (["C2"], "invalid", []),
    ],
    ids=["one", "repeated", "none", "member-id", "unknown"],
)
def test_realization_outcomes(targets: list[str], outcome: str, edges: list[Any]) -> None:
    evidence = [{"fragment_id": "frag-cap-1", "quote": "Text about Authentication."}]
    verdict = RealizationVerdict.model_validate({"confidence": 0.9, "evidence": evidence, "targets": targets})
    assert evaluate(CAP, REALIZATION_SUBJECT, verdict) == (outcome, edges)


@pytest.mark.parametrize(
    ("target", "confidence", "outcome", "edges"),
    [
        ("C1", 0.9, "accepted", [("req-1", "feat-2", "key|req-1|PART_OF|key|feat-2")]),
        ("C2", 0.7, "accepted", [("req-1", "mod-a", "key|req-1|PART_OF|key|mod-a")]),
        ("C2", 0.6, "rejected", []),
        (None, 0.9, "declined", []),
        ("C3", 0.9, "invalid", []),
    ],
    ids=["first", "second-at-floor", "below-floor", "none", "unknown"],
)
def test_attach_orphan_outcomes(target: str | None, confidence: float, outcome: str, edges: list[Any]) -> None:
    verdict = AttachOrphanVerdict.model_validate({"confidence": confidence, "evidence": QUOTE_REQ, "target": target})
    assert evaluate(ORPH, ORPHAN_SUBJECT, verdict) == (outcome, edges)


def test_a_link_illegal_for_the_relation_is_invalid() -> None:
    # A capability candidate of an orphan: PART_OF requirement -> capability is not a legal pair.
    nodes = NODES
    subject = _Subject("req-1", "SPEC::REQ-1", candidates=("cap-1",))
    shown = judgement(ORPH, subject, nodes)
    verdict = AttachOrphanVerdict.model_validate({"confidence": 0.9, "evidence": QUOTE_REQ, "target": "C1"})
    texts = {fragment_id: TEXTS[fragment_id] for fragment_id in shown.fragments}
    evaluation = _evaluate(ONTOLOGY, ORPH, subject, shown, verdict, nodes=nodes, texts=texts)
    assert (evaluation.outcome, evaluation.proposals) == ("invalid", ())
    # The shared validator refused it (illegal_pair): the edge is what gets quarantined.
    assert [(p.source, p.target) for p in evaluation.refused] == [("req-1", "cap-1")]


# Payload


@pytest.mark.parametrize("rule_id", sorted(SUBJECTS))
def test_the_payload_shows_local_ids_only(rule_id: str) -> None:
    rule, subject = SUBJECTS[rule_id]
    shown = judgement(rule, subject)
    text = json.dumps(shown.payload)
    for n in NODES.values():
        assert n.uuid not in text and n.spec_id not in text and n.identity_key not in text
    assert shown.payload["relation"] == rule.writes
    sections = {"sections"} if rule.template == "attach_orphan" else set()  # attach_orphan shows where nodes sit
    assert set(shown.payload["subject"]) == {"name", "label", "statements", *sections}  # the subject has no id
    assert set(shown.ids.values()) <= set(NODES) and all(LOCAL_ID.match(local) for local in shown.ids)
    listed = [item for key in ("targets", "members", "candidates") for item in shown.payload.get(key, [])]
    assert listed and {item["id"] for item in listed} == set(shown.ids)
    assert "fragments" not in shown.payload and set(shown.fragments) <= set(TEXTS)


def test_the_payload_of_each_template_names_what_its_prompt_describes() -> None:
    endpoints_payload = judgement(INT, ENDPOINT_SUBJECT).payload
    assert set(endpoints_payload) == {"relation", "subject", "targets", "candidate_pairs"}
    assert [t["id"] for t in endpoints_payload["targets"]] == ["T1", "T2", "T3"]
    assert endpoints_payload["candidate_pairs"] == [["T1", "T2"], ["T1", "T3"]]
    realization_payload = judgement(CAP, REALIZATION_SUBJECT).payload
    assert set(realization_payload) == {"relation", "subject", "members", "candidates"}
    assert realization_payload["candidates"] == [
        {
            "id": "C1",
            "name": "Orders service",
            "label": "Module",
            "statements": ["Orders service statement."],
            "via": ["M1"],
        }
    ]
    orphan_payload = judgement(ORPH, ORPHAN_SUBJECT).payload
    assert set(orphan_payload) == {"relation", "subject", "candidates"}
    assert [c["name"] for c in orphan_payload["candidates"]] == [
        "Order export",
        "Checkout service",
    ]  # most similar first
    for rule, payload in ((INT, endpoints_payload), (CAP, realization_payload), (ORPH, orphan_payload)):
        prompt = ONTOLOGY.judge_prompt(rule.prompt)
        for key in payload:
            assert f"`{key}`" in prompt, (rule.prompt, key)
    assert all(f"`{field}`" in ONTOLOGY.judge_prompt(INT.prompt) for field in ("links", "source", "target"))
    assert "`targets`" in ONTOLOGY.judge_prompt(CAP.prompt) and "`via`" in ONTOLOGY.judge_prompt(CAP.prompt)
    assert "`target`" in ONTOLOGY.judge_prompt(ORPH.prompt)


def test_the_payload_bounds_its_fragments() -> None:
    many = {f"m-{index:02d}": node(f"m-{index:02d}", "Module", f"Module {index}") for index in range(12)}
    subject_node = node("int-9", "IntegrationRequirement", "Many", *((f"s-{i}", f"h-{i}") for i in range(5)))
    nodes = {**many, "int-9": subject_node}
    targets = tuple(sorted(many))
    subject = _Subject("int-9", "SPEC::INT-9", candidates=targets, pairs=((targets[0], targets[1]),), targets=targets)
    shown = judgement(INT, subject, nodes)
    assert len(shown.fragments) == inference.MAX_FRAGMENTS
    assert list(shown.fragments)[: inference.SUBJECT_FRAGMENTS] == ["s-0", "s-1", "s-2"]


async def test_document_text_reaches_the_judge_only_inside_untrusted_blocks() -> None:
    gateway = ScriptedGateway()
    gateway.answer(ORPH.prompt, None, {"confidence": 0.9, "evidence": QUOTE_REQ, "target": "C1"})
    shown = judgement(ORPH, ORPHAN_SUBJECT)
    fragments = {fragment_id: TEXTS[fragment_id] for fragment_id in shown.fragments}
    await JudgeRunner(gateway, ONTOLOGY).judge(
        ORPH.prompt, {**shown.payload, "fragments": fragments}, AttachOrphanVerdict
    )
    (call,) = gateway.calls
    system, user = call.messages[0]["content"], call.messages[1]["content"]
    assert system == f"{ONTOLOGY.judge_prompt(ORPH.prompt).strip()}\n\n{EVIDENCE_RULE}"
    assert outside_untrusted(user).strip() == ""
    blocks = dict(untrusted_blocks(user))
    assert set(blocks) == {"payload", *fragments}
    assert all(blocks[fragment_id] == text for fragment_id, text in fragments.items())
    assert json.loads(blocks["payload"]) == shown.payload
    for text in (*fragments.values(), "Customer downloads orders", "Order export"):
        assert text not in system and text not in outside_untrusted(user)


# Input hash (judge memory key, G1)


def test_the_input_hash_is_stable() -> None:
    first = judgement(INT, ENDPOINT_SUBJECT)
    shuffled_nodes = dict(reversed(list(NODES.items())))
    shuffled_subject = dataclasses.replace(ENDPOINT_SUBJECT, targets=("mod-c", "mod-a", "mod-b"))
    again = judgement(INT, shuffled_subject, shuffled_nodes)
    assert re.fullmatch(r"[0-9a-f]{64}", first.digest)
    assert again.digest == first.digest and again.payload == first.payload


def _changed(nodes: dict[str, _Node], uuid: str, **changes: Any) -> dict[str, _Node]:
    return {**nodes, uuid: dataclasses.replace(nodes[uuid], **changes)}


@pytest.mark.parametrize(
    "change",
    [
        "subject-quote",
        "target-quote",
        "target-name",
        "fragment-content-hash",
        "candidate-uuid",
        "prompt",
        "judge-model",
        "pairs",
    ],
)
def test_the_input_hash_changes_with_every_input(change: str) -> None:
    base = judgement(INT, ENDPOINT_SUBJECT).digest
    rule, subject, nodes, model = INT, ENDPOINT_SUBJECT, NODES, MODEL
    if change == "subject-quote":
        nodes = _changed(NODES, "int-1", quote="Checkout calls the Orders service.")
    elif change == "target-quote":
        nodes = _changed(NODES, "mod-b", quote="| Orders | Order management |")
    elif change == "target-name":
        nodes = _changed(NODES, "mod-b", name="Order management service")
    elif change == "fragment-content-hash":
        nodes = _changed(NODES, "int-1", fragments=(("frag-int", "hash-edited"),))
    elif change == "candidate-uuid":
        nodes = {**NODES, "mod-z": dataclasses.replace(NODES["mod-c"], uuid="mod-z")}
        subject = dataclasses.replace(
            ENDPOINT_SUBJECT,
            candidates=("mod-a", "mod-b", "mod-z"),
            targets=("mod-a", "mod-b", "mod-z"),
            pairs=(("mod-a", "mod-b"), ("mod-a", "mod-z")),
        )
    elif change == "prompt":
        rule = dataclasses.replace(INT, prompt="judge_endpoints_next")
    elif change == "judge-model":
        model = "another-judge"
    else:
        subject = dataclasses.replace(ENDPOINT_SUBJECT, pairs=(("mod-a", "mod-b"),))
    assert judgement(rule, subject, nodes, model).digest != base


@pytest.mark.parametrize(
    ("rule", "subject"),
    [(INT, ENDPOINT_SUBJECT), (CAP, REALIZATION_SUBJECT), (ORPH, ORPHAN_SUBJECT)],
    ids=["endpoints", "realization", "attach_orphan"],
)
def test_reworded_statements_of_unchanged_nodes_keep_the_input_hash(rule: InferenceRule, subject: _Subject) -> None:
    """A re-extraction that rewords a node's statements (or its ``stmt``, never shown) while its identity
    key, label, name, quote and fragments stay keeps the hash; the payload still shows the new texts."""

    base = judgement(rule, subject)
    reworded = {
        uuid: dataclasses.replace(found, statements=(f"Reworded {found.name}.",)) for uuid, found in NODES.items()
    }
    again = judgement(rule, subject, reworded)
    assert again.digest == base.digest and again.payload != base.payload
    requoted = _changed(NODES, subject.uuid, quote="A new quote line.")
    assert judgement(rule, subject, requoted).digest != base.digest


def test_a_new_order_of_the_same_orphan_candidates_keeps_the_input_hash() -> None:
    base = judgement(ORPH, ORPHAN_SUBJECT)
    turned = dataclasses.replace(ORPHAN_SUBJECT, candidates=tuple(reversed(ORPHAN_SUBJECT.candidates)))
    again = judgement(ORPH, turned)
    assert again.payload != base.payload and again.digest == base.digest
    fewer = dataclasses.replace(ORPHAN_SUBJECT, candidates=ORPHAN_SUBJECT.candidates[:1])
    assert judgement(ORPH, fewer).digest != base.digest


async def test_judge_memory_keys_by_project_rule_subject_and_hash_and_is_fingerprinted_by_prompt_and_model(
    tmp_path: Path,
) -> None:
    store = FileExtractionStore(tmp_path)
    memory = inference.JudgeMemory(store, "p-1", MODEL)
    key = memory.key(ORPH, "SPEC::REQ-1", "abc")
    assert key == f"p-1|{ORPH.id}|SPEC::REQ-1|abc"
    entry = {"outcome": "declined", "edges": []}
    await memory.remember(ORPH, {key: entry})
    assert await memory.recall(ORPH, [key]) == {key: entry}
    assert await inference.JudgeMemory(store, "p-1", "another-judge").recall(ORPH, [key]) == {}
    assert await memory.recall(dataclasses.replace(ORPH, prompt="judge_attach_orphan_next"), [key]) == {}
    assert await inference.JudgeMemory(None, "p-1", MODEL).recall(ORPH, [key]) == {}


async def test_judge_memory_with_the_ontology_is_fingerprinted_by_the_prompt_text(tmp_path: Path) -> None:
    store = FileExtractionStore(tmp_path)
    memory = inference.JudgeMemory(store, "p-1", MODEL, ONTOLOGY)
    key = memory.key(ORPH, "SPEC::REQ-1", "abc")
    entry = {"outcome": "declined", "edges": []}
    await memory.remember(ORPH, {key: entry})
    assert await inference.JudgeMemory(store, "p-1", MODEL, ONTOLOGY).recall(ORPH, [key]) == {key: entry}
    assert await inference.JudgeMemory(store, "p-1", MODEL).recall(ORPH, [key]) == {}  # bare reference
    prompts = {**ONTOLOGY.judge_prompts, ORPH.prompt: f"{ONTOLOGY.judge_prompt(ORPH.prompt)}\nMore."}
    edited = dataclasses.replace(ONTOLOGY, judge_prompts=prompts)
    assert await inference.JudgeMemory(store, "p-1", MODEL, edited).recall(ORPH, [key]) == {}


async def test_the_build_budget_is_kept_with_the_ontology_given(tmp_path: Path) -> None:
    """The budget entry names no judge prompt: it reads and writes with or without an ontology."""

    store = FileExtractionStore(tmp_path)
    await inference.JudgeMemory(store, "p-1", MODEL, ONTOLOGY).record_spent("b-1", 120)
    assert await inference.JudgeMemory(store, "p-1", MODEL, ONTOLOGY).spent("b-1") == 120
    assert await inference.JudgeMemory(store, "p-1", MODEL).spent("b-1") == 120


def test_a_stored_judgement_reads_back_or_is_judged_again() -> None:
    verdict = endpoints(("T1", "T2"), ("T1", "T3"))
    shown = judgement(INT, ENDPOINT_SUBJECT)
    texts = {fragment_id: TEXTS[fragment_id] for fragment_id in shown.fragments}
    outcome, proposals, _ = _evaluate(ONTOLOGY, INT, ENDPOINT_SUBJECT, shown, verdict, nodes=NODES, texts=texts)
    entry = json.loads(json.dumps(_memory_entry(outcome, proposals)))  # as the store keeps it
    recalled = _recalled(ONTOLOGY, INT, entry, NODES)
    assert recalled is not None and all(p.reused for p in recalled)
    assert [(p.edge_key, p.confidence, p.evidence) for p in recalled] == [
        (p.edge_key, p.confidence, p.evidence) for p in proposals
    ]
    assert _recalled(ONTOLOGY, INT, {"outcome": "declined", "edges": []}, NODES) == []
    broken = [
        {"outcome": "maybe", "edges": []},
        {"outcome": "accepted"},
        {"outcome": "accepted", "edges": [{"source": "mod-a", "target": "gone", "confidence": 0.9, "evidence": []}]},
        {
            "outcome": "accepted",
            "edges": [{"source": "mod-a", "target": "mod-b", "confidence": "high", "evidence": []}],
        },
        {  # stored before one direction per pair
            "outcome": "accepted",
            "edges": [
                {"source": "mod-a", "target": "mod-b", "confidence": 0.9, "evidence": []},
                {"source": "mod-b", "target": "mod-a", "confidence": 0.9, "evidence": []},
            ],
        },
    ]
    assert [_recalled(ONTOLOGY, INT, item, NODES) for item in broken] == [None] * len(broken)


# Ontology loader, check 7: the endpoints template's ``via`` and ``mentions`` (G13)


def load_variant(directory: Path, edit: Any) -> Any:
    """The frozen v1.3-in-format-2 ontology with its inference rules edited by ``edit``, or its problems."""

    data = yaml.safe_load(FROZEN_ONTOLOGY_FILE.read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    edit(data["inference"]["rules"])
    directory.mkdir(parents=True, exist_ok=True)
    copy_frozen_prompts(directory)
    path = directory / "ontology.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    try:
        return load_ontology(path, submodule_files=[])
    except OntologyError as error:
        return error.problems


def test_the_shipped_endpoints_rule_reads_part_of_relates_to_and_mentions() -> None:
    assert (INT.via, INT.mentions, INT.min_targets, INT.max_targets) == (("PART_OF", "RELATES_TO"), True, 2, 4)
    assert ONTOLOGY.is_symmetric("RELATES_TO") and not ONTOLOGY.is_symmetric("PART_OF")


def test_mentions_can_be_turned_off_and_via_can_be_one_symmetric_relation(tmp_path: Path) -> None:
    def edit(rules: list[dict[str, Any]]) -> None:
        rules[0]["mentions"] = False
        rules[0]["via"] = ["RELATES_TO"]

    loaded = load_variant(tmp_path, edit)
    rule = next(r for r in loaded.inference.rules if r.template == "endpoints")
    assert (rule.via, rule.mentions) == (("RELATES_TO",), False)


def test_mentions_is_optional_and_defaults_to_off(tmp_path: Path) -> None:
    def edit(rules: list[dict[str, Any]]) -> None:
        del rules[0]["mentions"]

    loaded = load_variant(tmp_path, edit)
    rule = next(r for r in loaded.inference.rules if r.template == "endpoints")
    assert rule.mentions is False


@pytest.mark.parametrize(
    ("index", "field", "value", "message"),
    [
        (1, "mentions", True, "not a parameter of template 'realization'"),
        (2, "via", ["PART_OF"], "not a parameter of template 'attach_orphan'"),
        (0, "mentions", "yes", "mentions"),
    ],
    ids=["mentions-on-realization", "via-on-attach-orphan", "mentions-not-a-boolean"],
)
def test_check7_rejects_via_and_mentions_where_they_do_not_belong(
    tmp_path: Path, index: int, field: str, value: Any, message: str
) -> None:
    problems = load_variant(tmp_path, lambda rules: rules[index].__setitem__(field, value))
    assert isinstance(problems, tuple) and any(message in problem for problem in problems), problems


# Inference v2.0: owner-based orphans, candidate labels from owner pairs
# exclude_where, exclude_owned_by, several container paths, the shared validator and rule order.
# These read the shipped v2.0 ontology: its owner pairs and flags are what the parameters act on.

V2 = shipped_ontology()
V2_RULES: dict[str, InferenceRule] = {rule.id: rule for rule in V2.inference.rules}
CONT1, CONT2, INT2, CAP2 = (V2_RULES[rule_id] for rule_id in ("INF-CONT-01", "INF-CONT-02", "INF-INT-01", "INF-CAP-01"))
STAMP = inference.InferenceStamp(build_id="b-1", at="2026-01-01T00:00:00+00:00", judge_model=MODEL)


def spec(uuid: str) -> str:
    return f"X::{uuid.upper()}"


class _FakeTx:
    def __init__(self, graph: FakeGraph) -> None:
        self._graph = graph

    async def __aenter__(self) -> _FakeTx:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def run(self, query: Any, **params: Any) -> list[dict[str, Any]]:
        rows = list(params["rows"])
        self._graph.writes.append((query.name, rows))
        return [{"refs": [row["ref"] for row in rows], "written": len(rows)}]


@dataclasses.dataclass
class FakeGraph:
    """An executor over a hand-made project graph: answers the reads inference makes by query name (spine nodes and
    edges, current edges, similar targets by a given order, exclusive holders, the shown nodes and fragment texts;
    ``endpoint_rows`` stands for that subject query; ``realization`` subjects are walked over the spine edges in code)
    and records reads and swap writes.
    """

    nodes: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    edges: list[tuple[str, str, str, str]] = dataclasses.field(default_factory=list)
    similar: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    endpoint_rows: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    hub_rows: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    embeddings: dict[str, list[float]] = dataclasses.field(default_factory=dict)
    titles: dict[str, str] = dataclasses.field(default_factory=dict)
    rules: dict[tuple[str, str, str], str] = dataclasses.field(default_factory=dict)
    """The rule of an inferred edge, by (relation, source, target)."""
    edge_props: dict[tuple[str, str, str], dict[str, Any]] = dataclasses.field(default_factory=dict)
    """Further stored properties of an edge (an inferred edge's confidence, evidence), by (relation, source, target)."""
    grounding: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    """A node's grounding fragment ids, in order (default: its own ``f-<uuid>``)."""
    fragment_texts: dict[str, str] = dataclasses.field(default_factory=dict)
    """Texts of fragments that are no node's own (default: the node text of ``f-<uuid>``)."""
    headings: dict[str, tuple[list[str], list[str]]] = dataclasses.field(default_factory=dict)
    """A fragment's heading path and the heading and fence lines of its text up to its node's evidence
    line, by fragment id (default: none)."""
    documents: dict[str, str] = dataclasses.field(default_factory=dict)
    """A fragment's document id, by fragment id (default: ``doc``)."""
    reads: list[tuple[str, dict[str, Any]]] = dataclasses.field(default_factory=list)
    writes: list[tuple[str, list[dict[str, Any]]]] = dataclasses.field(default_factory=list)

    def node(self, uuid: str, label: str, name: str = "", text: str = "", **attributes: str) -> FakeGraph:
        self.nodes[uuid] = {"label": label, "name": name or uuid, "text": text or f"Text of {uuid}.", **attributes}
        return self

    def edge(
        self,
        relation: str,
        source: str,
        target: str,
        basis: str = "stated",
        rule: str | None = None,
        *,
        props: dict[str, Any] | None = None,
    ) -> FakeGraph:
        self.edges.append((relation, source, target, basis))
        if rule is not None:
            self.rules[(relation, source, target)] = rule
        if props is not None:
            self.edge_props[(relation, source, target)] = props
        return self

    def reads_of(self, prefix: str) -> list[dict[str, Any]]:
        return [params for name, params in self.reads if name.startswith(prefix)]

    def _attributes(self, uuid: str) -> dict[str, str]:
        return {key: value for key, value in self.nodes[uuid].items() if key not in ("label", "name", "text")}

    async def read(self, query: Any, _project_id: str, /, **params: Any) -> list[dict[str, Any]]:
        name = query.name
        self.reads.append((name, params))
        handlers: tuple[tuple[str, Any], ...] = (
            ("build_read_spine_nodes", self._spine_nodes),
            ("build_read_spine_edges", self._spine_edges),
            ("build_read_current_edges_", self._current_edges),
            ("build_infer_similar_targets_", self._similar),
            ("build_infer_exclusive_holders_", self._holders),
            ("build_infer_endpoint_subjects_", lambda _name, _params: copy.deepcopy(self.endpoint_rows)),
            ("build_infer_xdoc_members", lambda _name, _params: copy.deepcopy(self.hub_rows)),
            ("build_infer_xdoc_embeddings", self._embeddings),
            ("build_infer_xdoc_documents", self._titles),
            ("build_read_infer_nodes", self._shown_nodes),
            ("build_read_infer_fragments", self._fragments),
        )
        handler = next((handler for prefix, handler in handlers if name.startswith(prefix)), None)
        if handler is None:
            raise AssertionError(f"unexpected read {name}")
        return handler(name, params)

    def _spine_nodes(self, _name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "uuid": uuid,
                "spec_id": spec(uuid),
                "labels": [node["label"]] if node["label"] in params["labels"] else [],
                "attributes": [[k, v] for k, v in self._attributes(uuid).items() if k in params["attributes"]],
            }
            for uuid, node in sorted(self.nodes.items())
        ]

    def _spine_edges(self, _name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"relation": rel, "source": s, "target": t, "basis": b, "inference_rule": self.rules.get((rel, s, t))}
            for rel, s, t, b in self.edges
            if rel in params["relations"]
        ]

    def _current_edges(self, name: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        relation = name.removeprefix("build_read_current_edges_")
        return [
            {
                "edge_key": f"k|{s}|{rel}|k|{t}",
                "props": {
                    **self.edge_props.get((rel, s, t), {}),
                    "basis": b,
                    "uuid": f"e|{s}|{t}",
                    "inference_rule": self.rules.get((rel, s, t)),
                },
                "source_uuid": s,
                "target_uuid": t,
                "source_spec_id": spec(s),
                "target_spec_id": spec(t),
            }
            for rel, s, t, b in self.edges
            if rel == relation
        ]

    def _similar(self, _name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        labels = set(params["target_labels"])
        return [
            {
                "uuid": uuid,
                "candidates": [
                    {"uuid": other, "score": 1.0}
                    for other in self.similar.get(uuid, [])
                    if self.nodes[other]["label"] in labels and other not in params["excluded"]
                ][: params["limit"]],
            }
            for uuid in params["uuids"]
        ]

    def _holders(self, name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        relation = name.removeprefix("build_infer_exclusive_holders_")
        pairs = {tuple(pair) for pair in params["pairs"]}
        return [
            {"uuid": s}
            for rel, s, t, b in self.edges
            if rel == relation
            and s in params["uuids"]
            and b in params["bases"]
            and (self.nodes[s]["label"], self.nodes[t]["label"]) in pairs
        ]

    def _embeddings(self, _name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"uuid": uuid, "embedding": self.embeddings[uuid]} for uuid in params["uuids"] if uuid in self.embeddings
        ]

    def _titles(self, _name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"document_id": d, "title": self.titles[d]} for d in params["document_ids"] if d in self.titles]

    def _shown_nodes(self, _name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "uuid": uuid,
                "spec_id": spec(uuid),
                "name": self.nodes[uuid]["name"],
                "identity_key": f"k|{uuid}",
                "labels": [self.nodes[uuid]["label"]],
                "statements": json.dumps([{"text": self.nodes[uuid]["name"]}]),
                "fragments": [
                    {
                        "fragment_id": fid,
                        "document_id": self.documents.get(fid, "doc"),
                        "content_hash": "h",
                        "heading_path": self.headings.get(fid, ([], []))[0],
                        "headings": self.headings.get(fid, ([], []))[1],
                    }
                    for fid in self.grounding.get(uuid, [f"f-{uuid}"])
                ],
            }
            for uuid in params["uuids"]
            if uuid in self.nodes
        ]

    def _fragments(self, _name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"fragment_id": fid, "text": self.fragment_texts.get(fid) or self.nodes[fid.removeprefix("f-")]["text"]}
            for fid in params["fragment_ids"]
        ]

    async def write(self, query: Any, _project_id: str, /, **_params: Any) -> list[dict[str, Any]]:
        raise AssertionError(f"inference wrote outside its swap with {query.name}")

    def tx(self, _project_id: str) -> _FakeTx:
        return _FakeTx(self)


def subject_of(request: Any) -> str:
    payload = json.loads(dict(untrusted_blocks(request.user))["payload"])
    return str((payload.get("subject") or payload["hub"])["name"])


def scripted(answers: dict[tuple[str, str], Any]) -> ScriptedGateway:
    gateway = ScriptedGateway()
    for prompt in {rule.prompt for rule in V2.inference.rules}:
        gateway.key_by(prompt, subject_of)
    for (prompt, name), value in answers.items():
        gateway.answer(prompt, name, value)
    return gateway


def attach_to(local_id: str | None, fragment: str = "", quote: str = "") -> dict[str, Any]:
    evidence = [{"fragment_id": fragment, "quote": quote}] if fragment else []
    return {"confidence": 0.9, "evidence": evidence, "target": local_id}


def with_rules(ontology: Any, *rules: InferenceRule) -> Any:
    spec_ = ontology.inference
    return dataclasses.replace(ontology, inference=InferenceSpec(cap=spec_.cap, floor=spec_.floor, rules=rules))


async def run_infer(
    graph: FakeGraph, ontology: Any, gateway: ScriptedGateway, memory: Any = None, *, write: bool = True
) -> Any:
    return await inference.infer(
        cast(Executor, graph),
        "p-1",
        ontology,
        JudgeRunner(gateway, ontology),
        stamp=STAMP,
        memory=memory,
        write=write,
    )


def test_rules_run_in_the_ontology_files_order() -> None:
    shipped = Path(__file__).resolve().parents[1] / "src" / "graph" / "core" / "ontology" / "spec-ontology.yaml"
    data = yaml.safe_load(shipped.read_text(encoding="utf-8"))
    yaml_order = [rule["id"] for rule in data["inference"]["rules"]]
    assert yaml_order == ["INF-CONT-01", "INF-CONT-02", "INF-INT-01", "INF-CAP-01", "INF-XDOC-01"]
    assert [rule.id for rule in V2.inference.rules] == yaml_order


async def test_infer_reports_every_rule_in_order_and_reads_the_graph_once() -> None:
    graph = FakeGraph()
    report = await run_infer(graph, V2, scripted({}))
    assert [rule.rule for rule in report.rules] == [rule.id for rule in V2.inference.rules]
    assert [name for name, _ in graph.reads].count("build_read_spine_nodes") == 1
    assert report.quarantine == () and graph.writes == []


async def test_a_rule_cap_bounds_its_subjects_instead_of_the_ontology_cap() -> None:
    graph = FakeGraph().node("feat-1", "Feature")
    for uuid in ("fr-1", "fr-2"):
        graph.node(uuid, "FunctionalRequirement")
        graph.similar[uuid] = ["feat-1"]
    capped = dataclasses.replace(CONT1, cap=1)
    answers = {(CONT1.prompt, uuid): attach_to(None) for uuid in ("fr-1", "fr-2")}
    (rule,) = (await run_infer(graph, with_rules(V2, capped), scripted(answers))).rules
    assert V2.inference.cap > 1
    assert (rule.subjects, rule.judged, rule.deferred) == (2, 1, 1)


@pytest.mark.parametrize(
    ("rule_id", "label", "labels"),
    [
        ("INF-CONT-01", "FunctionalRequirement", {"Feature", "Module"}),
        ("INF-CONT-01", "Constraint", {"Feature", "Module"}),
        ("INF-CONT-01", "IntegrationRequirement", {"Module"}),  # owned by an interface or a module
        ("INF-CONT-01", "DataRequirement", set()),  # owned by a data entity only: never a subject
        ("INF-CONT-02", "Feature", {"Module"}),
    ],
)
def test_candidate_labels_are_the_rule_targets_that_own_the_subject(rule_id: str, label: str, labels: set[str]) -> None:
    assert inference._candidate_labels(V2, V2_RULES[rule_id], label) == labels


def test_without_owner_pairs_orphans_keep_the_v1_definition() -> None:
    assert not inference._owner_based(ONTOLOGY, ORPH)
    assert inference._candidate_labels(ONTOLOGY, ORPH, "DataRequirement") == ORPH.targets
    assert inference._owner_based(V2, CONT1) and inference._owner_based(V2, CONT2)


def orphan_graph() -> FakeGraph:
    graph = FakeGraph()
    for uuid, label in (("feat-1", "Feature"), ("feat-2", "Feature"), ("mod-1", "Module"), ("iface-1", "Interface")):
        graph.node(uuid, label)
    graph.node("ent-1", "DataEntity").node("feat-out", "Feature", in_scope="no")
    graph.node("fr-free", "FunctionalRequirement")  # no owner: a subject
    graph.node("fr-owned", "FunctionalRequirement").edge("PART_OF", "fr-owned", "feat-1")
    graph.node("fr-implied", "QualityRequirement").edge("PART_OF", "fr-implied", "mod-1", "implied")
    graph.node("fr-inferred", "FunctionalRequirement").edge("PART_OF", "fr-inferred", "feat-1", "inferred")
    graph.node("fr-related", "BusinessRule").edge("RELATES_TO", "fr-related", "mod-1")  # reaches a module, no owner
    graph.node("fr-out", "FunctionalRequirement", in_scope="no")
    graph.node("fr-out-owner", "Constraint").edge("PART_OF", "fr-out-owner", "feat-out")  # owned: never a subject
    graph.node("fr-nocand", "FunctionalRequirement")
    graph.node("data-1", "DataRequirement")  # its only owner label is not a target
    graph.node("int-free", "IntegrationRequirement")
    graph.node("int-owned", "IntegrationRequirement").edge("PART_OF", "int-owned", "iface-1")
    graph.edge("PART_OF", "feat-2", "mod-1")  # feat-1 and feat-out have no module
    for uuid in ("fr-free", "fr-inferred", "fr-related", "fr-out", "data-1", "int-free", "feat-1", "feat-out"):
        graph.similar[uuid] = ["feat-2", "mod-1", "feat-1"]
    return graph


async def test_attach_orphan_subjects_have_no_owner_and_take_candidates_from_their_owner_labels() -> None:
    graph = orphan_graph()
    selection = await inference._select(cast(Executor, graph), "p-1", V2, CONT1)
    subjects = {subject.spec_id: subject.candidates for subject in selection.subjects}
    assert subjects == {
        "X::FR-FREE": ("feat-2", "mod-1", "feat-1"),
        "X::FR-INFERRED": ("feat-2", "mod-1", "feat-1"),  # an inferred owner does not count
        "X::FR-RELATED": ("feat-2", "mod-1", "feat-1"),  # reaching a module is not owning one
        "X::INT-FREE": ("mod-1",),  # modules only
    }
    assert selection.without_candidate == ("X::FR-NOCAND",)  # data-1 is neither a subject nor listed
    labels = sorted(params["target_labels"] for params in graph.reads_of("build_infer_similar_targets_"))
    assert labels == [["Feature", "Module"], ["Module"]]
    assert all("data-1" not in params["uuids"] for params in graph.reads_of("build_infer_similar_targets_"))


async def test_an_orphan_also_gets_the_hub_its_section_heading_names_as_a_candidate() -> None:
    """Beside the most similar candidates, the first heading up the orphan's section path (never the document title)
    that names one owner-legal hub adds it: by whole name without the section number, else by a hub name of two or more
    words the title contains; an excluded hub never counts.
    """

    graph = orphan_graph()
    graph.node("feat-3", "Feature", "Order Export").node("feat-users", "Feature", "Users")
    graph.nodes["feat-out"]["name"] = "Archive"
    graph.node("fr-users", "FunctionalRequirement").node("fr-title", "FunctionalRequirement")
    for uuid in ("fr-users", "fr-title"):
        graph.similar[uuid] = ["feat-2"]
    graph.headings.update(
        {
            "f-fr-free": (["Orders spec", "4. Order Export", "4.2 Limits"], []),  # walks up to an exact title
            "f-fr-related": (["Orders spec", "7. Rules for order export"], []),  # contains a two-word name
            "f-fr-out": (["Orders spec", "Archive"], []),  # out of scope: no candidate added (and no subject)
            "f-fr-inferred": (["Orders spec", "Archive"], []),  # names only an excluded hub
            "f-fr-users": (["Orders spec", "3.2 User Classes"], []),  # a one-word name is never contained
            "f-fr-title": (["Order Export"], []),  # the document title names no section
        }
    )
    selection = await inference._select(cast(Executor, graph), "p-1", V2, CONT1)
    subjects = {subject.spec_id: subject.candidates for subject in selection.subjects}
    assert subjects["X::FR-FREE"] == ("feat-2", "mod-1", "feat-1", "feat-3")
    assert subjects["X::FR-RELATED"] == ("feat-2", "mod-1", "feat-1", "feat-3")
    assert subjects["X::FR-INFERRED"] == ("feat-2", "mod-1", "feat-1")
    assert subjects["X::FR-USERS"] == subjects["X::FR-TITLE"] == ("feat-2",)
    graph.headings["f-fr-free"] = (["Orders spec", "4. Order Export"], [])
    graph.similar["fr-free"] = ["feat-3", "feat-2"]
    selection = await inference._select(cast(Executor, graph), "p-1", V2, CONT1)
    assert {s.spec_id: s.candidates for s in selection.subjects}["X::FR-FREE"] == ("feat-3", "feat-2")  # no duplicate


async def test_feature_orphans_are_features_without_a_module_and_not_out_of_scope() -> None:
    selection = await inference._select(cast(Executor, orphan_graph()), "p-1", V2, CONT2)
    assert [(subject.spec_id, subject.candidates) for subject in selection.subjects] == [("X::FEAT-1", ("mod-1",))]


async def test_orphan_candidates_leave_out_excluded_targets_and_targets_their_owner_pair_forbids() -> None:
    """``target_exclude_where`` (an out-of-scope feature) and the owner pair's ``target_where`` (an external module for
    a functional requirement; an external or deployment module for a feature) are never candidates; a constraint's
    module pair has no ``target_where``, so it keeps the external module.
    """

    graph = FakeGraph()
    graph.node("feat-in", "Feature").node("feat-out", "Feature", in_scope="no")
    graph.node("mod-ext", "Module", external="yes", type="backend").node("mod-dep", "Module", type="deployment")
    graph.node("mod-in", "Module", external="no", type="backend")
    graph.node("fr-1", "FunctionalRequirement").node("con-1", "Constraint").node("feat-1", "Feature")
    graph.edge("PART_OF", "feat-in", "mod-in")
    for uuid in ("fr-1", "con-1", "feat-1"):
        graph.similar[uuid] = ["feat-out", "mod-ext", "mod-dep", "feat-in", "mod-in"]
    assert CONT1.target_exclude_where == CONT2.target_exclude_where == {"in_scope": frozenset({"no"})}
    cont1 = await inference._select(cast(Executor, graph), "p-1", V2, CONT1)
    assert {subject.spec_id: subject.candidates for subject in cont1.subjects} == {
        "X::CON-1": ("mod-ext", "mod-dep", "feat-in"),
        "X::FR-1": ("mod-dep", "feat-in", "mod-in"),
    }
    excluded = sorted(sorted(params["excluded"]) for params in graph.reads_of("build_infer_similar_targets_"))
    assert excluded == [["feat-out"], ["feat-out", "mod-ext"]]  # one read per distinct exclusion
    cont2 = await inference._select(cast(Executor, graph), "p-1", V2, CONT2)
    assert {subject.spec_id: subject.candidates for subject in cont2.subjects} == {"X::FEAT-1": ("mod-in",)}


async def test_an_orphan_payload_shows_the_section_each_node_sits_in() -> None:
    graph = FakeGraph().node("fr-1", "BusinessRule", "At most 10 checks per minute")
    graph.node("feat-api", "Feature", "Diagnostics endpoints").node("feat-diag", "Feature", "Diagnostics")
    graph.node("feat-none", "Feature", "Elsewhere")
    graph.headings = {
        "f-fr-1": (["Requirements", "14. Diagnostics"], ["### 14.1 Checks", "### 14.2 Rate limits", "#### Table"]),
        "f-feat-api": (["API", "22. HTTP API"], ["```", "# not a heading", "```", "### 22.10 Diagnostics endpoints"]),
        "f-feat-diag": (["Requirements", "14. Diagnostics"], ["### 14. Diagnostics"]),
    }
    nodes = await inference._nodes(cast(Executor, graph), "p-1", V2, ["fr-1", "feat-api", "feat-diag", "feat-none"])
    assert nodes["fr-1"].sections == ("Requirements > 14. Diagnostics > 14.2 Rate limits > Table",)
    subject = inference._Subject("fr-1", spec("fr-1"), candidates=("feat-api", "feat-diag", "feat-none"))
    found = inference._judgement(CONT1, subject, nodes, MODEL)
    assert found is not None
    assert found.payload["subject"]["sections"] == ["Requirements > 14. Diagnostics > 14.2 Rate limits > Table"]
    assert [(c["name"], c["sections"]) for c in found.payload["candidates"]] == [
        ("Diagnostics endpoints", ["API > 22. HTTP API > 22.10 Diagnostics endpoints"]),
        ("Diagnostics", ["Requirements > 14. Diagnostics"]),  # its own heading line is not repeated
        ("Elsewhere", []),  # no heading known
    ]
    endpoints = judgement(INT, ENDPOINT_SUBJECT)
    assert "sections" not in endpoints.payload["subject"]  # other templates show no sections


def test_exclude_where_matches_the_node_or_its_owner() -> None:
    graph = orphan_graph()
    graph.node("fr-x", "FunctionalRequirement").edge("PART_OF", "fr-x", "feat-out", "implied")
    graph.node("fr-y", "FunctionalRequirement").edge("PART_OF", "fr-y", "feat-out", "inferred")
    spine = SpineGraph(
        tuple(
            SpineNode(uuid, spec(uuid), node["label"], graph._attributes(uuid)) for uuid, node in graph.nodes.items()
        ),
        tuple(SpineEdge(rel, s, t, b) for rel, s, t, b in graph.edges),
    )
    loaded = inference._Loaded.of(V2, spine)
    by_key = loaded.nodes
    excluded = {key for key in ("fr-out", "fr-x", "fr-y", "fr-free") if loaded.excluded(V2, CONT1, by_key[key])}
    assert excluded == {"fr-out", "fr-x"}  # itself; through its (implied) owner; not through an inferred one
    assert not loaded.excluded(V2, INT2, by_key["fr-out"])  # a rule without exclude_where excludes nothing


async def test_endpoints_skip_subjects_owned_by_an_excluded_label() -> None:
    assert INT2.exclude_owned_by == frozenset({"Interface"})
    graph = FakeGraph()
    for uuid in ("mod-1", "mod-2"):
        graph.node(uuid, "Module")
    graph.node("iface-1", "Interface")
    graph.node("int-a", "IntegrationRequirement").edge("PART_OF", "int-a", "iface-1")
    graph.node("int-b", "IntegrationRequirement").edge("PART_OF", "int-b", "mod-1")
    graph.node("int-c", "IntegrationRequirement").edge("PART_OF", "int-c", "iface-1")
    graph.node("int-d", "IntegrationRequirement").edge("PART_OF", "int-d", "iface-1", "inferred")
    graph.endpoint_rows = [
        {"uuid": "int-a", "spec_id": spec("int-a"), "targets": ["mod-1", "mod-2"]},
        {"uuid": "int-b", "spec_id": spec("int-b"), "targets": ["mod-1", "mod-2"]},
        {"uuid": "int-c", "spec_id": spec("int-c"), "targets": ["mod-1"]},  # would be an outlier
        {"uuid": "int-d", "spec_id": spec("int-d"), "targets": ["mod-1", "mod-2"]},
    ]
    selection = await inference._select(cast(Executor, graph), "p-1", V2, INT2)
    assert [subject.spec_id for subject in selection.subjects] == ["X::INT-B", "X::INT-D"]
    assert selection.outliers == ()


def test_realization_reads_one_query_per_container_path() -> None:
    assert len(CAP2.container_paths) == 4
    texts = [render_cypher(query) for query in inference._realization_queries(V2, CAP2)]
    patterns = (
        "(m)-[:PART_OF*1..3]->(c:SpecNode)",
        "(m)-[:RELATES_TO*1..1]-(c:SpecNode)",  # symmetric: either direction
        "(m)-[:PART_OF]->(:SpecNode)-[:PROVIDED_BY]->(c:SpecNode)",
        "(m)-[:PART_OF]->(:SpecNode)-[:CONSUMED_BY]->(c:SpecNode)",
    )
    assert [[pattern in text for pattern in patterns] for text in texts] == [
        [index == position for index in range(4)] for position in range(4)
    ]
    (frozen,) = inference._realization_queries(ONTOLOGY, CAP)
    assert "(m)-[:PART_OF*1..3]->(c:SpecNode)" in render_cypher(frozen)


def realization_graph() -> FakeGraph:
    """``cap-1`` with members f-1, r-1, r-2, r-3 (``GROUPED_BY``): m-a by ``PART_OF`` 1-3 (f-1 -> f-2 -> m-a) and by
    ``RELATES_TO`` stored the other way (m-a - r-1), m-b by ``PART_OF`` then ``PROVIDED_BY`` (r-2 -> ifc-1 -> m-b), m-c
    by ``PART_OF`` then ``CONSUMED_BY`` (r-3 -> ifc-2 -> m-c); not m-deep (4 hops), m-done (already ``REALIZED_BY``),
    m-inf (an inferred edge) or m-other (reached from a node that is no member).
    """

    graph = FakeGraph()
    for uuid in ("cap-1",):
        graph.node(uuid, "Capability", "Audit logging", "Audit logging writes every download to the log.")
    for uuid in ("m-a", "m-b", "m-c", "m-deep", "m-done", "m-inf", "m-other"):
        graph.node(uuid, "Module")
    for uuid in ("f-1", "f-2", "f-3", "f-4"):
        graph.node(uuid, "Feature")
    for uuid in ("r-1", "r-2", "r-3", "r-4", "r-x"):
        graph.node(uuid, "FunctionalRequirement")
    graph.node("ifc-1", "Interface").node("ifc-2", "Interface")
    for member in ("f-1", "r-1", "r-2", "r-3", "r-4"):
        graph.edge("GROUPED_BY", member, "cap-1")
    graph.edge("PART_OF", "f-1", "f-2").edge("PART_OF", "f-2", "m-a")
    graph.edge("PART_OF", "f-2", "f-3").edge("PART_OF", "f-3", "f-4").edge("PART_OF", "f-4", "m-deep")
    graph.edge("RELATES_TO", "m-a", "r-1")
    graph.edge("PART_OF", "r-2", "ifc-1").edge("PROVIDED_BY", "ifc-1", "m-b")
    graph.edge("PART_OF", "r-3", "ifc-2").edge("CONSUMED_BY", "ifc-2", "m-c")
    graph.edge("PART_OF", "f-1", "m-done").edge("REALIZED_BY", "cap-1", "m-done")
    graph.edge("PART_OF", "r-4", "m-inf", "inferred", rule=CONT1.id)
    graph.edge("PART_OF", "r-x", "m-other")
    return graph


async def test_realization_walks_every_container_path_in_code_over_read_edges() -> None:
    graph = realization_graph()
    selection = await inference._select(cast(Executor, graph), "p-1", V2, CAP2)
    (subject,) = selection.subjects
    assert subject.candidates == ("m-a", "m-b", "m-c")
    assert subject.members == ("f-1", "r-1", "r-2", "r-3", "r-4")
    assert dict(subject.via) == {"m-a": ("f-1", "r-1"), "m-b": ("r-2",), "m-c": ("r-3",)}
    assert dict(subject.reads) == {}  # no overlay: nothing read
    assert graph.reads_of("build_infer_realization_subjects_") == []


def test_the_realization_walk_sees_the_overlay_and_names_the_read_edges_behind_each_candidate() -> None:
    graph = realization_graph()
    spine = SpineGraph(
        tuple(SpineNode(uuid, spec(uuid), node["label"]) for uuid, node in graph.nodes.items()),
        tuple(SpineEdge(rel, s, t, b, graph.rules.get((rel, s, t))) for rel, s, t, b in graph.edges),
    )
    overlay = (
        SpineEdge("PART_OF", "r-4", "f-3", "inferred", CONT1.id),  # r-4 -> f-3 -> f-4 -> m-deep: 3 hops
        SpineEdge("PART_OF", "f-3", "m-a", "inferred", CONT2.id),
    )
    (row,) = infer_reads.realization_rows(V2, CAP2, spine, bases=("stated", "implied"), overlay=overlay)
    assert sorted(row.containers) == ["m-a", "m-b", "m-c", "m-deep", "m-done"]  # the rows keep m-done
    assert row.containers["m-deep"] == infer_reads.Container(("r-4",), ("r-4|PART_OF|f-3",))
    # m-a: f-1 -> f-2 -> m-a over read edges, f-1 -> f-2 -> f-3 -> m-a and r-4 -> f-3 -> m-a over overlay
    # edges: read edges alone reach it, so it keeps no overlay edge (they cannot make or unmake it)
    assert row.containers["m-a"].via == ("f-1", "r-1", "r-4")
    assert row.containers["m-a"].reads == ()
    assert row.containers["m-b"].reads == ()


def test_a_candidate_reached_without_the_overlay_keeps_no_read_edges() -> None:
    """m-a is reached over read edges (f-1 -> f-2 -> m-a) and over the overlay (r-4 -> f-3 -> m-a): it
    keeps no read edge with the overlay edge into it or without it (only its ``via`` tells them apart)."""

    graph = realization_graph()
    spine = SpineGraph(
        tuple(SpineNode(uuid, spec(uuid), node["label"]) for uuid, node in graph.nodes.items()),
        tuple(SpineEdge(rel, s, t, b, graph.rules.get((rel, s, t))) for rel, s, t, b in graph.edges),
    )
    into_a = SpineEdge("PART_OF", "f-3", "m-a", "inferred", CONT2.id)
    to_f3 = SpineEdge("PART_OF", "r-4", "f-3", "inferred", CONT1.id)
    walked = [
        infer_reads.realization_rows(V2, CAP2, spine, bases=("stated", "implied"), overlay=overlay)[0]
        for overlay in ((to_f3, into_a), (to_f3,))
    ]
    assert [row.containers["m-a"] for row in walked] == [
        infer_reads.Container(("f-1", "r-1", "r-4")),
        infer_reads.Container(("f-1", "r-1")),
    ]
    # m-deep is reached only over the overlay: it keeps the read edge behind it
    assert {row.containers["m-deep"].reads for row in walked} == {("r-4|PART_OF|f-3",)}


def reads_graph() -> FakeGraph:
    """``cap-1`` groups the orphan fr-1; feat-1 sits in mod-1. ``INF-CONT-01`` attaches fr-1 to
    feat-1 in the build, and only that edge leads ``INF-CAP-01`` from fr-1 to mod-1."""

    graph = FakeGraph()
    graph.node("cap-1", "Capability", "Order export", "Order export lets staff download orders.")
    graph.node("fr-1", "FunctionalRequirement", "Staff downloads orders", "Staff downloads orders as CSV.")
    graph.node("feat-1", "Feature", "Export screen").node("mod-1", "Module", "Orders service", type="backend")
    graph.edge("GROUPED_BY", "fr-1", "cap-1").edge("PART_OF", "feat-1", "mod-1")
    graph.similar["fr-1"] = ["feat-1"]
    return graph


def reads_answers() -> dict[tuple[str, str], Any]:
    return {
        (CONT1.prompt, "Staff downloads orders"): attach_to("C1", "f-fr-1", "Staff downloads orders"),
        (CAP2.prompt, "Order export"): {
            "confidence": 0.9,
            "evidence": [{"fragment_id": "f-cap-1", "quote": "lets staff download orders"}],
            "targets": ["C1"],
        },
    }


async def test_inf_cap_01_sees_the_inf_cont_edges_accepted_in_the_same_build_and_a_dry_run_sees_the_same() -> None:
    assert CAP2.reads == (CONT1.id, CONT2.id)
    graph = reads_graph()
    gateway = scripted(reads_answers())
    report = await run_infer(graph, V2, gateway)
    rules = {rule.rule: rule for rule in report.rules}
    assert (rules[CAP2.id].subjects, rules[CAP2.id].accepted, rules[CAP2.id].read_edges) == (1, 1, 1)
    assert rules[CONT1.id].read_edges is None  # a rule without reads
    assert set(report.inferred_added) == {"X::CAP-1|REALIZED_BY|X::MOD-1", "X::FR-1|PART_OF|X::FEAT-1"}
    (call,) = gateway.calls_for(CAP2.prompt)
    payload = json.loads(dict(untrusted_blocks(call.messages[-1]["content"]))["payload"])
    assert [(c["name"], c["via"]) for c in payload["candidates"]] == [("Orders service", ["M1"])]

    dry_graph = reads_graph()
    dry = await run_infer(dry_graph, V2, scripted(reads_answers()), write=False)
    assert dry.rules == report.rules and dry.inferred_added == report.inferred_added
    assert dry_graph.writes == []

    assert (report.spine_unreachable_before, report.spine_unreachable_after) == (1, 0)  # fr-1 reaches mod-1
    assert (dry.spine_unreachable_before, dry.spine_unreachable_after) == (1, 0)

    without = with_rules(V2, CONT1, dataclasses.replace(CAP2, reads=()))
    alone = await run_infer(reads_graph(), without, scripted(reads_answers()))
    assert [(rule.rule, rule.subjects, rule.read_edges) for rule in alone.rules] == [
        (CONT1.id, 1, None),
        (CAP2.id, 0, None),
    ]


async def test_a_reading_subject_is_judged_again_when_the_read_edges_behind_a_candidate_change(tmp_path: Path) -> None:
    memory = inference.JudgeMemory(FileExtractionStore(tmp_path), "p-1", MODEL)
    graph = reads_graph()
    await run_infer(graph, V2, scripted(reads_answers()), memory)
    graph.edge("PART_OF", "fr-1", "feat-1", "inferred", rule=CONT1.id)  # the edge the first build wrote

    again = await run_infer(graph, V2, scripted({}), memory)  # every judgement reused
    assert {rule.rule: (rule.reused, rule.judged) for rule in again.rules if rule.subjects} == {
        CONT1.id: (1, 0),
        CAP2.id: (1, 0),
    }

    graph.node("feat-2", "Feature", "Report screen").edge("PART_OF", "feat-2", "mod-1")
    graph.similar["fr-1"] = ["feat-2"]  # now attached to feat-2: same container, another read edge behind it
    answers = reads_answers()
    gateway = scripted(answers)
    changed = await run_infer(graph, V2, gateway, memory)
    cap = next(rule for rule in changed.rules if rule.rule == CAP2.id)
    assert (cap.reused, cap.judged, cap.read_edges) == (0, 1, 1)
    assert len(gateway.calls_for(CAP2.prompt)) == 1


async def test_a_reused_judgement_leaves_its_edge_and_refreshes_one_an_earlier_judgement_left(
    tmp_path: Path,
) -> None:
    """judgements reach judge memory before the swap; when the swap then fails, the next
    build reuses them while the graph still holds the edge an earlier judgement wrote."""

    memory = inference.JudgeMemory(FileExtractionStore(tmp_path), "p-1", MODEL)
    ontology = with_rules(V2, CONT1)
    graph = reads_graph()
    verdict = attach_to("C1", "f-fr-1", "Staff downloads orders")
    await run_infer(graph, ontology, scripted({(CONT1.prompt, "Staff downloads orders"): verdict}), memory)
    ((_, (written,)),) = [(name, rows) for name, rows in graph.writes if rows]
    edge = ("PART_OF", "fr-1", "feat-1")
    assert (written["source"], written["target"], written["props"]["confidence"]) == ("fr-1", "feat-1", 0.9)

    # the edge as the swap wrote it: the reused judgement leaves it in place
    graph.edge(*edge, "inferred", rule=CONT1.id, props=written["props"])
    graph.writes.clear()
    report = await run_infer(graph, ontology, scripted({}), memory)
    assert counts_of(report.rules[0]) == (1, 0, 0, 0)
    assert [rows for _, rows in graph.writes if rows] == []

    # the edge as an earlier judgement wrote it (another confidence and quote): refreshed from the judgement
    graph.edge_props[edge] = {**written["props"], "confidence": 0.8, "evidence": "[]"}
    report = await run_infer(graph, ontology, scripted({}), memory)
    assert counts_of(report.rules[0]) == (1, 0, 0, 1)
    ((_, (refreshed,)),) = [(name, rows) for name, rows in graph.writes if rows]
    assert refreshed["props"]["confidence"] == 0.9
    assert refreshed["props"]["evidence"] == written["props"]["evidence"]


def counts_of(rule: Any) -> tuple[int, int, int, int]:
    """A rule's reused subjects and edges added, removed and updated."""

    return (rule.reused, rule.edges_added, rule.edges_removed, rule.edges_updated)


def test_the_judgement_hash_of_a_subject_without_read_edges_is_unchanged() -> None:
    plain = judgement(CAP, REALIZATION_SUBJECT)
    empty = judgement(CAP, dataclasses.replace(REALIZATION_SUBJECT, reads={}))
    read = judgement(CAP, dataclasses.replace(REALIZATION_SUBJECT, reads={"mod-b": ("feat-1|PART_OF|mod-b",)}))
    assert plain.digest == empty.digest != read.digest
    assert plain.payload == read.payload  # the judge never sees the edges


def test_a_read_rule_without_a_plan_keeps_its_current_inferred_edges() -> None:
    spine = SpineGraph(
        (SpineNode("a", "X::A", "FunctionalRequirement"), SpineNode("b", "X::B", "Feature")),
        (SpineEdge("PART_OF", "a", "b", "inferred", CONT1.id), SpineEdge("PART_OF", "b", "a", "inferred", "OTHER")),
    )
    loaded = inference._Loaded.of(V2, spine)
    assert inference._overlay(loaded, CAP2, []) == (SpineEdge("PART_OF", "a", "b", "inferred", CONT1.id),)
    assert inference._overlay(loaded, CONT1, []) == ()


# Batched attach_orphan calls

BATCH_PROMPT = "judge_attach_orphan_batch"
SECTION = "Order export: staff downloads orders. Staff filters orders by date. Staff prints the list."


def batch_graph() -> FakeGraph:
    """Orphans fr-1..fr-4 written in the section f-sec (their top fragment) and fr-5 elsewhere, all with
    the candidates feat-1 (C1) and mod-1 (C2)."""

    graph = FakeGraph()
    graph.node("feat-1", "Feature", "Order export").node("mod-1", "Module", "Orders service", type="backend")
    graph.fragment_texts["f-sec"] = SECTION
    for index in range(1, 6):
        uuid = f"fr-{index}"
        graph.node(uuid, "FunctionalRequirement", f"Requirement {index}", f"Requirement {index} text.")
        graph.similar[uuid] = ["feat-1", "mod-1"]
        if index < 5:
            graph.grounding[uuid] = ["f-sec", f"f-{uuid}"]
    return graph


def batched(size: int = 3) -> Any:
    return with_rules(V2, dataclasses.replace(CONT1, batch=size))


def batch_answers(*answers: dict[str, Any]) -> dict[str, Any]:
    return {"confidence": 0.5, "evidence": [], "answers": list(answers)}


def batch_answer(subject: str, target: str | None, fragment: str = "f-sec", quote: str = "") -> dict[str, Any]:
    evidence = [{"fragment_id": fragment, "quote": quote}] if quote else []
    return {"subject": subject, "target": target, "confidence": 0.9, "evidence": evidence}


def single_answers() -> dict[tuple[str, str], Any]:
    return {
        (CONT1.prompt, "Requirement 4"): attach_to(None),
        (CONT1.prompt, "Requirement 5"): attach_to("C2", "f-fr-5", "Requirement 5 text"),
    }


def batch_payloads(gateway: ScriptedGateway) -> list[dict[str, Any]]:
    return [
        json.loads(dict(untrusted_blocks(call.messages[-1]["content"]))["payload"])
        for call in gateway.calls_for(BATCH_PROMPT)
    ]


async def test_orphans_sharing_their_top_fragment_and_candidates_are_judged_together_each_on_its_own() -> None:
    gateway = scripted(single_answers())
    gateway.handle(
        BATCH_PROMPT,
        lambda _request: batch_answers(
            batch_answer("S1", "C1", quote="staff downloads orders"),
            batch_answer("S2", None),
            batch_answer("S3", "C1", "f-fr-1", "Requirement 1 text"),  # a quote of another subject's fragment
        ),
    )
    report = await run_infer(batch_graph(), batched(), gateway)
    (rule,) = report.rules
    assert (rule.subjects, rule.judged, rule.accepted, rule.declined, rule.rejected) == (5, 5, 2, 2, 1)
    assert report.inferred_added == ("X::FR-1|PART_OF|X::FEAT-1", "X::FR-5|PART_OF|X::MOD-1")
    (payload,) = batch_payloads(gateway)  # fr-1..fr-3; fr-4 (the group's rest) and fr-5 alone: single calls
    assert [(item["id"], item["name"], item["fragments"]) for item in payload["subjects"]] == [
        (f"S{index}", f"Requirement {index}", ["f-sec", f"f-fr-{index}"]) for index in (1, 2, 3)
    ]
    assert [(item["id"], item["name"]) for item in payload["candidates"]] == [
        ("C1", "Order export"),
        ("C2", "Orders service"),
    ]
    singles = [
        json.loads(dict(untrusted_blocks(call.messages[-1]["content"]))["payload"])
        for call in gateway.calls_for(CONT1.prompt)
    ]
    assert sorted(single["subject"]["name"] for single in singles) == ["Requirement 4", "Requirement 5"]
    assert rule.budget_tokens is not None and rule.budget_tokens > 0
    outcomes = {(item.subject, item.outcome) for item in report.judged_items or ()}
    assert outcomes == {
        ("X::FR-1", "accepted"),
        ("X::FR-2", "declined"),
        ("X::FR-3", "quote_missing"),
        ("X::FR-4", "declined"),
        ("X::FR-5", "accepted"),
    }


def fallback_answers() -> dict[tuple[str, str], Any]:
    return {
        **single_answers(),
        (CONT1.prompt, "Requirement 1"): attach_to("C1", "f-sec", "staff downloads orders"),
        (CONT1.prompt, "Requirement 2"): attach_to(None),
        (CONT1.prompt, "Requirement 3"): attach_to(None),
    }


@pytest.mark.parametrize(
    "answer",
    [
        batch_answers(*(batch_answer(f"S{index}", None) for index in (1, 2, 3)), batch_answer("S1", None)),  # twice
        batch_answers(*(batch_answer(f"S{index}", None) for index in (1, 2)), batch_answer("S7", None)),  # unknown
        LLMError("malformed"),
    ],
    ids=["twice", "unknown", "failed"],
)
async def test_a_failed_or_malformed_batch_is_judged_again_one_subject_per_call(answer: Any) -> None:
    gateway = scripted(fallback_answers())
    gateway.handle(BATCH_PROMPT, lambda _request: answer)
    (rule,) = (await run_infer(batch_graph(), batched(), gateway)).rules
    assert len(gateway.calls_for(BATCH_PROMPT)) == 1 and len(gateway.calls_for(CONT1.prompt)) == 5
    assert (rule.judged, rule.accepted, rule.declined, rule.failed) == (5, 2, 3, 0)


async def test_a_subject_a_batch_leaves_without_an_answer_is_judged_again_on_its_own() -> None:
    gateway = scripted(fallback_answers())
    answer = batch_answers(batch_answer("S1", "C1", quote="staff downloads orders"), batch_answer("S3", None))
    gateway.handle(BATCH_PROMPT, lambda _request: answer)  # no answer for S2 (Requirement 2)
    report = await run_infer(batch_graph(), batched(), gateway)
    (rule,) = report.rules
    assert len(gateway.calls_for(BATCH_PROMPT)) == 1
    assert sorted(call.key for call in gateway.calls_for(CONT1.prompt)) == [
        "Requirement 2",
        "Requirement 4",
        "Requirement 5",
    ]
    assert (rule.judged, rule.accepted, rule.declined, rule.failed) == (5, 2, 3, 0)
    assert report.inferred_added == ("X::FR-1|PART_OF|X::FEAT-1", "X::FR-5|PART_OF|X::MOD-1")


def section_graph() -> FakeGraph:
    """fr-1..fr-3 written in the section "Orders > Export" of doc-a (fragments f-a1, f-a2), fr-4 in the
    same-named section of doc-b, fr-5 in "Orders > Import" of doc-a. fr-1 has the candidates feat-1 and
    mod-1, fr-2 feat-2 and feat-1, fr-3 mod-1."""

    graph = FakeGraph()
    graph.node("feat-1", "Feature", "Order export").node("feat-2", "Feature", "Export filters")
    graph.node("mod-1", "Module", "Orders service", type="backend")
    place = {"fr-1": "f-a1", "fr-2": "f-a2", "fr-3": "f-a1", "fr-4": "f-b1", "fr-5": "f-a3"}
    for uuid, fragment in place.items():
        graph.node(uuid, "FunctionalRequirement", f"Requirement {uuid[-1]}", f"Requirement {uuid[-1]} text.")
        graph.grounding[uuid] = [fragment]
        graph.fragment_texts[fragment] = f"Section text of {fragment}: staff downloads orders."
        graph.documents[fragment] = "doc-b" if fragment == "f-b1" else "doc-a"
        graph.headings[fragment] = (["Orders", "Import" if fragment == "f-a3" else "Export"], [])
    graph.similar.update(
        {"fr-1": ["feat-1", "mod-1"], "fr-2": ["feat-2", "feat-1"], "fr-3": ["mod-1"], "fr-4": ["feat-1"]}
    )
    graph.similar["fr-5"] = ["feat-1"]
    return graph


async def test_a_batch_takes_the_orphans_of_one_section_of_one_document_each_with_its_own_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grouped over the whole judging order (not per chunk of ``CHUNK`` subjects): fr-1..fr-3 share "Orders > Export" of
    doc-a; fr-4 (another document) and fr-5 (another section) are judged alone.
    """

    monkeypatch.setattr(inference.infer_bound, "CHUNK", 2)
    gateway = scripted(
        {
            (CONT1.prompt, "Requirement 4"): attach_to(None),
            (CONT1.prompt, "Requirement 5"): attach_to(None),
        }
    )
    gateway.handle(
        BATCH_PROMPT,
        lambda _request: batch_answers(
            batch_answer("S1", "C1", "f-a1", "staff downloads orders"),
            batch_answer("S2", "C3", "f-a2", "staff downloads orders"),
            batch_answer("S3", "C1", "f-a1", "staff downloads orders"),  # C1 (feat-1) is not one of S3's
        ),
    )
    report = await run_infer(section_graph(), batched(8), gateway)
    (payload,) = batch_payloads(gateway)
    assert [(item["id"], item["name"], item["fragments"], item["candidates"]) for item in payload["subjects"]] == [
        ("S1", "Requirement 1", ["f-a1"], ["C1", "C2"]),
        ("S2", "Requirement 2", ["f-a2"], ["C3", "C1"]),
        ("S3", "Requirement 3", ["f-a1"], ["C2"]),
    ]
    assert [(item["id"], item["name"]) for item in payload["candidates"]] == [
        ("C1", "Order export"),
        ("C2", "Orders service"),
        ("C3", "Export filters"),
    ]
    (call,) = gateway.calls_for(BATCH_PROMPT)
    blocks = [label for label, _ in untrusted_blocks(call.messages[-1]["content"])]
    assert blocks == ["payload", "f-a1", "f-a2", "f-feat-1", "f-feat-2", "f-mod-1"]  # each fragment once
    assert sorted(call.key for call in gateway.calls_for(CONT1.prompt)) == ["Requirement 4", "Requirement 5"]
    (rule,) = report.rules
    assert (rule.judged, rule.accepted, rule.invalid, rule.declined) == (5, 2, 1, 2)
    assert report.inferred_added == ("X::FR-1|PART_OF|X::FEAT-1", "X::FR-2|PART_OF|X::FEAT-2")


async def test_batched_judgements_are_remembered_per_subject_under_its_single_input_hash(tmp_path: Path) -> None:
    """A batched verdict is stored under each subject's own single-call input hash (the single prompt's
    judge memory): switching ``batch`` on or off, regrouping or another batch prompt judges nothing again."""

    memory = inference.JudgeMemory(FileExtractionStore(tmp_path), "p-1", MODEL)
    gateway = scripted(single_answers())
    gateway.handle(BATCH_PROMPT, lambda _request: batch_answers(*(batch_answer(f"S{i}", None) for i in (1, 2, 3))))
    await run_infer(batch_graph(), batched(), gateway, memory)

    again = scripted({})
    (rule,) = (await run_infer(batch_graph(), batched(), again, memory)).rules
    assert (rule.reused, rule.judged, again.calls) == (5, 0, [])
    regrouped = scripted({})  # batches of 2: other groups, the same subjects: still reused
    (rule,) = (await run_infer(batch_graph(), batched(2), regrouped, memory)).rules
    assert (rule.reused, regrouped.calls) == (5, [])

    unbatched = scripted({})  # batching off: the same single inputs, still reused
    (rule,) = (await run_infer(batch_graph(), with_rules(V2, CONT1), unbatched, memory)).rules
    assert (rule.reused, rule.judged, unbatched.calls) == (5, 0, [])
    other_batch = with_rules(V2, dataclasses.replace(CONT1, batch=3, batch_prompt="judge_attach_orphan_batch_next"))
    rebatched = scripted({})  # another batch prompt: the single inputs did not change
    (rule,) = (await run_infer(batch_graph(), other_batch, rebatched, memory)).rules
    assert (rule.reused, rebatched.calls) == (5, [])


async def test_a_batch_respects_the_places_an_update_reserves_for_the_queue(tmp_path: Path) -> None:
    """Update, cap 2: fr-1 and fr-3 were judged before with other inputs (changed), fr-5 is queued, fr-2 and fr-4 never
    judged.
    """

    memory = inference.JudgeMemory(FileExtractionStore(tmp_path), "p-1", MODEL)
    rule = dataclasses.replace(CONT1, batch=3, cap=2)
    await memory.remember(rule, {memory.seen_key(rule, spec(uuid)): {"digest": "old"} for uuid in ("fr-1", "fr-3")})
    gateway = scripted(single_answers() | {(CONT1.prompt, "Requirement 1"): attach_to(None)})
    queue = {CONT1.id: BacklogQueue(spec_ids=(spec("fr-5"),), since_build="b-0", age_builds=1)}
    report = await inference.infer(
        cast(Executor, batch_graph()),
        "p-1",
        with_rules(V2, rule),
        JudgeRunner(gateway, V2),
        stamp=STAMP,
        memory=memory,
        changes=inference.BuildChanges(update=inference.UpdateScope(), backlog=queue),
    )
    assert gateway.calls_for(BATCH_PROMPT) == []
    assert [call.key for call in gateway.calls_for(CONT1.prompt)] == ["Requirement 1", "Requirement 5"]
    assert (report.rules[0].judged, report.rules[0].limit) == (2, "cap")
    assert report.backlog is not None
    assert report.backlog[CONT1.id].spec_ids == ("X::FR-3", "X::FR-2", "X::FR-4")


async def test_a_batch_is_cut_at_the_cap_and_counted_as_one_call() -> None:
    capped = with_rules(V2, dataclasses.replace(CONT1, batch=3, cap=2))
    gateway = scripted({})
    gateway.handle(BATCH_PROMPT, lambda _request: batch_answers(batch_answer("S1", None), batch_answer("S2", None)))
    report = await run_infer(batch_graph(), capped, gateway)
    (rule,) = report.rules
    (payload,) = batch_payloads(gateway)
    assert [item["name"] for item in payload["subjects"]] == ["Requirement 1", "Requirement 2"]
    assert (rule.judged, rule.deferred, rule.limit, rule.backlog) == (2, 3, "cap", 3)
    assert report.backlog is not None and report.backlog[CONT1.id].spec_ids == ("X::FR-3", "X::FR-4", "X::FR-5")
    assert gateway.calls_for(CONT1.prompt) == []


# Judged items of every template, sampling


@pytest.mark.parametrize(
    ("answer", "outcome", "target"),
    [
        (attach_to("C1", "f-fr-1", "Customer exports orders"), "accepted", "X::FEAT-1"),
        (attach_to(None), "declined", None),
        (attach_to("C1", "f-fr-1", "not in the fragment at all"), "quote_missing", "X::FEAT-1"),
        ({**attach_to("C1", "f-fr-1", "Customer exports orders"), "confidence": 0.6}, "below_floor", "X::FEAT-1"),
        (attach_to("C7", "f-fr-1", "Customer exports orders"), "invalid", None),
        # the external module is never shown (its owner pair needs external: no), so C2 names nothing
        (attach_to("C2", "f-fr-1", "Customer exports orders"), "invalid", None),
    ],
    ids=["accepted", "declined", "quote_missing", "below_floor", "unknown_id", "forbidden_target"],
)
async def test_an_attach_orphan_verdict_gives_one_owner_item_with_its_confidence(
    answer: dict[str, Any], outcome: str, target: str | None
) -> None:
    graph = FakeGraph()
    graph.node("feat-1", "Feature").node("mod-ext", "Module", "Payment provider", external="yes", type="backend")
    graph.node("fr-1", "FunctionalRequirement", "Customer exports orders", text="Customer exports orders monthly.")
    graph.similar["fr-1"] = ["feat-1", "mod-ext"]
    report = await run_infer(
        graph, with_rules(V2, CONT1), scripted({(CONT1.prompt, "Customer exports orders"): answer}), write=False
    )
    (item,) = report.judged_items or ()
    assert (item.rule, item.kind, item.subject, item.source_spec_id, item.relation) == (
        CONT1.id,
        "owner",
        "X::FR-1",
        "X::FR-1",
        "PART_OF",
    )
    assert (item.target_spec_id, item.outcome, item.confidence) == (target, outcome, answer["confidence"])
    assert item.evidence == tuple((e["fragment_id"], e["quote"]) for e in answer["evidence"])


@pytest.mark.parametrize(
    ("cited", "quote", "outcome", "evidence"),
    [
        ("f-fr-1>>>", "Customer exports orders", "accepted", ("f-fr-1", "Customer exports orders")),  # garbled id
        ("f-fr-1", "the order export feature", "accepted", ("f-feat-1", "the order export feature")),  # owner's
        ("f-fr-1", "Unrelated passage", "quote_missing", ("f-fr-1", "Unrelated passage")),  # shown, not theirs
    ],
    ids=["garbled_id", "owner_fragment", "other_fragment"],
)
async def test_an_attach_orphan_quote_is_cited_from_the_subject_or_owner_fragment_that_holds_it(
    cited: str, quote: str, outcome: str, evidence: tuple[str, str]
) -> None:
    """a quote its cited fragment does not hold counts when a shown fragment of the subject or
    of the chosen candidate holds it, and the edge cites that fragment; any other quote still fails."""

    graph = FakeGraph()
    graph.node("feat-1", "Feature", "Order export", text="Order export: the order export feature.")
    graph.node("feat-2", "Feature", "Reports", text="Reports. Unrelated passage.")
    graph.node("fr-1", "FunctionalRequirement", "Customer exports orders", text="Customer exports orders monthly.")
    graph.similar["fr-1"] = ["feat-1", "feat-2"]
    answer = attach_to("C1", cited, quote)
    report = await run_infer(
        graph, with_rules(V2, CONT1), scripted({(CONT1.prompt, "Customer exports orders"): answer}), write=False
    )
    (item,) = report.judged_items or ()
    assert (item.target_spec_id, item.outcome, item.evidence) == ("X::FEAT-1", outcome, (evidence,))
    assert report.inferred_added == (("X::FR-1|PART_OF|X::FEAT-1",) if outcome == "accepted" else ())


async def test_a_realization_verdict_gives_one_link_item_per_edge() -> None:
    graph = reads_graph()
    graph.node("mod-2", "Module", "Mail service", type="backend").edge("RELATES_TO", "fr-1", "mod-2")
    answers = reads_answers()
    answers[(CAP2.prompt, "Order export")] = {**answers[(CAP2.prompt, "Order export")], "targets": ["C1", "C2"]}
    report = await run_infer(graph, with_rules(V2, CONT1, CAP2), scripted(answers))
    items = [
        (i.rule, i.kind, i.source_spec_id, i.target_spec_id, i.outcome, i.read_edges) for i in report.judged_items or ()
    ]
    assert items == [  # report order: rule, subject, source, relation, target
        # mod-1 is reached only over the INF-CONT-01 edge this build plans; mod-2 over a stated edge
        (CAP2.id, "link", "X::CAP-1", "X::MOD-1", "accepted", ("X::FR-1|PART_OF|X::FEAT-1",)),
        (CAP2.id, "link", "X::CAP-1", "X::MOD-2", "accepted", ()),
        (CONT1.id, "owner", "X::FR-1", "X::FEAT-1", "accepted", None),  # a rule without reads
    ]
    assert all(item.evidence is None for item in report.judged_items or ())  # a build keeps no quotes


async def test_an_endpoints_verdict_gives_one_link_item_per_link() -> None:
    graph = (
        FakeGraph()
        .node("mod-a", "Module", "Checkout", type="backend")
        .node("mod-b", "Module", "Orders", type="backend")
    )
    graph.node("int-1", "IntegrationRequirement", "Checkout hands orders over", "Checkout hands orders to Orders.")
    graph.endpoint_rows = [{"uuid": "int-1", "spec_id": spec("int-1"), "targets": ["mod-a", "mod-b"]}]
    answer = {
        "confidence": 0.8,
        "evidence": [{"fragment_id": "f-int-1", "quote": "Checkout hands orders to Orders"}],
        "links": [{"source": "T1", "target": "T2"}],
    }
    report = await run_infer(
        graph, with_rules(V2, INT2), scripted({(INT2.prompt, "Checkout hands orders over"): answer})
    )
    items = [
        (i.kind, i.subject, i.source_spec_id, i.relation, i.target_spec_id, i.outcome)
        for i in report.judged_items or ()
    ]
    assert items == [("link", "X::INT-1", "X::MOD-A", INT2.writes, "X::MOD-B", "accepted")]


async def test_a_sample_judges_a_seeded_subset_of_a_rules_subjects_and_defers_the_rest() -> None:
    sample = inference.SubjectSample({CONT1.id: 2}, seed=7)
    picked = sample.pick(CONT1.id, [f"X::FR-{index}" for index in range(1, 6)])
    assert picked is not None and len(picked) == 2
    assert sample.pick(CONT1.id, [f"X::FR-{index}" for index in (5, 4, 3, 2, 1)]) == picked  # order-free
    assert inference.SubjectSample({CONT1.id: 2}, seed=8).pick(CONT1.id, sorted(picked | {"X::FR-9"})) is not None
    assert (
        sample.pick(CONT2.id, ["X::FR-1"]) is None
        and inference.SubjectSample({CONT1.id: 9}).pick(CONT1.id, ["X::FR-1"]) is None
    )
    gateway = scripted({(CONT1.prompt, f"Requirement {index}"): attach_to(None) for index in range(1, 6)})
    report = await inference.infer(
        cast(Executor, batch_graph()),
        "p-1",
        with_rules(V2, CONT1),
        JudgeRunner(gateway, V2),
        stamp=STAMP,
        changes=inference.BuildChanges(sample=sample),
        write=False,
    )
    (rule,) = report.rules
    assert (rule.subjects, rule.judged, rule.deferred, rule.backlog) == (5, 2, 3, 0)
    assert {item.subject for item in report.judged_items or ()} == picked


class _NoExecutor:
    """An executor any read or write of fails the test."""

    async def read(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("inference read the graph")

    async def write(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("inference wrote the graph")


async def test_a_container_path_mixing_directions_fails_before_reading_or_judging() -> None:
    mixed = dataclasses.replace(
        CAP2, container_paths=(SpinePath(relations=("PART_OF", "RELATES_TO"), sequence=False, min_hops=1, max_hops=2),)
    )
    ontology = with_rules(V2, mixed)
    gateway = ScriptedGateway()
    with pytest.raises(ConformanceError) as raised:
        await inference.infer(
            cast(Executor, _NoExecutor()),
            "p-1",
            ontology,
            JudgeRunner(gateway, ontology),
            stamp=STAMP,
        )
    assert raised.value.code == "inference_container_path_mixed_direction"
    assert gateway.calls == []


def module_graph() -> FakeGraph:
    """Features without a module and three candidate modules (most similar first): an external one,
    a deployment one and a backend one."""

    graph = FakeGraph()
    graph.node("mod-ext", "Module", "Payment provider", external="yes", type="backend")
    graph.node("mod-dep", "Module", "Kubernetes cluster", type="deployment")
    graph.node("mod-ok", "Module", "Shop backend", type="backend")
    for index in (1, 2, 3):
        graph.node(f"feat-{index}", "Feature", f"Feature {index}", text=f"Feature {index} runs in the shop backend.")
        graph.similar[f"feat-{index}"] = ["mod-ext", "mod-dep", "mod-ok"]
    return graph


def cont2_answers(*targets: str) -> dict[tuple[str, str], Any]:
    return {
        (CONT2.prompt, f"Feature {index}"): attach_to(target, f"f-feat-{index}", "runs in the shop backend")
        for index, target in enumerate(targets, 1)
    }


async def test_inf_cont_02_never_shows_external_or_deployment_modules_and_attaches_a_feature() -> None:
    """The feature to module owner pair's ``target_where`` (not external, not a deployment target)
    keeps those modules out of the candidates, so the shop backend is each feature's only one (C1)."""

    graph = module_graph()
    report = await run_infer(graph, V2, scripted(cont2_answers("C1", None, "C2")))  # ok, none, unknown id
    rules = {rule.rule: rule for rule in report.rules}
    cont2 = rules[CONT2.id]
    assert (cont2.subjects, cont2.accepted, cont2.declined, cont2.invalid) == (3, 1, 1, 1)
    assert {tuple(params["excluded"]) for params in graph.reads_of("build_infer_similar_targets_")} == {
        ("mod-dep", "mod-ext")
    }
    assert report.inferred_added == ("X::FEAT-1|PART_OF|X::MOD-OK",)
    assert report.quarantine == ()
    written = [row["ref"] for _, rows in graph.writes for row in rows]
    assert written == ["X::FEAT-1|PART_OF|X::MOD-OK"]


async def test_a_target_that_turns_forbidden_is_no_candidate_and_its_inferred_edges_go(tmp_path: Path) -> None:
    memory = inference.JudgeMemory(FileExtractionStore(tmp_path), "p-1", MODEL)
    graph = module_graph()
    first = await run_infer(graph, with_rules(V2, CONT2), scripted(cont2_answers("C1", "C1", "C1")), memory)
    assert len(first.inferred_added) == 3
    for index in (1, 2, 3):
        graph.edge("PART_OF", f"feat-{index}", "mod-ok", "inferred", rule=CONT2.id)
    graph.nodes["mod-ok"]["external"] = "yes"  # the owner pair forbids it now: no judgement is reused
    graph.writes.clear()
    report = await run_infer(graph, with_rules(V2, CONT2), scripted({}), memory)
    (rule,) = report.rules
    assert (rule.subjects, rule.reused, rule.judged, rule.accepted) == (0, 0, 0, 0)
    assert report.orphans_without_candidate == ("X::FEAT-1", "X::FEAT-2", "X::FEAT-3")
    assert len(report.inferred_removed) == 3 and report.quarantine == ()


async def test_a_later_rule_is_validated_against_the_edges_an_earlier_rule_accepted() -> None:
    graph = FakeGraph()
    graph.node("feat-1", "Feature").node("mod-1", "Module", type="backend")
    graph.node("fr-1", "FunctionalRequirement", "Customer exports orders", text="Customer exports orders monthly.")
    graph.similar["fr-1"] = ["feat-1", "mod-1"]
    second = dataclasses.replace(CONT1, id="INF-CONT-01B")
    answers = iter(("C1", "C2"))  # the first rule run attaches to the feature, the second to the module
    gateway = ScriptedGateway()
    gateway.handle(CONT1.prompt, lambda _request: attach_to(next(answers), "f-fr-1", "Customer exports orders"))
    report = await run_infer(graph, with_rules(V2, CONT1, second), gateway)
    first, later = report.rules
    assert (first.rule, first.accepted, later.rule, later.invalid) == (CONT1.id, 1, second.id, 1)
    assert [(e.source_spec_id, e.target_spec_id) for e in report.quarantine] == [("X::FR-1", "X::MOD-1")]
    assert report.inferred_added == ("X::FR-1|PART_OF|X::FEAT-1",)


class _EscapingOnce:
    """A judge runner from which a non-fatal ``LLMError`` escapes on the first call for one subject."""

    def __init__(self, inner: JudgeRunner, subject: str) -> None:
        self._inner, self._subject, self._raised = inner, subject, False
        self.concurrency = inner.concurrency

    async def judge(self, prompt: str, payload: Any, verdict_type: Any) -> Any:
        if payload["subject"]["name"] == self._subject and not self._raised:
            self._raised = True
            raise LLMError("exhausted")
        return await self._inner.judge(prompt, payload, verdict_type)


async def test_a_failed_rule_leaves_the_validator_as_before_it(tmp_path: Path) -> None:
    """A rule whose reused edge was accepted and then failed: a later rule does not see that edge."""

    memory = inference.JudgeMemory(FileExtractionStore(tmp_path), "p-1", MODEL)
    graph = FakeGraph()
    graph.node("feat-1", "Feature").node("mod-1", "Module", type="backend")
    graph.node("fr-1", "FunctionalRequirement", "Customer exports orders", text="Customer exports orders monthly.")
    graph.similar["fr-1"] = ["feat-1", "mod-1"]
    feature = attach_to("C1", "f-fr-1", "Customer exports orders")
    await run_infer(
        graph, with_rules(V2, CONT1), scripted({(CONT1.prompt, "Customer exports orders"): feature}), memory
    )

    graph.node("fr-2", "FunctionalRequirement", "Staff prints invoices", text="Staff prints invoices daily.")
    graph.similar["fr-2"] = ["feat-1"]
    later = dataclasses.replace(CONT1, id="INF-CONT-01B")
    gateway = scripted(
        {
            (CONT1.prompt, "Customer exports orders"): attach_to("C2", "f-fr-1", "Customer exports orders"),
            (CONT1.prompt, "Staff prints invoices"): attach_to(None),
        }
    )
    ontology = with_rules(V2, CONT1, later)
    report = await inference.infer(
        cast(Executor, graph),
        "p-1",
        ontology,
        cast(JudgeRunner, _EscapingOnce(JudgeRunner(gateway, ontology), "Staff prints invoices")),
        stamp=STAMP,
        memory=memory,
    )
    failed, judged = report.rules
    assert (failed.rule, failed.error_code) == (CONT1.id, "llm_exhausted")
    assert (judged.rule, judged.accepted, judged.declined, judged.invalid) == (later.id, 1, 1, 0)
    assert report.quarantine == ()
    assert report.inferred_added == ("X::FR-1|PART_OF|X::MOD-1",)


# Cross-document links

XDOC = V2_RULES["INF-XDOC-01"]
XDOC_TEXTS = {
    "fr-a": "A refund is accepted only after the order is cancelled.",
    "fr-b": "Paid orders can be cancelled within 24 hours.",
    "fr-c": "Refunds: requests over 500 EUR are rejected.",
    "fr-d": "Refund requests are logged for audit.",
    "fr-e": "Refunds are paid back to the card.",
}
XDOC_NAMES = {
    "fr-a": "Refund request",
    "fr-b": "Order cancel",
    "fr-c": "Refund limit",
    "fr-d": "Refund log",
    "fr-e": "Card payback",
}
XDOC_DOCUMENTS = {"fr-a": ["d1"], "fr-b": ["d2"], "fr-c": ["d2"], "fr-d": ["d1"], "fr-e": ["d2"]}


def hub_row(hub: str, members: dict[str, list[str]]) -> dict[str, Any]:
    return {"uuid": hub, "spec_id": spec(hub), "members": [{"uuid": u, "documents": d} for u, d in members.items()]}


def xdoc_graph(*, hub_attributes: dict[str, str] | None = None) -> FakeGraph:
    """The hub "Orders service" (a module) with five requirement members from two documents: d1 holds fr-a and fr-d, d2
    holds fr-b, fr-c and fr-e; fr-d and fr-e are part of the feature "Refunds", the others are orphans.
    """

    graph = FakeGraph()
    graph.node("mod-hub", "Module", "Orders service", "The Orders service keeps every order.", **(hub_attributes or {}))
    graph.node("feat-x", "Feature", "Refunds", "Refunds: the refund feature covers every refund.")
    for uuid, text in XDOC_TEXTS.items():
        graph.node(uuid, "FunctionalRequirement", XDOC_NAMES[uuid], text)
    graph.edge("PART_OF", "fr-d", "feat-x").edge("PART_OF", "fr-e", "feat-x")
    graph.hub_rows = [hub_row("mod-hub", XDOC_DOCUMENTS)]
    graph.embeddings = {uuid: [1.0, 0.0] for uuid in XDOC_TEXTS}
    graph.titles = {"d1": "Refunds spec", "d2": "Orders spec"}
    return graph


def quote(uuid: str, text: str) -> dict[str, str]:
    return {"fragment_id": f"f-{uuid}", "quote": text}


def xdoc_answer(links: list[dict[str, Any]] | None = None, owners: list[dict[str, Any]] | None = None) -> dict:
    return {"confidence": 0.9, "evidence": [], "links": links or [], "owners": owners or []}


def link(a: str, b: str, relation: str, *quotes: dict[str, str], source: str = "", confidence: float = 0.9) -> dict:
    return {
        "a": a,
        "b": b,
        "relation": relation,
        "source": source or a,
        "confidence": confidence,
        "evidence": list(quotes),
    }


def xdoc_payload(gateway: ScriptedGateway) -> dict[str, Any]:
    (call,) = gateway.calls_for(XDOC.prompt)
    return cast(dict[str, Any], json.loads(dict(untrusted_blocks(call.messages[-1]["content"]))["payload"]))


def test_the_cross_document_verdict_judges_each_item_on_its_own() -> None:
    verdict = CrossDocumentVerdict.model_validate(
        xdoc_answer(
            [link("M1", "M2", "RELATES_TO", quote("fr-a", "x"))],
            [{"member": "M3", "owner": "O1", "confidence": 1, "evidence": []}],
        )
    )
    assert (verdict.links[0].source, verdict.owners[0].owner) == ("M1", "O1")
    for bad in (
        {"confidence": 0.9, "evidence": [], "links": []},  # owners are required
        {**xdoc_answer(), "links": [{"a": "M1"}]},
        xdoc_answer([link("M1", "M2", "RELATES_TO", confidence=1.2)]),
    ):
        with pytest.raises(ValidationError):
            CrossDocumentVerdict.model_validate(bad)
    schema = CrossDocumentVerdict.model_json_schema()
    assert {"confidence", "evidence", "links", "owners"} <= set(schema["required"])


async def test_xdoc_subjects_have_members_from_two_documents_with_a_pair_sharing_none() -> None:
    graph = FakeGraph()
    for hub in ("hub-1", "hub-2", "hub-3", "hub-4"):
        graph.node(hub, "Module", type="backend")
    for uuid in ("r-1", "r-2", "r-3"):
        graph.node(uuid, "FunctionalRequirement")
    graph.node("r-out", "FunctionalRequirement", in_scope="no")
    graph.hub_rows = [
        hub_row("hub-1", {"r-1": ["d1"], "r-2": ["d1"]}),  # one document
        hub_row("hub-2", {"r-1": ["d1"], "r-3": ["d1", "d2"]}),  # two documents, but no pair apart
        hub_row("hub-3", {"r-1": ["d1"], "r-2": ["d1"], "r-out": ["d2"], "r-3": ["d2"]}),
        hub_row("hub-4", {"r-1": ["d1"], "r-out": ["d2"]}),  # out of scope: one document left
    ]
    selection = await inference._select(cast(Executor, graph), "p-1", V2, XDOC)
    assert [(subject.spec_id, subject.targets) for subject in selection.subjects] == [
        ("X::HUB-3", ("r-1", "r-2", "r-3"))
    ]
    (read,) = graph.reads_of("build_infer_xdoc_members")
    assert (read["via"], read["mentions"], read["subject_labels"]) == (
        ["PART_OF", "RELATES_TO"],
        True,
        ["DataEntity", "Feature", "Interface", "Module"],
    )
    assert "FunctionalRequirement" in read["member_labels"] and "Module" not in read["member_labels"]
    assert {name for name, _ in graph.reads if name.startswith("build_read_current_edges_")} == {
        f"build_read_current_edges_{relation}" for relation in XDOC.written
    }


async def test_xdoc_a_member_by_shared_evidence_needs_its_own_text_to_name_the_hub() -> None:
    """A requirement grounded in a fragment the hub is grounded in too is a member only when its name, quote or
    statement in that fragment names the hub (its name or a surface alias, in hub form: plural, hyphens aside); one that
    only shares the fragment is left out (long Q&A fragments ground many hubs).
    """

    def shared(uuid: str, documents: list[str], **texts: Any) -> dict[str, Any]:
        return {"uuid": uuid, "documents": documents, "shared": ["f-hub"], **texts}

    row = {
        "uuid": "hub",
        "spec_id": spec("hub"),
        "name": "Orders service",
        "keys": ["module|surface|order service", "module|surface|order desk"],
        "members": [
            {"uuid": "by-edge", "documents": ["d1"]},
            shared("by-name", ["d2"], name="Order-services keep paid orders"),
            shared("by-quote", ["d2"], name="Paid orders", quote="Paid orders stay in the orders service."),
            shared(
                "by-statement",
                ["d2"],
                name="Paid orders",
                statements=json.dumps(
                    [
                        {"fragment_id": "f-other", "text": "Nothing here."},
                        {"fragment_id": "f-hub", "text": "The order desk keeps them."},
                    ]
                ),
            ),
            shared(
                "elsewhere",
                ["d2"],
                name="Paid orders",
                quote="Refunds are paid back.",
                statements=json.dumps([{"fragment_id": "f-other", "text": "The Orders service keeps them."}]),
            ),
            shared("topic", ["d2"], name="Refund limit", quote="Requests over 500 EUR are rejected."),
        ],
    }
    graph = FakeGraph(hub_rows=[row])
    (hub,) = await infer_xdoc.read_hubs(cast(Executor, graph), "p-1", XDOC, bases=["stated"], statuses=["active"])
    assert sorted(hub.members) == ["by-edge", "by-name", "by-quote", "by-statement"]
    (read,) = graph.reads_of("build_infer_xdoc_members")
    assert read["quote"] == "quote"
    lone = {**row, "members": [row["members"][0], row["members"][-1]]}
    assert (
        await infer_xdoc.read_hubs(cast(Executor, FakeGraph(hub_rows=[lone])), "p-1", XDOC, bases=[], statuses=[]) == []
    )


async def test_xdoc_pairs_skip_one_owner_counting_owners_accepted_earlier_in_the_build() -> None:
    graph = xdoc_graph(hub_attributes={"type": "backend"})
    graph.similar["fr-c"] = ["feat-x"]  # INF-CONT-01 attaches the orphan fr-c to "Refunds" first
    gateway = scripted(
        {
            (CONT1.prompt, "Refund limit"): attach_to("C1", "f-fr-c", "requests over 500 EUR are rejected"),
            (XDOC.prompt, "Orders service"): xdoc_answer(),
        }
    )
    report = await run_infer(graph, with_rules(V2, CONT1, XDOC), gateway)
    payload = xdoc_payload(gateway)
    names = {member["id"]: member["name"] for member in payload["members"]}
    assert [sorted((names[a], names[b])) for a, b in payload["pairs"]] == [
        ["Order cancel", "Refund request"],
        ["Refund limit", "Refund request"],
        ["Card payback", "Refund request"],
        ["Order cancel", "Refund log"],
    ]  # fr-c and fr-d, fr-d and fr-e: one owner; fr-a and fr-d: one document
    members = {member["name"]: member for member in payload["members"]}
    assert (members["Refund limit"]["owner"], members["Refund request"]["owner"]) == ("Refunds", None)
    assert (members["Refund request"]["document"], members["Order cancel"]["document"]) == (
        "Refunds spec",
        "Orders spec",
    )
    candidates = {names[o["member"]]: [c["name"] for c in o["candidates"]] for o in payload["owners"]}
    assert candidates == {
        "Refund request": ["Refunds", "Orders service"],
        "Order cancel": ["Refunds", "Orders service"],
    }
    assert payload["relations"] == ["RELATES_TO", "DEPENDS_ON"]
    assert members["Refund request"]["fragments"] == ["f-fr-a"]
    prompt = V2.judge_prompt(XDOC.prompt)
    shown = (*payload, *payload["members"][0], *payload["owners"][0], *payload["owners"][0]["candidates"][0])
    for key in (*shown, "links", "a", "b", "source", "member", "owner", "confidence", "evidence"):
        assert f"`{key}`" in prompt, key
    rule = {r.rule: r for r in report.rules}[XDOC.id]
    assert (rule.subjects, rule.judged, rule.declined, rule.items_by_outcome) == (1, 1, 1, {})
    assert [item for item in report.judged_items or () if item.rule == XDOC.id] == []  # a declined hub: no item


def xdoc_items_answer() -> dict[str, Any]:
    """M1..M5 = fr-a..fr-e; O1 = "Refunds", O2 = the hub (fr-a's candidates come first)."""

    a, b, c = (
        quote("fr-a", "accepted only after the order is cancelled"),
        quote("fr-b", "Paid orders can be cancelled"),
        quote("fr-c", "requests over 500 EUR are rejected"),
    )
    d = quote("fr-d", "Refund requests are logged")
    return xdoc_answer(
        links=[
            link("M1", "M2", "DEPENDS_ON", a, b),  # a cue in the source's quote: kept
            link("M1", "M3", "DEPENDS_ON", quote("fr-a", "A refund is accepted"), c),  # no cue: downgraded
            link("M1", "M5", "RELATES_TO", a),  # one side quoted only
            link("M2", "M4", "RELATES_TO", b, d),  # one owner once fr-b is part of "Refunds"
            link("M3", "M4", "RELATES_TO", c, d, confidence=0.5),  # below the floor
            link("M1", "M4", "RELATES_TO", a, d),  # not a given pair (one document)
        ],
        owners=[
            {"member": "M2", "owner": "O1", "confidence": 0.9, "evidence": [b, quote("feat-x", "the refund feature")]},
            {"member": "M3", "owner": "O2", "confidence": 0.9, "evidence": [c, quote("mod-hub", "keeps every order")]},
        ],
    )


async def test_the_xdoc_rule_floor_binds_link_items_only_and_other_rules_keep_the_ontology_floor() -> None:
    """INF-XDOC-01 has its own ``floor`` 0.85 for links: a link at 0.8 is ``below_floor``
    and passes once the rule has no floor of its own; an owner at 0.8 keeps the ontology's 0.7 and is
    accepted either way, as an endpoints verdict at 0.8 is."""

    assert (V2.inference.floor_of(XDOC), V2.inference.floor_of(INT)) == (0.85, 0.7)
    assert evaluate(INT, ENDPOINT_SUBJECT, endpoints(("T1", "T2"), confidence=0.8))[0] == "accepted"
    a, b = (quote("fr-a", "accepted only after the order is cancelled"), quote("fr-b", "Paid orders can be cancelled"))
    c = quote("fr-c", "requests over 500 EUR are rejected")
    owner = {"member": "M3", "owner": "O2", "confidence": 0.8, "evidence": [c, quote("mod-hub", "keeps every order")]}
    answer = xdoc_answer(links=[link("M1", "M2", "DEPENDS_ON", a, b, confidence=0.8)], owners=[owner])
    outcomes = []
    for rule in (XDOC, dataclasses.replace(XDOC, floor=None)):
        gateway = scripted({(rule.prompt, "Orders service"): answer})
        report = await run_infer(xdoc_graph(), with_rules(V2, rule), gateway)
        outcomes.append(sorted((item.kind, item.outcome) for item in report.judged_items or ()))
    assert outcomes == [
        [("link", "below_floor"), ("owner", "accepted")],
        [("link", "accepted"), ("owner", "accepted")],
    ]


async def test_xdoc_items_are_judged_one_by_one_and_written_over_every_relation_the_rule_writes() -> None:
    graph = xdoc_graph(hub_attributes={"type": "backend", "external": "yes"})
    gateway = scripted({(XDOC.prompt, "Orders service"): xdoc_items_answer()})
    report = await run_infer(graph, with_rules(V2, XDOC), gateway)
    (rule,) = report.rules
    assert (rule.judged, rule.accepted, rule.edges_added) == (1, 1, 3)
    assert rule.items_by_outcome == {
        "accepted": 2,
        "below_floor": 1,
        "downgraded": 1,
        "invalid": 3,
        "quote_missing": 1,
    }
    assert report.inferred_added == (
        "X::FR-A|DEPENDS_ON|X::FR-B",
        "X::FR-A|RELATES_TO|X::FR-C",
        "X::FR-B|PART_OF|X::FEAT-X",
    )
    writes = {name: [row["ref"] for row in rows] for name, rows in graph.writes}
    assert writes == {
        "build_infer_write_edges_RELATES_TO": ["X::FR-A|RELATES_TO|X::FR-C"],
        "build_infer_write_edges_DEPENDS_ON": ["X::FR-A|DEPENDS_ON|X::FR-B"],
        "build_infer_write_edges_PART_OF": ["X::FR-B|PART_OF|X::FEAT-X"],
    }
    rows = {row["ref"]: row for _, written in graph.writes for row in written}
    depends = rows["X::FR-A|DEPENDS_ON|X::FR-B"]["props"]
    assert depends["inference_rule"] == XDOC.id and depends["source_fragment_ids"] == ["f-fr-a", "f-fr-b"]
    assert [item["fragment_id"] for item in json.loads(depends["evidence"])] == ["f-fr-a", "f-fr-b"]
    assert [(e.source_spec_id, e.relation, e.target_spec_id) for e in report.quarantine] == [
        ("X::FR-B", "RELATES_TO", "X::FR-D"),
        ("X::FR-C", "PART_OF", "X::MOD-HUB"),
    ]
    items = report.judged_items
    assert items is not None
    assert [(i.kind, i.source_spec_id, i.relation, i.target_spec_id, i.outcome) for i in items] == [
        ("link", "X::FR-A", "DEPENDS_ON", "X::FR-B", "accepted"),
        ("link", "X::FR-A", "RELATES_TO", "X::FR-C", "downgraded"),
        ("link", "X::FR-A", "RELATES_TO", "X::FR-D", "invalid"),
        ("link", "X::FR-A", "RELATES_TO", "X::FR-E", "quote_missing"),
        ("owner", "X::FR-B", "PART_OF", "X::FEAT-X", "accepted"),
        ("link", "X::FR-B", "RELATES_TO", "X::FR-D", "invalid"),
        ("owner", "X::FR-C", "PART_OF", "X::MOD-HUB", "invalid"),
        ("link", "X::FR-C", "RELATES_TO", "X::FR-D", "below_floor"),
    ]
    assert {item.subject for item in items} == {"X::MOD-HUB"} and items[0].fragment_ids == ("f-fr-a", "f-fr-b")
    # a build's report holds no document text: no quotes
    assert {item.evidence for item in items} == {None}


async def test_a_dry_run_report_keeps_each_judged_items_quotes() -> None:
    graph = xdoc_graph(hub_attributes={"type": "backend", "external": "yes"})
    gateway = scripted({(XDOC.prompt, "Orders service"): xdoc_items_answer()})
    report = await run_infer(graph, with_rules(V2, XDOC), gateway, write=False)
    assert graph.writes == []
    items = {(i.kind, i.source_spec_id, i.target_spec_id): i for i in report.judged_items or ()}
    assert items[("link", "X::FR-A", "X::FR-B")].evidence == (
        ("f-fr-a", "accepted only after the order is cancelled"),
        ("f-fr-b", "Paid orders can be cancelled"),
    )
    assert items[("owner", "X::FR-B", "X::FEAT-X")].evidence == (
        ("f-fr-b", "Paid orders can be cancelled"),
        ("f-feat-x", "the refund feature"),
    )
    assert all(item.evidence for item in items.values())


async def test_a_downgraded_dependency_stored_the_other_way_round_lists_the_stored_source_side_first() -> None:
    # fr-c waits for fr-a without a cue: RELATES_TO (symmetric) is stored from fr-a, the lower identity key
    graph = xdoc_graph()
    a, c = (quote("fr-a", "A refund is accepted"), quote("fr-c", "requests over 500 EUR are rejected"))
    answer = xdoc_answer(links=[link("M1", "M3", "DEPENDS_ON", c, a, source="M3")])
    report = await run_infer(graph, with_rules(V2, XDOC), scripted({(XDOC.prompt, "Orders service"): answer}))
    assert report.inferred_added == ("X::FR-A|RELATES_TO|X::FR-C",)
    (item,) = report.judged_items or ()
    assert (item.source_spec_id, item.relation, item.target_spec_id, item.outcome) == (
        "X::FR-A",
        "RELATES_TO",
        "X::FR-C",
        "downgraded",
    )
    assert item.fragment_ids == ("f-fr-a", "f-fr-c")
    (row,) = [row for _, written in graph.writes for row in written]
    assert row["props"]["source_fragment_ids"] == ["f-fr-a", "f-fr-c"]
    assert [quoted["fragment_id"] for quoted in json.loads(row["props"]["evidence"])] == ["f-fr-a", "f-fr-c"]


def test_the_dependency_cue_matches_whole_words_in_the_source_quote_only() -> None:
    assert V2.cue_matches("DEPENDS_ON", "accepted only after the order is cancelled")
    assert not V2.cue_matches("DEPENDS_ON", "each requirement is listed")
    assert not V2.cue_matches("RELATES_TO", "requires the order")


def test_the_validator_lets_only_a_cue_gated_rule_infer_a_stated_only_relation() -> None:
    view = InMemoryGraphView([ViewNode("a", "FunctionalRequirement"), ViewNode("b", "FunctionalRequirement")], [], V2)
    checker = ProposalChecker(view, V2)
    assert checker.check(ViewEdge("DEPENDS_ON", "a", "b", "inferred", XDOC.id)) == []
    for edge in (
        ViewEdge("DEPENDS_ON", "a", "b", "inferred", CONT1.id),
        ViewEdge("DEPENDS_ON", "a", "b", "inferred"),
        ViewEdge("DEPENDS_ON", "a", "b", "implied", XDOC.id),
    ):
        assert [found.check for found in checker.check(edge)] == ["stated_only"], edge


async def test_a_reused_xdoc_judgement_is_checked_again_edge_by_edge(tmp_path: Path) -> None:
    memory = inference.JudgeMemory(FileExtractionStore(tmp_path), "p-1", MODEL)
    graph = xdoc_graph(hub_attributes={"type": "backend", "external": "yes"})
    gateway = scripted({(XDOC.prompt, "Orders service"): xdoc_items_answer()})
    await run_infer(graph, with_rules(V2, XDOC), gateway, memory)
    graph.writes.clear()
    report = await run_infer(graph, with_rules(V2, XDOC), scripted({}), memory)
    (rule,) = report.rules
    assert (rule.reused, rule.judged, rule.items_by_outcome) == (1, 0, {})
    assert report.inferred_added == (
        "X::FR-A|DEPENDS_ON|X::FR-B",
        "X::FR-A|RELATES_TO|X::FR-C",
        "X::FR-B|PART_OF|X::FEAT-X",
    )
    assert [e.relation for e in report.quarantine] == ["RELATES_TO", "PART_OF"]


def with_billing_hub(graph: FakeGraph, members: dict[str, list[str]]) -> FakeGraph:
    """A second hub, the module "Billing service", with ``members``; fr-f (d1) and fr-g (d2) are new
    orphan requirements of their own."""

    graph.node("mod-two", "Module", "Billing service", type="backend")
    for uuid, text in (("fr-f", "Invoices are sent monthly."), ("fr-g", "Invoices list each refund.")):
        graph.node(uuid, "FunctionalRequirement", uuid, text)
        graph.embeddings[uuid] = [1.0, 0.0]
    graph.hub_rows.append(hub_row("mod-two", members))
    return graph


async def test_the_xdoc_rule_cap_defers_the_subjects_past_it() -> None:
    graph = with_billing_hub(xdoc_graph(hub_attributes={"type": "backend"}), {"fr-f": ["d1"], "fr-g": ["d2"]})
    gateway = scripted({(XDOC.prompt, "Orders service"): xdoc_answer()})
    (rule,) = (await run_infer(graph, with_rules(V2, dataclasses.replace(XDOC, cap=1)), gateway)).rules
    assert XDOC.cap == 36 and V2.inference.cap_of(XDOC) == 36
    assert (rule.subjects, rule.judged, rule.deferred, rule.duplicate_pairs) == (2, 1, 1, 0)
    # most members first: "Orders service" (5) before "Billing service" (2)
    assert [call.key for call in gateway.calls_for(XDOC.prompt)] == ["Orders service"]


async def test_a_member_pair_is_judged_under_one_hub_only() -> None:
    """Two hubs sharing members: the one with more members chooses first and keeps its pairs; the other
    is shown only the pairs left, and a hub left with none is no subject (one sf pair was
    accepted under up to 4 hubs)."""

    members = {"fr-a": ["d1"], "fr-b": ["d2"], "fr-c": ["d2"], "fr-f": ["d1"]}
    graph = with_billing_hub(xdoc_graph(hub_attributes={"type": "backend"}), members)
    gateway = scripted(
        {(XDOC.prompt, "Orders service"): xdoc_answer(), (XDOC.prompt, "Billing service"): xdoc_answer()}
    )
    (rule,) = (await run_infer(graph, with_rules(V2, XDOC), gateway)).rules
    assert (rule.subjects, rule.judged, rule.duplicate_pairs) == (2, 2, 2)
    payloads = {}
    for call in gateway.calls_for(XDOC.prompt):
        payload = json.loads(dict(untrusted_blocks(call.messages[-1]["content"]))["payload"])
        names = {member["id"]: member["name"] for member in payload["members"]}
        payloads[call.key] = sorted(sorted((names[a], names[b])) for a, b in payload["pairs"])
    assert payloads["Billing service"] == [["Order cancel", "fr-f"], ["Refund limit", "fr-f"]]
    shown = {tuple(pair) for pair in payloads["Orders service"]}
    assert {("Order cancel", "Refund request"), ("Refund limit", "Refund request")} <= shown
    # the same members under two hubs: the second has nothing left
    same = with_billing_hub(xdoc_graph(hub_attributes={"type": "backend"}), dict(XDOC_DOCUMENTS))
    (rule,) = (
        await run_infer(same, with_rules(V2, XDOC), scripted({(XDOC.prompt, "Orders service"): xdoc_answer()}))
    ).rules
    assert (rule.subjects, rule.judged, rule.duplicate_pairs) == (1, 1, 5)


def xdoc_old_edge(relation: str, source: str, target: str) -> inference._CurrentEdge:
    key = f"{source}|{relation}|{target}"
    return inference._CurrentEdge(f"e|{key}", key, source, target, "inferred", XDOC.id, key, relation)


def xdoc_swap(subjects: tuple[_Subject, ...], judged: set[str], current: list[inference._CurrentEdge]) -> set[str]:
    """The keys of the old inferred edges the swap of an INF-XDOC-01 plan deletes (no item accepted)."""

    plan = inference._RulePlan(
        XDOC, RuleReport(rule=XDOC.id), subjects, frozenset(judged), {}, tuple(current), stale=frozenset()
    )
    swaps = inference._swap_rows(plan, project_id="p-1", build_id="b-1", at="t", judge_model=MODEL)
    return {row["edge_key"] for swap in swaps.values() for row in swap.deletes}


def test_a_deferred_hub_keeps_only_edges_between_its_members_or_to_its_owner_candidates() -> None:
    """H1 (members a, b) is judged again and accepts nothing; H2 (members a, c; owner candidate mod-2) is deferred."""

    h1 = _Subject("h1", "X::H1", targets=("a", "b"))
    h2 = _Subject("h2", "X::H2", targets=("a", "c"), candidates=("mod-2",), via={"c": ("mod-2",)})
    current = [
        xdoc_old_edge("RELATES_TO", "a", "b"),
        xdoc_old_edge("RELATES_TO", "a", "c"),
        xdoc_old_edge("PART_OF", "c", "mod-2"),
        xdoc_old_edge("PART_OF", "c", "mod-x"),
        xdoc_old_edge("PART_OF", "mod-2", "c"),
        xdoc_old_edge("RELATES_TO", "c", "x"),
    ]
    deleted = xdoc_swap((h1, h2), {"h1"}, current)
    assert deleted == {"a|RELATES_TO|b", "c|PART_OF|mod-x", "mod-2|PART_OF|c", "c|RELATES_TO|x"}


def test_a_hub_no_longer_a_subject_loses_its_edges_beside_a_deferred_hub_sharing_one_end() -> None:
    """Hub H0 (members a, z) no longer qualifies; H2 (members a, c) is deferred: a-z goes."""

    h2 = _Subject("h2", "X::H2", targets=("a", "c"))
    current = [xdoc_old_edge("RELATES_TO", "a", "z"), xdoc_old_edge("RELATES_TO", "a", "c")]
    assert xdoc_swap((h2,), set(), current) == {"a|RELATES_TO|z"}


def test_xdoc_pair_cosines_are_on_the_neo4j_scale_per_hub() -> None:
    """(1 + cos) / 2 of each pair; an end without an embedding or with a zero one gives no score."""

    embeddings = {
        "a": np.array([1.0, 0.0]),
        "b": np.array([3.0, 4.0]),
        "c": np.array([-2.0, 0.0]),
        "z": np.array([0.0, 0.0]),
    }
    scores = infer_xdoc.pair_cosines([("a", "b"), ("a", "c"), ("b", "c"), ("a", "z"), ("a", "n")], embeddings)
    assert scores == pytest.approx({frozenset(("a", "b")): 0.8, frozenset(("a", "c")): 0.0, frozenset(("b", "c")): 0.2})
    assert infer_xdoc.pair_cosines([], embeddings) == {}


def test_each_owner_candidate_keeps_one_shown_fragment_past_the_bound() -> None:
    """16 members and 5 owner candidates (the hub last): every owner candidate's first fragment is
    shown; the members' firsts fill the room left, then nothing of their further fragments."""

    def shown(uuid: str, count: int) -> infer_xdoc.Shown:
        return infer_xdoc.Shown(uuid, {}, tuple((f"f-{uuid}-{n}", f"h-{uuid}-{n}") for n in range(count)))

    members = [shown(f"m{index:02}", 2) for index in range(16)]
    owners = [shown(f"o{index}", 1) for index in range(4)] + [shown("hub", 1)]
    pairs = [(members[index].uuid, members[index + 1].uuid) for index in range(0, 16, 2)]
    fragments = infer_xdoc._fragments(members, owners, pairs)
    assert len(fragments) == infer_xdoc.MAX_FRAGMENTS
    assert {f"f-{owner.uuid}-0" for owner in owners} <= set(fragments)
    assert list(fragments)[:15] == [f"f-m{index:02}-0" for index in range(15)]
    assert list(fragments)[15:] == [f"f-{owner.uuid}-0" for owner in owners]

    few = infer_xdoc._fragments(members[:4], owners[:1], pairs[:2])
    assert list(few) == [
        *(f"f-m{index:02}-0" for index in range(4)),
        "f-o0-0",
        *(f"f-m{index:02}-1" for index in range(4)),
    ]


async def test_a_dry_run_selects_and_judges_but_writes_no_edge_and_no_memory(tmp_path: Path) -> None:
    store = FileExtractionStore(tmp_path)
    memory = inference.JudgeMemory(store, "p-1", MODEL)
    graph = xdoc_graph(hub_attributes={"type": "backend", "external": "yes"})
    gateway = scripted({(XDOC.prompt, "Orders service"): xdoc_items_answer()})
    report = await inference.infer(
        cast(Executor, graph),
        "p-1",
        with_rules(V2, XDOC),
        JudgeRunner(gateway, V2),
        stamp=STAMP,
        memory=memory,
        write=False,
    )
    assert graph.writes == [] and len(gateway.calls_for(XDOC.prompt)) == 1
    (rule,) = report.rules
    assert (rule.accepted, rule.edges_added) == (1, 3) and len(report.inferred_added) == 3
    assert stored_files(tmp_path) == []


def stored_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


# Owners of earlier builds (the update's Verify repairs of two owners) and the update scope


async def test_xdoc_counts_the_inferred_owners_of_earlier_builds() -> None:
    """fr-a is part of "Refunds" by an INF-CONT-01 edge of an earlier build: it is shown with that
    owner, gets no owner candidates, and is not paired with fr-e (also of "Refunds")."""

    graph = xdoc_graph(hub_attributes={"type": "backend"})
    graph.edge("PART_OF", "fr-a", "feat-x", "inferred", rule=CONT1.id)
    gateway = scripted({(XDOC.prompt, "Orders service"): xdoc_answer()})
    await run_infer(graph, with_rules(V2, XDOC), gateway)
    payload = xdoc_payload(gateway)
    names = {member["id"]: member["name"] for member in payload["members"]}
    members = {member["name"]: member for member in payload["members"]}
    assert members["Refund request"]["owner"] == "Refunds"
    assert ["Card payback", "Refund request"] not in [sorted((names[a], names[b])) for a, b in payload["pairs"]]
    assert "Refund request" not in {names[owner["member"]] for owner in payload["owners"]}


async def test_an_xdoc_owner_edge_of_an_earlier_build_is_judged_again_only_by_a_hub_that_could_give_it() -> None:
    """fr-b -> the hub (INF-XDOC-01, an earlier build): the hub's payload shows fr-b as an orphan with the hub among its
    candidates, and a deferred hub keeps the edge (not stale).
    """

    graph = xdoc_graph(hub_attributes={"type": "backend"})
    graph.node("feat-bill", "Feature", "Billing")
    graph.edge("PART_OF", "fr-b", "mod-hub", "inferred", rule=XDOC.id)
    graph.edge("PART_OF", "fr-c", "feat-bill", "inferred", rule=XDOC.id)
    gateway = scripted({(XDOC.prompt, "Orders service"): xdoc_answer()})
    await run_infer(graph, with_rules(V2, XDOC), gateway, write=False)
    payload = xdoc_payload(gateway)
    names = {member["id"]: member["name"] for member in payload["members"]}
    members = {member["name"]: member for member in payload["members"]}
    assert (members["Order cancel"]["owner"], members["Refund limit"]["owner"]) == (None, "Billing")
    candidates = {names[o["member"]]: [c["name"] for c in o["candidates"]] for o in payload["owners"]}
    assert "Orders service" in candidates["Order cancel"] and "Refund limit" not in candidates

    deferred = xdoc_graph(hub_attributes={"type": "backend"})
    deferred.node("feat-bill", "Feature", "Billing")
    deferred.edge("PART_OF", "fr-b", "mod-hub", "inferred", rule=XDOC.id)
    deferred.edge("PART_OF", "fr-c", "feat-bill", "inferred", rule=XDOC.id)
    report = await inference.infer(
        cast(Executor, deferred),
        "p-1",
        with_rules(V2, XDOC),
        JudgeRunner(scripted({}), V2),
        stamp=STAMP,
        changes=inference.BuildChanges(update=inference.UpdateScope()),  # no member changed, no queue: deferred
    )
    assert report.inferred_removed == ("X::FR-C|PART_OF|X::FEAT-BILL",)


async def test_an_xdoc_owner_is_refused_when_a_deferred_hub_keeps_the_members_earlier_owner() -> None:
    """fr-b and fr-c are part of "Billing service" by INF-XDOC-01 edges of an earlier build, so "Orders service" (two of
    its members owned by Billing) judges fr-b's owner again.
    """

    members = {"fr-b": ["d2"], "fr-c": ["d2"], "fr-f": ["d1"], "fr-g": ["d2"]}
    graph = with_billing_hub(xdoc_graph(hub_attributes={"type": "backend"}), members)
    graph.edge("PART_OF", "fr-b", "mod-two", "inferred", rule=XDOC.id)
    graph.edge("PART_OF", "fr-c", "mod-two", "inferred", rule=XDOC.id)
    owner = {
        "member": "M2",
        "owner": "O2",
        "confidence": 0.9,
        "evidence": [quote("fr-b", "Paid orders can be cancelled"), quote("mod-hub", "keeps every order")],
    }
    gateway = scripted({(XDOC.prompt, "Orders service"): xdoc_answer(owners=[owner])})
    report = await run_infer(graph, with_rules(V2, dataclasses.replace(XDOC, cap=1)), gateway, write=False)
    (rule,) = report.rules
    assert (rule.judged, rule.deferred, rule.items_by_outcome) == (1, 1, {"invalid": 1})
    owners = {(names.source_spec_id, names.target_spec_id) for names in report.judged_items or ()}
    assert owners == {("X::FR-B", "X::MOD-HUB")}
    assert [(e.source_spec_id, e.target_spec_id) for e in report.quarantine] == [("X::FR-B", "X::MOD-HUB")]
    assert report.inferred_added == () and report.inferred_removed == ()


async def test_attach_orphan_skips_a_node_another_rule_attached_in_an_earlier_build() -> None:
    """One owner per node across rules: an inferred owner edge of INF-XDOC-01 makes fr-inferred no
    INF-CONT-01 subject; its own rule's inferred edge leaves it a subject (judged again, kept or not)."""

    other = orphan_graph()
    other.rules[("PART_OF", "fr-inferred", "feat-1")] = XDOC.id
    selection = await inference._select(cast(Executor, other), "p-1", V2, CONT1)
    assert "X::FR-INFERRED" not in {subject.spec_id for subject in selection.subjects}
    own = orphan_graph()
    own.rules[("PART_OF", "fr-inferred", "feat-1")] = CONT1.id
    selection = await inference._select(cast(Executor, own), "p-1", V2, CONT1)
    assert "X::FR-INFERRED" in {subject.spec_id for subject in selection.subjects}


@pytest.mark.parametrize(
    ("members", "before", "judged", "expected"),
    [
        ({"a", "b"}, {"a", "b"}, True, False),  # nothing changed: deferred, keeps its edges
        ({"a", "b", "c"}, {"a", "b"}, True, True),  # c joined
        ({"a"}, {"a", "b"}, True, True),  # b left
        ({"a", "m"}, {"a", "m"}, True, True),  # m modified
        ({"a", "m"}, None, True, True),  # no record: a modified member still counts
        ({"a", "b"}, None, True, False),
        ({"a", "c"}, {"a"}, False, True),  # never judged, c joined
        ({"a", "m"}, {"a", "m"}, False, False),  # never judged, only modified: the backlog waits
        ({"a", "n"}, None, False, True),  # never judged, no record: an added member joined
        ({"a", "m"}, None, False, False),
    ],
)
def test_an_update_judges_a_cross_document_subject_only_when_its_members_changed(
    members: set[str], before: set[str] | None, *, judged: bool, expected: bool
) -> None:
    scope = inference.UpdateScope(added=frozenset({"n"}), changed=frozenset({"n", "m"}))
    covered = scope.covers(frozenset(members), None if before is None else frozenset(before), judged=judged)
    assert covered is expected


async def test_an_update_defers_xdoc_subjects_whose_members_did_not_change(tmp_path: Path) -> None:
    """A judged hub whose inputs then change (a member renamed) is judged again in a full build; in an
    update only when the scope names a changed member. Every build records the hub's members."""

    memory = inference.JudgeMemory(FileExtractionStore(tmp_path), "p-1", MODEL)
    graph = xdoc_graph(hub_attributes={"type": "backend"})
    first = scripted({(XDOC.prompt, "Orders service"): xdoc_answer()})
    await run_infer(graph, with_rules(V2, XDOC), first, memory)
    assert len(first.calls_for(XDOC.prompt)) == 1
    assert await memory.members(XDOC, ["X::MOD-HUB"]) == {"X::MOD-HUB": frozenset(map(spec, XDOC_DOCUMENTS))}

    graph.nodes["fr-a"]["name"] = "Refund request (renamed)"

    async def update(scope: inference.UpdateScope) -> tuple[int, int]:
        gateway = scripted({(XDOC.prompt, "Orders service"): xdoc_answer()})
        report = await inference.infer(
            cast(Executor, graph),
            "p-1",
            with_rules(V2, XDOC),
            JudgeRunner(gateway, V2),
            stamp=STAMP,
            memory=memory,
            write=False,
            changes=inference.BuildChanges(update=scope),
        )
        return len(gateway.calls_for(XDOC.prompt)), report.rules[0].deferred

    assert await update(inference.UpdateScope()) == (0, 1)
    assert await update(inference.UpdateScope(changed=frozenset({"X::FR-A"}))) == (1, 0)


def test_a_cross_document_input_hash_keeps_reworded_statements_and_follows_quotes() -> None:
    nodes = {
        "hub": node("hub", "Module", "Refund service"),
        "fr-a": node("fr-a", "FunctionalRequirement", XDOC_NAMES["fr-a"], ("d1#f", "h1")),
        "fr-b": node("fr-b", "FunctionalRequirement", XDOC_NAMES["fr-b"], ("d2#f", "h2")),
    }
    subject = _Subject(
        "hub",
        "SPEC::HUB",
        targets=("fr-a", "fr-b"),
        pairs=(("fr-a", "fr-b"),),
        members=("fr-a", "fr-b"),
        documents={"fr-a": "D1", "fr-b": "D2"},
    )

    def digest(found: dict[str, _Node]) -> str:
        shown = inference._judgement(XDOC, subject, found, MODEL, ontology=V2)
        assert shown is not None
        return shown.digest

    base = digest(nodes)
    assert digest({key: dataclasses.replace(found, statements=("Reworded.",)) for key, found in nodes.items()}) == base
    assert digest(_changed(nodes, "fr-b", quote="Paid orders can be cancelled.")) != base
