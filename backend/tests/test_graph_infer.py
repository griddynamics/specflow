"""Offline tests of the pure parts of light inference (plan 4.T1; FR-INF-03; analysis.md "Light
inference"; m4-spec-edits G1-G3, G11, G13).

Verdict models, acceptance (``_evaluate``), the judge payload (local ids only; document text only
inside untrusted blocks), the input hash judge memory keys on, stored judgements read back, the
judge prompts against the payload and verdict fields, and the ontology loader's check 7 for the
``endpoints`` parameters ``via`` and ``mentions``. Live behaviour is in ``test_graph_infer_live.py``.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import graph.core.ontology as ontology_package
from graph.build import infer as inference
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
from graph.core.errors import OntologyError
from graph.core.llm import JudgeRunner, Verdict
from graph.core.llm.judge import EVIDENCE_RULE
from graph.core.ontology import InferenceRule, load_ontology
from graph.core.ontology.model import TEMPLATE_PARAMETERS
from graph_support import ScriptedGateway, outside_untrusted, shipped_ontology, untrusted_blocks

ONTOLOGY = shipped_ontology()
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
    outcome, proposals = _evaluate(ONTOLOGY, rule, subject, shown, verdict, nodes=NODES, texts=texts)
    return outcome, [(p.source, p.target, p.edge_key) for p in proposals]


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


# Acceptance (FR-INF-03, G2, G3)


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
    assert _evaluate(ONTOLOGY, ORPH, subject, shown, verdict, nodes=nodes, texts=texts) == ("invalid", [])


# Payload


@pytest.mark.parametrize("rule_id", sorted(SUBJECTS))
def test_the_payload_shows_local_ids_only(rule_id: str) -> None:
    rule, subject = SUBJECTS[rule_id]
    shown = judgement(rule, subject)
    text = json.dumps(shown.payload)
    for n in NODES.values():
        assert n.uuid not in text and n.spec_id not in text and n.identity_key not in text
    assert shown.payload["relation"] == rule.writes
    assert set(shown.payload["subject"]) == {"name", "label", "statements"}  # the subject has no id at all
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
        "subject-statement",
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
    if change == "subject-statement":
        nodes = _changed(NODES, "int-1", statements=("Another statement.",))
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
        rule = dataclasses.replace(INT, prompt="judge_endpoints.3")
    elif change == "judge-model":
        model = "another-judge"
    else:
        subject = dataclasses.replace(ENDPOINT_SUBJECT, pairs=(("mod-a", "mod-b"),))
    assert judgement(rule, subject, nodes, model).digest != base


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
    assert await memory.recall(dataclasses.replace(ORPH, prompt="judge_attach_orphan.3"), [key]) == {}
    assert await inference.JudgeMemory(None, "p-1", MODEL).recall(ORPH, [key]) == {}


def test_a_stored_judgement_reads_back_or_is_judged_again() -> None:
    verdict = endpoints(("T1", "T2"), ("T1", "T3"))
    shown = judgement(INT, ENDPOINT_SUBJECT)
    texts = {fragment_id: TEXTS[fragment_id] for fragment_id in shown.fragments}
    outcome, proposals = _evaluate(ONTOLOGY, INT, ENDPOINT_SUBJECT, shown, verdict, nodes=NODES, texts=texts)
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

SHIPPED_DIR = Path(ontology_package.__file__).parent


def load_variant(directory: Path, edit: Any) -> Any:
    data = yaml.safe_load((SHIPPED_DIR / "spec-ontology.yaml").read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    edit(data["inference"]["rules"])
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SHIPPED_DIR / "judge_prompts", directory / "judge_prompts")
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
