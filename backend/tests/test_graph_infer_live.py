"""Live Neo4j tests of light inference with the scripted judge (plan 4.T1; FR-INF-01..05).

docs/graph/analysis.md "Light inference" and the approved M4 spec edits (G1-G4, G9-G13):
judge memory, ``declined``, ``INF-INT-01`` targets from ``PART_OF`` / ``RELATES_TO`` / mentions with
one direction per pair, the exclusive-pair exclusion and the exact, project-scoped orphan candidates,
and fatal errors that propagate.

Most tests seed a small graph by Cypher and call ``graph.build.infer.infer`` directly; the shop tests
at the end run whole builds (``run_build``) of the shop corpus. Every judge answer is hand-written
(decision P3), keyed by ``(rule prompt, subject name)`` as tech-specs 9.1 says. A verdict names the
payload's local ids (``T1``, ``M1``, ``C1``), which follow from the seeded ``spec_id``s (``endpoints``
targets, ``realization`` members and candidates in ``spec_id`` order) or from the seeded vectors
(``attach_orphan`` candidates, most similar first); each answer says which node an id stands for.

Gate: ``SPECFLOW_TEST_NEO4J=1`` and ``uv run --env-file ../.env.local``. Every test writes into its
own ``pytest-graph-<hex>`` project ids; ``neo4j_project`` deletes them by ``group_id`` afterwards and
checks that nothing outside them changed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from graph.build import BuildContext, BuildResult, BuildSettings, FileStores, open_file_stores, run_build
from graph.build import infer as inference
from graph.build.files import FileExtractionStore
from graph.build.models import InferenceReport, RuleReport
from graph.core.errors import ConformanceError, GraphWriteError, InfrastructureUnavailableError, LLMError, LLMFatalError
from graph.core.identity import edge_uuid
from graph.core.llm import JudgeRunner
from graph.core.ontology import InferenceRule, InferenceSpec, Ontology
from graph.core.queries import Query
from graph_support import (
    NO_ANSWER,
    Neo4jProject,
    ScriptedGateway,
    ScriptedRequest,
    ScriptError,
    live_llm_config,
    shipped_ontology,
    shop_answers,
    shop_corpus,
    untrusted_blocks,
)
from specflow_live import requires_live

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, neo4j_project) live in graph_support.
pytest_plugins = ["graph_support"]

pytestmark = requires_live("NEO4J")

DIM = live_llm_config().embedding_dim
AT = "2026-10-23T00:00:00+00:00"
JUDGE_MODEL = "judge-test"
RULES: dict[str, InferenceRule] = {rule.template: rule for rule in shipped_ontology().inference.rules}
INT, CAP, ORPH = RULES["endpoints"], RULES["realization"], RULES["attach_orphan"]
RULE_IDS = tuple(rule.id for rule in shipped_ontology().inference.rules)
GatewayFactory = Callable[..., ScriptedGateway]


# Seeding


def vector(*parts: tuple[int, float]) -> list[float]:
    """A unit vector with weight ``w`` on axis ``i`` for each ``(i, w)``."""

    values = [0.0] * DIM
    for axis, weight in parts:
        values[axis] += weight
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


def near(anchor: int, cosine: float, own: int) -> list[float]:
    """A unit vector at ``cosine`` to the unit vector of axis ``anchor`` (rest on axis ``own``)."""

    return vector((anchor, cosine), (own, math.sqrt(1.0 - cosine * cosine)))


def ik(key: str) -> str:
    return f"k|{key}"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Seed:
    """A hand-made project graph: fragments, spec nodes (uuid = key, ``identity_key`` = ``k|<key>``,
    ``spec_id`` = ``X::<KEY>`` unless given), domain edges and mentions; written by ``write``."""

    fragments: list[dict[str, Any]] = field(default_factory=list)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    mentions: list[dict[str, str]] = field(default_factory=list)

    def fragment(self, fragment_id: str, text: str, status: str = "active") -> Seed:
        self.fragments.append(
            {
                "fragment_id": fragment_id,
                "text": text,
                "status": status,
                "ordinal": len(self.fragments),
                "content_hash": content_hash(text),
            }
        )
        return self

    def node(
        self,
        key: str,
        label: str,
        name: str,
        *fragments: str,
        spec_id: str | None = None,
        statements: Sequence[str] | None = None,
        embedding: list[float] | None = None,
    ) -> Seed:
        self.nodes.append(
            {
                "uuid": key,
                "label": label,
                "name": name,
                "spec_id": spec_id or f"X::{key.upper()}",
                "identity_key": ik(key),
                "statements": json.dumps([{"text": text} for text in (statements or [name])]),
                "embedding": embedding,
                "fragments": list(fragments),
            }
        )
        return self

    def edge(self, relation: str, source: str, target: str, basis: str | None = "stated", **props: Any) -> Seed:
        values = dict(props)
        if basis is not None:
            values["basis"] = basis
        if basis == "inferred":
            values.setdefault("confidence", 0.9)
            values.setdefault("evidence", "[]")
            values.setdefault("judge_model", JUDGE_MODEL)
        key = f"{ik(source)}|{relation}|{ik(target)}"
        self.edges.append({"relation": relation, "source": source, "target": target, "key": key, "props": values})
        return self

    def mention(self, node: str, fragment_id: str) -> Seed:
        self.mentions.append({"node": node, "fragment": fragment_id})
        return self

    async def write(self, project: Neo4jProject, project_id: str) -> Seed:
        await project.run(
            "UNWIND $rows AS row CREATE (f:Fragment {group_id: $p, fragment_id: row.fragment_id, "
            "document_id: 'd1', ordinal: row.ordinal, text: row.text, status: row.status, "
            "content_hash: row.content_hash})",
            p=project_id,
            rows=self.fragments,
        )
        await project.run(
            "UNWIND $rows AS row CREATE (n:SpecNode {group_id: $p, uuid: row.uuid, spec_id: row.spec_id, "
            "name: row.name, identity_key: row.identity_key, statements: row.statements}) "
            "SET n:$(row.label) SET n.embedding = row.embedding "
            "WITH n, row UNWIND row.fragments AS fragment_id "
            "MATCH (f:Fragment {group_id: $p, fragment_id: fragment_id}) "
            "CREATE (n)-[:REFERENCED_BY {group_id: $p, line_start: 1, line_end: 1}]->(f)",
            p=project_id,
            rows=self.nodes,
        )
        for relation in sorted({edge["relation"] for edge in self.edges}):
            await project.run(
                "UNWIND $rows AS row "
                "MATCH (s:SpecNode {group_id: $p, uuid: row.source}), (t:SpecNode {group_id: $p, uuid: row.target}) "
                f"CREATE (s)-[r:{relation} {{group_id: $p, edge_key: row.key, uuid: 'seeded|' + row.key}}]->(t) "
                "SET r += row.props",
                p=project_id,
                rows=[edge for edge in self.edges if edge["relation"] == relation],
            )
        await project.run(
            "UNWIND $rows AS row "
            "MATCH (m:SpecNode {group_id: $p, uuid: row.node}), (f:Fragment {group_id: $p, fragment_id: row.fragment}) "
            "CREATE (m)-[:MENTIONED_IN {group_id: $p, count: 1}]->(f)",
            p=project_id,
            rows=self.mentions,
        )
        return self


# Judge scripting: hand-written verdicts keyed by (rule prompt, subject name)


def payload_of(request: ScriptedRequest | Any) -> dict[str, Any]:
    user = request.user if isinstance(request, ScriptedRequest) else request.messages[-1]["content"]
    return json.loads(dict(untrusted_blocks(user))["payload"])


def fragments_of(call: Any) -> dict[str, str]:
    return {label: body for label, body in untrusted_blocks(call.messages[-1]["content"]) if label != "payload"}


def subject_name(request: ScriptedRequest) -> str:
    return str(payload_of(request)["subject"]["name"])


def evidence(fragment_id: str, quote: str) -> list[dict[str, str]]:
    return [{"fragment_id": fragment_id, "quote": quote}]


def links(*pairs: tuple[str, str], quote: tuple[str, str], confidence: float = 0.9) -> dict[str, Any]:
    return {
        "confidence": confidence,
        "evidence": evidence(*quote),
        "links": [{"source": source, "target": target} for source, target in pairs],
    }


def realized(*ids: str, quote: tuple[str, str], confidence: float = 0.9) -> dict[str, Any]:
    return {"confidence": confidence, "evidence": evidence(*quote), "targets": list(ids)}


def attach(target: str, quote: tuple[str, str], confidence: float = 0.9) -> dict[str, Any]:
    return {"confidence": confidence, "evidence": evidence(*quote), "target": target}


NO_LINK: dict[str, dict[str, Any]] = {
    INT.prompt: {"confidence": 0.9, "evidence": [], "links": []},
    CAP.prompt: {"confidence": 0.9, "evidence": [], "targets": []},
    ORPH.prompt: {"confidence": 0.9, "evidence": [], "target": None},
}

Answers = Mapping[tuple[str, str], Any]


def judge(factory: GatewayFactory, answers: Answers | None = None) -> ScriptedGateway:
    """A scripted gateway answering the judge prompts from ``answers``; any other judgement is
    unscripted (the test fails). ``judge(factory)`` alone allows no judge call at all."""

    gateway = factory(live_llm_config())
    for rule in RULES.values():
        gateway.key_by(rule.prompt, subject_name)
    for (prompt, name), value in (answers or {}).items():
        gateway.answer(prompt, name, value)
    return gateway


def judged(gateway: ScriptedGateway, rule: InferenceRule | None = None) -> list[Any]:
    """The subject names judged by inference, in call order (one rule, or every rule; hub
    consolidation also calls the judge class, under its own prompt)."""

    prompts = {rule.prompt} if rule is not None else {r.prompt for r in RULES.values()}
    return [call.key for call in gateway.calls if call.prompt in prompts]


def only(*rules: InferenceRule, cap: int | None = None) -> Ontology:
    """The shipped ontology with only ``rules`` (default all) and, when given, another ``cap``."""

    base = shipped_ontology()
    spec = base.inference
    return dataclasses.replace(
        base, inference=InferenceSpec(cap=cap or spec.cap, floor=spec.floor, rules=tuple(rules) or spec.rules)
    )


async def run(
    project: Neo4jProject,
    project_id: str,
    gateway: ScriptedGateway,
    *,
    ontology: Ontology | None = None,
    memory: inference.JudgeMemory | None = None,
    build_id: str = "b-1",
    runner: Any = None,
    graph_changed: bool = True,
) -> InferenceReport:
    ontology = ontology or project.ontology
    return await inference.infer(
        project.executor,
        project_id,
        ontology,
        runner or JudgeRunner(gateway, ontology),
        stamp=inference.InferenceStamp(build_id=build_id, at=AT, judge_model=JUDGE_MODEL),
        memory=memory,
        graph_changed=graph_changed,
    )


def remembering(root: Path, project_id: str, judge_model: str = JUDGE_MODEL) -> inference.JudgeMemory:
    return inference.JudgeMemory(FileExtractionStore(root / "extraction"), project_id, judge_model)


async def inferred(project: Neo4jProject, project_id: str) -> dict[str, dict[str, Any]]:
    """Every inferred edge of the project as ``<source key>><RELATION>><target key>`` -> properties."""

    rows = await project.run(
        "MATCH (s)-[r]->(t) WHERE r.group_id = $p AND r.basis = 'inferred' "
        "RETURN s.uuid AS s, type(r) AS rel, t.uuid AS t, properties(r) AS props",
        p=project_id,
    )
    return {f"{row['s']}>{row['rel']}>{row['t']}": row["props"] for row in rows}


def by_rule(report: InferenceReport) -> dict[str, RuleReport]:
    return {rule.rule: rule for rule in report.rules}


def counts(rule: RuleReport, *names: str) -> tuple[int, ...]:
    return tuple(getattr(rule, name) for name in names)


# Seed A: one subject per rule


F_MOD, F_INT, F_CAP, F_ORPH, F_FEAT = "f-mod", "f-int", "f-cap", "f-orph", "f-feat"
INT_SUBJECT = "Checkout sends paid orders to Orders"
CAP_SUBJECT = "Authentication"
ORPH_SUBJECT = "Customer can download each order as CSV"
EDGE_INT, EDGE_CAP, EDGE_ORPH = "mod-a>INTEGRATES_WITH>mod-b", "cap-1>REALIZED_BY>mod-b", "req-1>PART_OF>feat-2"
REF_INT = "X::MOD-A|INTEGRATES_WITH|X::MOD-B"
REF_CAP = "X::CAP-1|REALIZED_BY|X::MOD-B"
REF_ORPH = "X::REQ-1|PART_OF|X::FEAT-2"


def seed_a() -> Seed:
    """``int-1`` (PART_OF mod-a and mod-b: targets T1 mod-a, T2 mod-b), ``cap-1`` (member feat-1,
    PART_OF mod-b: candidate C1 mod-b) and the orphan ``req-1`` (candidates by cosine: C1 feat-2 0.9,
    C2 mod-a 0.5, C3 mod-b 0.3)."""

    return (
        Seed()
        .fragment(F_MOD, "Acme Shop has three modules: the Checkout service, the Orders service and the ERP.")
        .fragment(F_INT, "The Checkout service sends each paid order to the Orders service over a queue.")
        .fragment(F_CAP, "Authentication: staff sign in through the Orders service login page.")
        .fragment(F_ORPH, "Customer can download each order as a CSV file from the export page.")
        .fragment(F_FEAT, "Order history export lets staff download past orders as files.")
        .node("mod-a", "Module", "Checkout service", F_MOD, embedding=near(0, 0.5, 10))
        .node("mod-b", "Module", "Orders service", F_MOD, embedding=near(0, 0.3, 11))
        .node("mod-c", "Module", "ERP", F_MOD, embedding=near(0, 0.2, 12))
        .node("int-1", "IntegrationRequirement", INT_SUBJECT, F_INT, embedding=vector((13, 1.0)))
        .node("cap-1", "Capability", CAP_SUBJECT, F_CAP)
        .node("feat-1", "Feature", "Staff login", F_CAP, embedding=near(0, 0.1, 14))
        .node("feat-2", "Feature", "Order history export", F_FEAT, embedding=near(0, 0.9, 15))
        .node("req-1", "FunctionalRequirement", ORPH_SUBJECT, F_ORPH, embedding=vector((0, 1.0)))
        .edge("PART_OF", "int-1", "mod-a")
        .edge("PART_OF", "int-1", "mod-b")
        .edge("GROUPED_BY", "feat-1", "cap-1")
        .edge("PART_OF", "feat-1", "mod-b")
    )


QUOTE_INT = (F_INT, "sends each paid order to the Orders service")
QUOTE_CAP = (F_CAP, "sign in through the Orders service login page")
QUOTE_ORPH = (F_ORPH, "download each order as a CSV file")


def accept_all(confidence: float = 0.9) -> dict[tuple[str, str], Any]:
    return {
        (INT.prompt, INT_SUBJECT): links(("T1", "T2"), quote=QUOTE_INT, confidence=confidence),  # mod-a -> mod-b
        (CAP.prompt, CAP_SUBJECT): realized("C1", quote=QUOTE_CAP, confidence=confidence),  # mod-b
        (ORPH.prompt, ORPH_SUBJECT): attach("C1", quote=QUOTE_ORPH, confidence=confidence),  # feat-2
    }


def decline_all() -> dict[tuple[str, str], Any]:
    return {
        (INT.prompt, INT_SUBJECT): NO_LINK[INT.prompt],
        (CAP.prompt, CAP_SUBJECT): NO_LINK[CAP.prompt],
        (ORPH.prompt, ORPH_SUBJECT): NO_LINK[ORPH.prompt],
    }


@pytest.fixture
async def graph_a(neo4j_project: Neo4jProject) -> str:
    await seed_a().write(neo4j_project, neo4j_project.project_id)
    return neo4j_project.project_id


# FR-INF-01: each rule once per build; edge properties and report names


async def test_each_rule_runs_once_and_judges_each_subject_once(
    neo4j_project: Neo4jProject, graph_a: str, scripted_gateway: GatewayFactory
) -> None:
    gateway = judge(scripted_gateway, accept_all())
    report = await run(neo4j_project, graph_a, gateway)

    assert tuple(rule.rule for rule in report.rules) == RULE_IDS  # one report per rule, in ontology order
    assert Counter((call.prompt, call.key) for call in gateway.calls if call.call_class == "judge") == {
        (INT.prompt, INT_SUBJECT): 1,
        (CAP.prompt, CAP_SUBJECT): 1,
        (ORPH.prompt, ORPH_SUBJECT): 1,
    }
    for rule in report.rules:
        assert counts(rule, "subjects", "judged", "accepted", "edges_added", "edges_removed") == (1, 1, 1, 1, 0)
    assert report.error_code is None
    assert report.spine_unreachable_before == 1  # req-1
    assert report.integration_outliers == () and report.orphans_without_candidate == ()
    assert set(await inferred(neo4j_project, graph_a)) == {EDGE_INT, EDGE_CAP, EDGE_ORPH}


async def test_inferred_edges_carry_the_data_model_properties_and_keep_their_uuid(
    neo4j_project: Neo4jProject, graph_a: str, scripted_gateway: GatewayFactory
) -> None:
    before_write = datetime.now(UTC) - timedelta(seconds=5)
    report = await run(neo4j_project, graph_a, judge(scripted_gateway, accept_all()), build_id="b-1")
    assert report.inferred_added == tuple(sorted((REF_INT, REF_CAP, REF_ORPH)))
    assert report.inferred_removed == ()
    for ref in report.inferred_added:  # spec-id form, never the edge key (G9, NFR-SEC-01)
        assert "k|" not in ref and ref.count("|") == 2

    edges = await inferred(neo4j_project, graph_a)
    props = edges[EDGE_ORPH]
    key = f"{ik('req-1')}|PART_OF|{ik('feat-2')}"
    assert props["edge_key"] == key and props["uuid"] == edge_uuid(graph_a, key)
    assert props["group_id"] == graph_a
    assert (props["basis"], props["inference_rule"], props["confidence"]) == ("inferred", ORPH.id, 0.9)
    assert json.loads(props["evidence"]) == [{"fragment_id": F_ORPH, "quote": "download each order as a CSV file"}]
    assert props["source_fragment_ids"] == [F_ORPH]
    assert props["judge_model"] == JUDGE_MODEL
    assert (props["created_at"], props["updated_at"], props["last_build_id"]) == (AT, AT, "b-1")
    assert datetime.fromisoformat(props["inferred_at"]) >= before_write  # the write time, not the build's ``at``
    assert {edges[name]["inference_rule"] for name in (EDGE_INT, EDGE_CAP)} == {INT.id, CAP.id}

    # A re-judged subject refreshes its edge in place: same uuid and created_at, new verdict.
    report = await run(neo4j_project, graph_a, judge(scripted_gateway, accept_all(0.95)), build_id="b-2")
    assert report.inferred_added == () and report.inferred_removed == ()
    assert all(counts(rule, "edges_added", "edges_removed", "edges_updated") == (0, 0, 1) for rule in report.rules)
    refreshed = (await inferred(neo4j_project, graph_a))[EDGE_ORPH]
    assert (refreshed["uuid"], refreshed["created_at"]) == (props["uuid"], AT)
    assert (refreshed["confidence"], refreshed["last_build_id"]) == (0.95, "b-2")

    # Declined re-judgements remove them, named in spec-id form.
    report = await run(neo4j_project, graph_a, judge(scripted_gateway, decline_all()), build_id="b-3")
    assert report.inferred_removed == tuple(sorted((REF_INT, REF_CAP, REF_ORPH)))
    assert all(counts(rule, "declined", "rejected", "edges_removed") == (1, 0, 1) for rule in report.rules)
    assert await inferred(neo4j_project, graph_a) == {}


# FR-INF-02: selection reads only stated and implied edges


async def test_an_orphan_attached_by_inference_is_still_a_subject(
    neo4j_project: Neo4jProject, graph_a: str, scripted_gateway: GatewayFactory
) -> None:
    await run(neo4j_project, graph_a, judge(scripted_gateway, accept_all()))
    # req-1 -[inferred PART_OF]-> feat-2 -[stated PART_OF]-> mod-c: still unreached over stated edges.
    await neo4j_project.run(
        "MATCH (f:SpecNode {group_id: $p, uuid: 'feat-2'}), (m:SpecNode {group_id: $p, uuid: 'mod-c'}) "
        "CREATE (f)-[:PART_OF {group_id: $p, edge_key: 'k|feat-2|PART_OF|k|mod-c', basis: 'stated'}]->(m)",
        p=graph_a,
    )
    gateway = judge(scripted_gateway, accept_all())
    report = await run(neo4j_project, graph_a, gateway)
    rules = by_rule(report)
    assert report.spine_unreachable_before == 1
    assert judged(gateway, ORPH) == [ORPH_SUBJECT]
    assert counts(rules[ORPH.id], "subjects", "judged", "accepted") == (1, 1, 1)
    # The pair and the container linked only by inferred edges stay candidates too.
    assert judged(gateway, INT) == [INT_SUBJECT] and judged(gateway, CAP) == [CAP_SUBJECT]
    assert all(counts(rule, "edges_added", "edges_removed", "edges_updated") == (0, 0, 1) for rule in report.rules)
    assert set(await inferred(neo4j_project, graph_a)) == {EDGE_INT, EDGE_CAP, EDGE_ORPH}


# FR-INF-03: acceptance


@pytest.mark.parametrize(("confidence", "accepted"), [(0.6, False), (0.69, False), (0.7, True), (1.0, True)], ids=str)
async def test_the_confidence_floor_decides_for_every_template(
    neo4j_project: Neo4jProject, graph_a: str, scripted_gateway: GatewayFactory, confidence: float, *, accepted: bool
) -> None:
    assert shipped_ontology().inference.floor == 0.7
    report = await run(neo4j_project, graph_a, judge(scripted_gateway, accept_all(confidence)))
    for rule in report.rules:
        assert counts(rule, "judged", "accepted", "rejected", "edges_added") == (
            (1, 1, 0, 1) if accepted else (1, 0, 1, 0)
        )
    edges = await inferred(neo4j_project, graph_a)
    assert set(edges) == ({EDGE_INT, EDGE_CAP, EDGE_ORPH} if accepted else set())
    if accepted:
        assert {props["confidence"] for props in edges.values()} == {confidence}


BAD_EVIDENCE = {
    "quote-not-in-fragment": lambda quote: evidence(quote[0], "this sentence is in no fragment at all"),
    "fragment-not-shown": lambda quote: evidence("f-unknown", quote[1]),
    "quote-from-another-fragment": lambda quote: evidence(F_MOD if quote[0] != F_MOD else F_INT, quote[1]),
    "no-evidence": lambda _quote: [],
}


@pytest.mark.parametrize("case", sorted(BAD_EVIDENCE))
async def test_a_verdict_without_valid_quotes_is_rejected(
    neo4j_project: Neo4jProject, graph_a: str, scripted_gateway: GatewayFactory, case: str
) -> None:
    answers = accept_all(0.95)
    for (prompt, name), quote in zip(list(answers), (QUOTE_INT, QUOTE_CAP, QUOTE_ORPH), strict=True):
        answers[(prompt, name)] = {**answers[(prompt, name)], "evidence": BAD_EVIDENCE[case](quote)}
    report = await run(neo4j_project, graph_a, judge(scripted_gateway, answers))
    assert all(counts(rule, "judged", "accepted", "rejected", "edges_added") == (1, 0, 1, 0) for rule in report.rules)
    assert await inferred(neo4j_project, graph_a) == {}


INVALID_VERDICTS = {
    "endpoints-unknown-id": (INT, links(("T1", "T9"), quote=QUOTE_INT)),
    "endpoints-self-link": (INT, links(("T1", "T1"), quote=QUOTE_INT)),
    "endpoints-only-both-ways": (INT, links(("T1", "T2"), ("T2", "T1"), quote=QUOTE_INT)),
    "realization-member-id": (CAP, realized("M1", quote=QUOTE_CAP)),  # feat-1 is a member, no candidate
    "realization-one-unknown": (CAP, realized("C1", "C7", quote=QUOTE_CAP)),
    "orphan-unknown-id": (ORPH, attach("C9", quote=QUOTE_ORPH)),
}


@pytest.mark.parametrize("case", sorted(INVALID_VERDICTS))
async def test_an_invalid_verdict_writes_nothing(
    neo4j_project: Neo4jProject, graph_a: str, scripted_gateway: GatewayFactory, case: str
) -> None:
    rule, verdict = INVALID_VERDICTS[case]
    answers = decline_all()
    answers[(rule.prompt, {INT.id: INT_SUBJECT, CAP.id: CAP_SUBJECT, ORPH.id: ORPH_SUBJECT}[rule.id])] = verdict
    report = await run(neo4j_project, graph_a, judge(scripted_gateway, answers))
    assert counts(by_rule(report)[rule.id], "judged", "invalid", "accepted", "edges_added") == (1, 1, 0, 0)
    assert await inferred(neo4j_project, graph_a) == {}


# G2: any re-judgement without an accepted link removes the subject's earlier inferred edges

SUBJECT_OF = {INT.id: INT_SUBJECT, CAP.id: CAP_SUBJECT, ORPH.id: ORPH_SUBJECT}
REF_OF = {INT.id: REF_INT, CAP.id: REF_CAP, ORPH.id: REF_ORPH}
EDGE_OF = {INT.id: EDGE_INT, CAP.id: EDGE_CAP, ORPH.id: EDGE_ORPH}
REJUDGED: dict[str, tuple[str, str, Any]] = {
    f"{rule.template}-{kind}": (rule.id, outcome, verdict)
    for rule, quote, invalid in (
        (INT, QUOTE_INT, links(("T1", "T9"), quote=QUOTE_INT)),
        (CAP, QUOTE_CAP, realized("C7", quote=QUOTE_CAP)),
        (ORPH, QUOTE_ORPH, attach("C9", quote=QUOTE_ORPH)),
    )
    for kind, outcome, verdict in (
        (
            "bad-quote",
            "rejected",
            {**accept_all()[(rule.prompt, SUBJECT_OF[rule.id])], "evidence": evidence(quote[0], "no such words")},
        ),
        ("below-floor", "rejected", {**accept_all()[(rule.prompt, SUBJECT_OF[rule.id])], "confidence": 0.6}),
        ("invalid", "invalid", invalid),
    )
}


@pytest.mark.parametrize("case", sorted(REJUDGED))
async def test_a_rejected_or_invalid_rejudgement_removes_the_earlier_inferred_edge(
    neo4j_project: Neo4jProject, graph_a: str, scripted_gateway: GatewayFactory, case: str
) -> None:
    rule_id, outcome, verdict = REJUDGED[case]
    await run(neo4j_project, graph_a, judge(scripted_gateway, accept_all()))
    prompt = next(rule.prompt for rule in RULES.values() if rule.id == rule_id)
    answers = {**accept_all(), (prompt, SUBJECT_OF[rule_id]): verdict}
    report = await run(neo4j_project, graph_a, judge(scripted_gateway, answers), build_id="b-2")
    rule = by_rule(report)[rule_id]
    assert counts(rule, "judged", outcome, "accepted", "edges_removed") == (1, 1, 0, 1)
    assert report.inferred_removed == (REF_OF[rule_id],)
    assert set(await inferred(neo4j_project, graph_a)) == {EDGE_INT, EDGE_CAP, EDGE_ORPH} - {EDGE_OF[rule_id]}


# FR-INF-04: one swap transaction


def failing_writes(relation_to_fail: str) -> Callable[[str], Query]:
    original = inference.infer_write_edges

    def factory(relation: str) -> Query:
        if relation != relation_to_fail:
            return original(relation)
        return Query(
            "test_failing_infer_write",
            "UNWIND $rows AS row WITH row WHERE $project_id IS NOT NULL RETURN 1 / 0 AS written, [] AS refs",
        )

    return factory


async def test_a_failure_inside_the_swap_leaves_the_previous_inferred_edges_intact(
    neo4j_project: Neo4jProject,
    graph_a: str,
    scripted_gateway: GatewayFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = {**accept_all(), (INT.prompt, INT_SUBJECT): NO_LINK[INT.prompt]}
    await run(neo4j_project, graph_a, judge(scripted_gateway, first))
    before = await inferred(neo4j_project, graph_a)
    assert set(before) == {EDGE_CAP, EDGE_ORPH}

    # In one transaction: INT creates an edge, CAP deletes its edge, then ORPH's write fails.
    second = {
        (INT.prompt, INT_SUBJECT): links(("T1", "T2"), quote=QUOTE_INT),
        (CAP.prompt, CAP_SUBJECT): NO_LINK[CAP.prompt],
        (ORPH.prompt, ORPH_SUBJECT): attach("C1", quote=QUOTE_ORPH, confidence=0.95),
    }
    memory = remembering(tmp_path, graph_a)
    monkeypatch.setattr(inference, "infer_write_edges", failing_writes(ORPH.writes))
    with pytest.raises(GraphWriteError):
        await run(neo4j_project, graph_a, judge(scripted_gateway, second), memory=memory)
    assert await inferred(neo4j_project, graph_a) == before
    monkeypatch.undo()

    # Judge memory is written only after the swap commits: the subjects are judged again.
    gateway = judge(scripted_gateway, second)
    report = await run(neo4j_project, graph_a, gateway, memory=memory)
    assert len(judged(gateway)) == 3 and all(rule.reused == 0 for rule in report.rules)
    after = await inferred(neo4j_project, graph_a)
    assert set(after) == {EDGE_INT, EDGE_ORPH} and after[EDGE_ORPH]["confidence"] == 0.95


# FR-INF-05: failure tolerance


async def test_a_judge_that_always_fails_keeps_the_previous_inferred_edges(
    neo4j_project: Neo4jProject, graph_a: str, scripted_gateway: GatewayFactory, tmp_path: Path
) -> None:
    await run(neo4j_project, graph_a, judge(scripted_gateway, accept_all()))
    before = await inferred(neo4j_project, graph_a)
    memory = remembering(tmp_path, graph_a)
    failing = {
        (INT.prompt, INT_SUBJECT): LLMError("exhausted"),
        (CAP.prompt, CAP_SUBJECT): LLMError("malformed"),
        (ORPH.prompt, ORPH_SUBJECT): LLMError("refused"),
    }
    report = await run(neo4j_project, graph_a, judge(scripted_gateway, failing), memory=memory)
    assert report.error_code is None
    for rule in report.rules:
        assert counts(rule, "subjects", "judged", "failed", "edges_removed", "edges_updated") == (1, 0, 1, 0, 0)
        assert rule.error_code is None
    assert await inferred(neo4j_project, graph_a) == before  # not even refreshed
    # A failed judgement is not remembered.
    gateway = judge(scripted_gateway, decline_all())
    report = await run(neo4j_project, graph_a, gateway, memory=memory)
    assert len(judged(gateway)) == 3 and await inferred(neo4j_project, graph_a) == {}


class EscapingJudge:
    """A judge runner from which a non-fatal ``LLMError`` escapes for one prompt."""

    def __init__(self, inner: JudgeRunner, prompt: str) -> None:
        self._inner = inner
        self._prompt = prompt

    async def judge(self, prompt: str, payload: Mapping[str, Any], verdict_type: type[Any]) -> Any:
        if prompt == self._prompt:
            raise LLMError("exhausted")
        return await self._inner.judge(prompt, payload, verdict_type)


async def test_a_non_fatal_error_escaping_a_rule_is_reported_and_leaves_its_edges(
    neo4j_project: Neo4jProject, graph_a: str, scripted_gateway: GatewayFactory
) -> None:
    await run(neo4j_project, graph_a, judge(scripted_gateway, accept_all()))
    before = await inferred(neo4j_project, graph_a)
    gateway = judge(scripted_gateway, decline_all())
    runner = EscapingJudge(JudgeRunner(gateway, neo4j_project.ontology), CAP.prompt)
    report = await run(neo4j_project, graph_a, gateway, runner=runner)
    rules = by_rule(report)
    assert report.error_code == "llm_exhausted"
    assert rules[CAP.id].error_code == "llm_exhausted" and rules[CAP.id].subjects == 1
    assert rules[INT.id].error_code is None and rules[ORPH.id].error_code is None
    # The other rules still swap: their declined subjects lose their edges.
    assert await inferred(neo4j_project, graph_a) == {EDGE_CAP: before[EDGE_CAP]}


@pytest.mark.parametrize(
    "error",
    [LLMFatalError(), InfrastructureUnavailableError("llm_unreachable")],
    ids=["llm-fatal", "infrastructure-unavailable"],
)
async def test_fatal_errors_propagate_with_the_previous_inferred_edges_intact(
    neo4j_project: Neo4jProject, graph_a: str, scripted_gateway: GatewayFactory, error: Exception
) -> None:
    await run(neo4j_project, graph_a, judge(scripted_gateway, accept_all()))
    before = await inferred(neo4j_project, graph_a)
    # INT and CAP decline (their edges would go), then the last rule's judge raises.
    answers = {**decline_all(), (ORPH.prompt, ORPH_SUBJECT): error}
    with pytest.raises(type(error)):
        await run(neo4j_project, graph_a, judge(scripted_gateway, answers))
    assert await inferred(neo4j_project, graph_a) == before


# Cap, deferred subjects and judge memory


def seed_cap() -> Seed:
    """Orphans o-1, o-2, o-3 whose ``spec_id`` order (o-2, o-3, o-1) differs from their uuid order;
    each one's C1 is its own feature (cosine 0.9; the other two features are orthogonal)."""

    seed = Seed()
    for index, (key, spec_id, name) in enumerate(
        (
            ("o-1", "X::R3", "Requirement one"),
            ("o-2", "X::R1", "Requirement two"),
            ("o-3", "X::R2", "Requirement three"),
        ),
        1,
    ):
        seed.fragment(f"f-o{index}", f"{name} is written in the section of feature {index}.")
        seed.node(
            key, "FunctionalRequirement", name, f"f-o{index}", spec_id=spec_id, embedding=vector((20 + index, 1.0))
        )
        seed.node(
            f"feat-{index}", "Feature", f"Feature {index}", f"f-o{index}", embedding=near(20 + index, 0.9, 30 + index)
        )
    return seed


def attach_own(*names: str, confidence: float = 0.9) -> dict[tuple[str, str], Any]:
    number = {"Requirement one": 1, "Requirement two": 2, "Requirement three": 3}
    return {
        (ORPH.prompt, name): attach(
            "C1", quote=(f"f-o{number[name]}", "is written in the section"), confidence=confidence
        )
        for name in names
    }


ALL_ORPHANS = ("Requirement one", "Requirement two", "Requirement three")


async def test_the_cap_defers_subjects_in_spec_id_order_and_deferred_ones_keep_their_edges(
    neo4j_project: Neo4jProject, scripted_gateway: GatewayFactory
) -> None:
    pid = neo4j_project.project_id
    await seed_cap().write(neo4j_project, pid)
    await run(neo4j_project, pid, judge(scripted_gateway, attach_own(*ALL_ORPHANS)), ontology=only(ORPH))
    before = await inferred(neo4j_project, pid)
    assert set(before) == {"o-1>PART_OF>feat-1", "o-2>PART_OF>feat-2", "o-3>PART_OF>feat-3"}

    declines = {(ORPH.prompt, name): NO_LINK[ORPH.prompt] for name in ALL_ORPHANS}
    gateway = judge(scripted_gateway, declines)
    report = await run(neo4j_project, pid, gateway, ontology=only(ORPH, cap=1))
    assert judged(gateway) == ["Requirement two"]  # X::R1 first
    assert counts(report.rules[0], "subjects", "judged", "declined", "deferred", "edges_removed") == (3, 1, 1, 2, 1)
    after = await inferred(neo4j_project, pid)
    assert after == {key: before[key] for key in ("o-1>PART_OF>feat-1", "o-3>PART_OF>feat-3")}


async def test_reused_judgements_do_not_count_against_the_cap(
    neo4j_project: Neo4jProject, scripted_gateway: GatewayFactory, tmp_path: Path
) -> None:
    pid = neo4j_project.project_id
    await seed_cap().write(neo4j_project, pid)
    memory = remembering(tmp_path, pid)
    capped = only(ORPH, cap=1)
    expected = [("Requirement two", 0, 2), ("Requirement three", 1, 1), ("Requirement one", 2, 0)]
    for build, (name, reused, deferred) in enumerate(expected, 1):
        gateway = judge(scripted_gateway, attach_own(name))
        report = await run(neo4j_project, pid, gateway, ontology=capped, memory=memory, build_id=f"b-{build}")
        assert judged(gateway) == [name]  # a subject without a stored judgement takes the slot
        assert counts(report.rules[0], "judged", "reused", "deferred", "edges_added") == (1, reused, deferred, 1)
    gateway = judge(scripted_gateway)
    report = await run(neo4j_project, pid, gateway, ontology=capped, memory=memory, build_id="b-4")
    assert judged(gateway) == [] and counts(report.rules[0], "judged", "reused", "deferred") == (0, 3, 0)
    assert len(await inferred(neo4j_project, pid)) == 3


# A build whose diff changed nothing judges no new subject (scale report F2)


async def test_a_no_change_build_makes_no_judge_call_and_keeps_the_backlog(
    neo4j_project: Neo4jProject, scripted_gateway: GatewayFactory, tmp_path: Path
) -> None:
    pid = neo4j_project.project_id
    await seed_cap().write(neo4j_project, pid)
    memory = remembering(tmp_path, pid)
    capped = only(ORPH, cap=1)
    await run(
        neo4j_project, pid, judge(scripted_gateway, attach_own("Requirement two")), ontology=capped, memory=memory
    )
    before = await inferred(neo4j_project, pid)
    for build in (2, 3):
        gateway = judge(scripted_gateway)
        report = await run(
            neo4j_project, pid, gateway, ontology=capped, memory=memory, build_id=f"b-{build}", graph_changed=False
        )
        assert judged(gateway) == []
        assert counts(report.rules[0], "subjects", "reused", "judged", "deferred") == (3, 1, 0, 2)
        assert await inferred(neo4j_project, pid) == before


async def test_a_changing_build_judges_changed_subjects_first_then_the_backlog(
    neo4j_project: Neo4jProject, scripted_gateway: GatewayFactory, tmp_path: Path
) -> None:
    pid = neo4j_project.project_id
    await seed_cap().write(neo4j_project, pid)
    memory = remembering(tmp_path, pid)
    capped = only(ORPH, cap=1)
    await run(
        neo4j_project, pid, judge(scripted_gateway, attach_own("Requirement two")), ontology=capped, memory=memory
    )

    # Something changed: the backlog is judged up to the cap, in spec_id order (X::R2 next).
    gateway = judge(scripted_gateway, attach_own("Requirement three"))
    report = await run(neo4j_project, pid, gateway, ontology=capped, memory=memory, build_id="b-2")
    assert judged(gateway) == ["Requirement three"]
    assert counts(report.rules[0], "reused", "judged", "deferred") == (1, 1, 1)

    # A judged subject whose inputs changed goes before the never-judged one (X::R3), even under the cap.
    await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p, uuid: 'o-2'}) SET n.statements = $s",
        p=pid,
        s=json.dumps([{"text": "Requirement two, reworded."}]),
    )
    gateway = judge(scripted_gateway, attach_own("Requirement two"))
    report = await run(neo4j_project, pid, gateway, ontology=capped, memory=memory, build_id="b-3")
    assert judged(gateway) == ["Requirement two"]
    assert counts(report.rules[0], "reused", "judged", "deferred") == (1, 1, 1)

    # Without a change in the build, a changed subject waits too: no call, it keeps its edge.
    await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p, uuid: 'o-3'}) SET n.statements = $s",
        p=pid,
        s=json.dumps([{"text": "Requirement three, reworded."}]),
    )
    before = await inferred(neo4j_project, pid)
    gateway = judge(scripted_gateway)
    report = await run(neo4j_project, pid, gateway, ontology=capped, memory=memory, build_id="b-4", graph_changed=False)
    assert judged(gateway) == [] and counts(report.rules[0], "reused", "judged", "deferred") == (1, 0, 2)
    assert await inferred(neo4j_project, pid) == before


async def test_a_changed_subject_goes_before_never_judged_ones_with_lower_spec_ids(
    neo4j_project: Neo4jProject, scripted_gateway: GatewayFactory, tmp_path: Path
) -> None:
    pid = neo4j_project.project_id
    await seed_cap().write(neo4j_project, pid)
    memory = remembering(tmp_path, pid)
    # Only o-1 (X::R3, last in spec_id order) gets a verdict; the two failures are not remembered.
    answers = {
        **attach_own("Requirement one"),
        **{(ORPH.prompt, name): LLMError("exhausted") for name in ALL_ORPHANS[1:]},
    }
    await run(neo4j_project, pid, judge(scripted_gateway, answers), ontology=only(ORPH), memory=memory)
    await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p, uuid: 'o-1'}) SET n.statements = $s",
        p=pid,
        s=json.dumps([{"text": "Requirement one, reworded."}]),
    )
    gateway = judge(scripted_gateway, attach_own("Requirement one"))
    report = await run(neo4j_project, pid, gateway, ontology=only(ORPH, cap=1), memory=memory, build_id="b-2")
    assert judged(gateway) == ["Requirement one"]  # before the never-judged X::R1 and X::R2
    assert counts(report.rules[0], "reused", "judged", "deferred") == (0, 1, 2)


async def test_judge_memory_reuses_unchanged_subjects_and_rejudges_changed_ones(
    neo4j_project: Neo4jProject, graph_a: str, scripted_gateway: GatewayFactory, tmp_path: Path
) -> None:
    memory = remembering(tmp_path, graph_a)
    await run(neo4j_project, graph_a, judge(scripted_gateway, accept_all()), memory=memory)
    before = await inferred(neo4j_project, graph_a)

    # No change: no judge call, nothing rewritten (inferred_at included).
    gateway = judge(scripted_gateway)
    report = await run(neo4j_project, graph_a, gateway, memory=memory, build_id="b-2")
    assert judged(gateway) == []
    assert all(
        counts(r, "subjects", "reused", "judged", "edges_added", "edges_removed", "edges_updated") == (1, 1, 0, 0, 0, 0)
        for r in report.rules
    )
    assert report.inferred_added == () and report.inferred_removed == ()
    assert await inferred(neo4j_project, graph_a) == before

    # The orphan's statement changes: only it is judged again; it declines and loses its edge.
    await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p, uuid: 'req-1'}) SET n.statements = $s",
        p=graph_a,
        s=json.dumps([{"text": "Customer can download each order as a CSV file."}]),
    )
    gateway = judge(scripted_gateway, {(ORPH.prompt, ORPH_SUBJECT): NO_LINK[ORPH.prompt]})
    report = await run(neo4j_project, graph_a, gateway, memory=memory, build_id="b-3")
    assert judged(gateway) == [ORPH_SUBJECT]
    assert counts(by_rule(report)[ORPH.id], "judged", "declined", "reused", "edges_removed") == (1, 1, 0, 1)
    assert report.inferred_removed == (REF_ORPH,)
    assert set(await inferred(neo4j_project, graph_a)) == {EDGE_INT, EDGE_CAP}

    # The declined outcome is remembered too.
    gateway = judge(scripted_gateway)
    await run(neo4j_project, graph_a, gateway, memory=memory, build_id="b-4")
    assert judged(gateway) == [] and set(await inferred(neo4j_project, graph_a)) == {EDGE_INT, EDGE_CAP}

    # A changed fragment (content hash) re-judges the subjects that show it.
    await neo4j_project.run(
        "MATCH (f:Fragment {group_id: $p, fragment_id: $f}) SET f.content_hash = 'changed', f.text = $t",
        p=graph_a,
        f=F_INT,
        t="The Checkout service sends each paid order to the Orders service over a queue, at once.",
    )
    gateway = judge(scripted_gateway, {(INT.prompt, INT_SUBJECT): links(("T1", "T2"), quote=QUOTE_INT, confidence=0.8)})
    report = await run(neo4j_project, graph_a, gateway, memory=memory, build_id="b-5")
    assert judged(gateway) == [INT_SUBJECT] and by_rule(report)[INT.id].edges_updated == 1
    assert (await inferred(neo4j_project, graph_a))[EDGE_INT]["confidence"] == 0.8

    # A new candidate changes the orphan's inputs; another judge model changes every subject's.
    await neo4j_project.run(
        "CREATE (:SpecNode:Feature {group_id: $p, uuid: 'feat-9', spec_id: 'X::FEAT-9', name: 'CSV export', "
        "identity_key: 'k|feat-9', statements: '[]', embedding: $e})",
        p=graph_a,
        e=near(0, 0.95, 16),
    )
    gateway = judge(scripted_gateway, {(ORPH.prompt, ORPH_SUBJECT): NO_LINK[ORPH.prompt]})
    await run(neo4j_project, graph_a, gateway, memory=memory, build_id="b-6")
    assert judged(gateway) == [ORPH_SUBJECT]
    assert payload_of(gateway.calls_for(ORPH.prompt)[0])["candidates"][0]["name"] == "CSV export"
    gateway = judge(scripted_gateway, decline_all())
    other_model = remembering(tmp_path, graph_a, judge_model="judge-other")
    await run(neo4j_project, graph_a, gateway, memory=other_model, build_id="b-7")
    assert sorted(judged(gateway)) == sorted([INT_SUBJECT, CAP_SUBJECT, ORPH_SUBJECT])


async def test_an_unreadable_subject_is_deferred_and_keeps_its_edges(
    neo4j_project: Neo4jProject, graph_a: str, scripted_gateway: GatewayFactory
) -> None:
    await run(neo4j_project, graph_a, judge(scripted_gateway, accept_all()))
    before = await inferred(neo4j_project, graph_a)
    # The orphan loses its identity key: still selected, but not a valid spec node to show or link.
    await neo4j_project.run("MATCH (n:SpecNode {group_id: $p, uuid: 'req-1'}) REMOVE n.identity_key", p=graph_a)
    gateway = judge(scripted_gateway, {key: v for key, v in accept_all().items() if key[0] != ORPH.prompt})
    report = await run(neo4j_project, graph_a, gateway, build_id="b-2")
    assert judged(gateway, ORPH) == []
    assert counts(by_rule(report)[ORPH.id], "subjects", "judged", "deferred", "edges_removed") == (1, 0, 1, 0)
    assert (await inferred(neo4j_project, graph_a))[EDGE_ORPH] == before[EDGE_ORPH]


# Endpoints (INF-INT-01)


def seed_endpoints() -> Seed:
    """Five modules (spec_id order A..E) and integration requirements, one per case (docstrings of
    the expectations below)."""

    seed = Seed().fragment("f-mods", "Modules: Checkout service, Orders service, Stripe, ERP, Mail gateway.")
    for key, name in (
        ("a", "Checkout service"),
        ("b", "Orders service"),
        ("c", "Stripe"),
        ("d", "ERP"),
        ("e", "Mail gateway"),
    ):
        seed.node(f"m-{key}", "Module", name, "f-mods", spec_id=f"X::MOD-{key.upper()}")
    texts = {
        "f-via": "The Checkout service charges cards through Stripe and hands the order to the Orders service.",
        "f-both": "The Checkout service and the ERP exchange order totals every night.",
        "f-inf": "The ERP sends invoices through the Mail gateway.",
        "f-rev": "The Mail gateway reads order details from the Orders service.",
        "f-bad": "Stripe notifies the Mail gateway about refunds.",
        "f-one": "The Checkout service keeps its own audit trail.",
        "f-five": "Every module talks to every other module.",
        "f-half": "The Checkout service calls Stripe for refunds.",
        "f-other": "Integration notes.",
    }
    for fragment_id, text in texts.items():
        seed.fragment(fragment_id, text)
    seed.fragment("f-via-skip", "Receipts go out through the Mail gateway.", status="skipped")
    seed.fragment("f-via-old", "Old text: totals were pushed to the ERP.", status="retired")

    def requirement(key: str, name: str, *fragments: str) -> None:
        seed.node(f"i-{key}", "IntegrationRequirement", name, *fragments, spec_id=f"X::INT-{key.upper()}")

    # targets from PART_OF (a), RELATES_TO stored module -> requirement (b), a mention in an active
    # fragment (c) and in a skipped one (e); the mention in the retired fragment (d) does not count.
    requirement("via", "Checkout pays and hands over orders", "f-via", "f-via-skip", "f-via-old")
    seed.edge("PART_OF", "i-via", "m-a").edge("RELATES_TO", "m-b", "i-via")
    seed.mention("m-c", "f-via").mention("m-e", "f-via-skip").mention("m-d", "f-via-old")
    requirement("both", "Checkout and ERP exchange totals", "f-both")
    seed.edge("PART_OF", "i-both", "m-a").edge("PART_OF", "i-both", "m-d")
    # pairs already linked (reverse stated, implied, no basis): no subject
    requirement("linked", "Orders and Stripe settle payments", "f-other")
    seed.edge("PART_OF", "i-linked", "m-b").edge("PART_OF", "i-linked", "m-c").edge("INTEGRATES_WITH", "m-c", "m-b")
    requirement("implied", "Stripe reports to ERP", "f-other")
    seed.edge("PART_OF", "i-implied", "m-c").edge("PART_OF", "i-implied", "m-d")
    seed.edge("INTEGRATES_WITH", "m-c", "m-d", basis="implied")
    requirement("nobasis", "Orders feeds ERP", "f-other")
    seed.edge("PART_OF", "i-nobasis", "m-b").edge("PART_OF", "i-nobasis", "m-d")
    seed.edge("INTEGRATES_WITH", "m-d", "m-b", basis=None)
    # a pair linked only by an inferred edge stays a candidate
    requirement("inferred", "ERP mails invoices", "f-inf")
    seed.edge("PART_OF", "i-inferred", "m-d").edge("PART_OF", "i-inferred", "m-e")
    seed.edge("INTEGRATES_WITH", "m-d", "m-e", basis="inferred", inference_rule=INT.id)
    requirement("rev", "Mail gateway reads orders", "f-rev")
    seed.edge("PART_OF", "i-rev", "m-b").edge("PART_OF", "i-rev", "m-e")
    requirement("bad", "Stripe notifies Mail gateway", "f-bad")
    seed.edge("PART_OF", "i-bad", "m-c").edge("PART_OF", "i-bad", "m-e")
    # outliers: one module (PART_OF and a mention of the same), five modules, one stated + one inferred
    requirement("one", "Checkout alone", "f-one")
    seed.edge("PART_OF", "i-one", "m-a").mention("m-a", "f-one")
    requirement("five", "Everything talks", "f-five")
    for key in "abcde":
        seed.edge("RELATES_TO", "i-five", f"m-{key}")
    requirement("half", "Checkout to Stripe half inferred", "f-half")
    seed.edge("PART_OF", "i-half", "m-a").edge("PART_OF", "i-half", "m-c", basis="inferred", inference_rule=ORPH.id)
    return seed


ENDPOINT_ANSWERS: dict[tuple[str, str], Any] = {
    # T1 a, T2 b, T3 c, T4 e: a<->b both ways is dropped, a -> c stays
    (INT.prompt, "Checkout pays and hands over orders"): links(
        ("T1", "T2"), ("T2", "T1"), ("T1", "T3"), quote=("f-via", "charges cards through Stripe")
    ),
    (INT.prompt, "Checkout and ERP exchange totals"): links(  # T1 a, T2 d, both ways only
        ("T1", "T2"), ("T2", "T1"), quote=("f-both", "exchange order totals every night")
    ),
    (INT.prompt, "ERP mails invoices"): NO_LINK[INT.prompt],
    (INT.prompt, "Mail gateway reads orders"): links(  # T1 b, T2 e: e -> b
        ("T2", "T1"), quote=("f-rev", "reads order details from the Orders service")
    ),
    (INT.prompt, "Stripe notifies Mail gateway"): links(("T1", "T9"), quote=("f-bad", "notifies the Mail gateway")),
}


async def test_endpoints_targets_pairs_directions_and_outliers(
    neo4j_project: Neo4jProject, scripted_gateway: GatewayFactory
) -> None:
    pid = neo4j_project.project_id
    await seed_endpoints().write(neo4j_project, pid)
    gateway = judge(scripted_gateway, ENDPOINT_ANSWERS)
    report = await run(neo4j_project, pid, gateway, ontology=only(INT))

    # Subjects in spec_id order; pairs linked by a stated, implied or basis-less edge are never judged.
    assert judged(gateway) == [
        "Stripe notifies Mail gateway",
        "Checkout and ERP exchange totals",
        "ERP mails invoices",
        "Mail gateway reads orders",
        "Checkout pays and hands over orders",
    ]
    assert report.integration_outliers == ("X::INT-FIVE", "X::INT-HALF", "X::INT-ONE")
    rule = report.rules[0]
    assert counts(rule, "subjects", "judged", "accepted", "invalid", "declined", "rejected") == (5, 5, 2, 2, 1, 0)
    assert counts(rule, "edges_added", "edges_removed") == (2, 1)
    assert report.inferred_added == ("X::MOD-A|INTEGRATES_WITH|X::MOD-C", "X::MOD-E|INTEGRATES_WITH|X::MOD-B")
    assert report.inferred_removed == ("X::MOD-D|INTEGRATES_WITH|X::MOD-E",)  # declined re-judgement
    edges = {key for key in await inferred(neo4j_project, pid) if ">INTEGRATES_WITH>" in key}
    assert edges == {"m-a>INTEGRATES_WITH>m-c", "m-e>INTEGRATES_WITH>m-b"}

    via = payload_of(next(c for c in gateway.calls if c.key == "Checkout pays and hands over orders"))
    assert [(t["id"], t["name"]) for t in via["targets"]] == [
        ("T1", "Checkout service"),
        ("T2", "Orders service"),
        ("T3", "Stripe"),
        ("T4", "Mail gateway"),
    ]
    # b-c is linked by i-linked's stated c -> b, so that pair is no candidate.
    assert via["candidate_pairs"] == [["T1", "T2"], ["T1", "T3"], ["T1", "T4"], ["T2", "T4"], ["T3", "T4"]]
    shown = fragments_of(next(c for c in gateway.calls if c.key == "Checkout pays and hands over orders"))
    assert "f-via" in shown and "f-via-skip" in shown and "f-via-old" not in shown


async def test_an_endpoints_edge_is_kept_by_a_failed_owner_until_its_pair_is_linked(
    neo4j_project: Neo4jProject, scripted_gateway: GatewayFactory
) -> None:
    pid = neo4j_project.project_id
    seed = Seed().fragment("f-x", "The Checkout service calls the Orders service and Stripe.")
    for key, name in (("a", "Checkout service"), ("b", "Orders service"), ("c", "Stripe")):
        seed.node(f"m-{key}", "Module", name, "f-x", spec_id=f"X::MOD-{key.upper()}")
    seed.node("i-x", "IntegrationRequirement", "Checkout calls both", "f-x", spec_id="X::INT-X")
    seed.node("i-y", "IntegrationRequirement", "Checkout calls Orders", "f-x", spec_id="X::INT-Y")
    for target in ("m-a", "m-b", "m-c"):
        seed.edge("PART_OF", "i-x", target)
    seed.edge("PART_OF", "i-y", "m-a").edge("PART_OF", "i-y", "m-b")
    await seed.write(neo4j_project, pid)
    quote = ("f-x", "calls the Orders service and Stripe")
    first = {
        (INT.prompt, "Checkout calls both"): links(("T1", "T2"), ("T1", "T3"), quote=quote),  # a -> b, a -> c
        (INT.prompt, "Checkout calls Orders"): NO_LINK[INT.prompt],
    }
    await run(neo4j_project, pid, judge(scripted_gateway, first), ontology=only(INT))
    before = await inferred(neo4j_project, pid)
    assert set(before) == {"m-a>INTEGRATES_WITH>m-b", "m-a>INTEGRATES_WITH>m-c"}

    # i-y declines a -> b, but i-x (whose targets hold both ends) failed: the edge stays.
    failed = {**first, (INT.prompt, "Checkout calls both"): LLMError("exhausted")}
    report = await run(neo4j_project, pid, judge(scripted_gateway, failed), ontology=only(INT))
    assert counts(report.rules[0], "failed", "declined", "edges_removed") == (1, 1, 0)
    assert await inferred(neo4j_project, pid) == before

    # A stated b -> a now links the pair: the failed owner's a -> b goes (i-y is no subject any more).
    await neo4j_project.run(
        "MATCH (b:SpecNode {group_id: $p, uuid: 'm-b'}), (a:SpecNode {group_id: $p, uuid: 'm-a'}) "
        "CREATE (b)-[:INTEGRATES_WITH {group_id: $p, edge_key: 'k|m-b|INTEGRATES_WITH|k|m-a', basis: 'stated'}]->(a)",
        p=pid,
    )
    gateway = judge(scripted_gateway, {(INT.prompt, "Checkout calls both"): LLMError("exhausted")})
    report = await run(neo4j_project, pid, gateway, ontology=only(INT))
    assert judged(gateway) == ["Checkout calls both"]
    assert report.inferred_removed == ("X::MOD-A|INTEGRATES_WITH|X::MOD-B",)
    assert await inferred(neo4j_project, pid) == {"m-a>INTEGRATES_WITH>m-c": before["m-a>INTEGRATES_WITH>m-c"]}
    rows = await neo4j_project.run(
        "MATCH (:SpecNode {group_id: $p, uuid: 'm-b'})-[r:INTEGRATES_WITH]-(:SpecNode {group_id: $p, uuid: 'm-a'}) "
        "RETURN r.basis AS basis",
        p=pid,
    )
    assert rows == [{"basis": "stated"}]


async def test_a_fresh_verdict_wins_over_a_remembered_one_for_the_same_edge(
    neo4j_project: Neo4jProject, scripted_gateway: GatewayFactory, tmp_path: Path
) -> None:
    """Two subjects propose the same edge at the same confidence; the one judged now (not the one
    reused from judge memory) is written, so the edge carries the current verdict."""

    pid = neo4j_project.project_id
    seed = Seed().fragment("f-x", "The Checkout service calls the Orders service.")
    seed.fragment("f-y", "Orders are pushed by the Checkout service to the Orders service.")
    seed.node("m-a", "Module", "Checkout service", "f-x", spec_id="X::MOD-A")
    seed.node("m-b", "Module", "Orders service", "f-x", spec_id="X::MOD-B")
    seed.node("i-x", "IntegrationRequirement", "Checkout calls Orders", "f-x", spec_id="X::INT-X")
    seed.node("i-y", "IntegrationRequirement", "Checkout pushes orders", "f-y", spec_id="X::INT-Y")
    seed.edge("PART_OF", "i-x", "m-a").edge("PART_OF", "i-x", "m-b").edge("PART_OF", "i-y", "m-a").edge(
        "PART_OF", "i-y", "m-b"
    )
    await seed.write(neo4j_project, pid)
    memory = remembering(tmp_path, pid)
    answers = {
        (INT.prompt, "Checkout calls Orders"): links(("T1", "T2"), quote=("f-x", "calls the Orders service")),
        (INT.prompt, "Checkout pushes orders"): links(("T1", "T2"), quote=("f-y", "pushed by the Checkout service")),
    }
    await run(neo4j_project, pid, judge(scripted_gateway, answers), ontology=only(INT), memory=memory)

    await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p, uuid: 'i-y'}) SET n.statements = $s",
        p=pid,
        s=json.dumps([{"text": "Orders are pushed to the Orders service."}]),
    )
    fresh = {(INT.prompt, "Checkout pushes orders"): links(("T1", "T2"), quote=("f-y", "to the Orders service"))}
    gateway = judge(scripted_gateway, fresh)
    report = await run(neo4j_project, pid, gateway, ontology=only(INT), memory=memory, build_id="b-2")
    assert judged(gateway) == ["Checkout pushes orders"]
    assert counts(report.rules[0], "reused", "judged", "edges_added", "edges_updated") == (1, 1, 0, 1)
    edge = (await inferred(neo4j_project, pid))["m-a>INTEGRATES_WITH>m-b"]
    assert edge["last_build_id"] == "b-2"
    assert json.loads(edge["evidence"]) == [{"fragment_id": "f-y", "quote": "to the Orders service"}]


async def test_an_inferred_edge_that_became_stated_is_neither_removed_nor_duplicated(
    neo4j_project: Neo4jProject, graph_a: str, scripted_gateway: GatewayFactory
) -> None:
    await run(neo4j_project, graph_a, judge(scripted_gateway, accept_all()))
    # Extraction later states the inferred PART_OF (data-model.md: the edge becomes stated in place).
    await neo4j_project.run(
        "MATCH ()-[r:PART_OF {group_id: $p, edge_key: $k}]->() SET r.basis = 'stated'",
        p=graph_a,
        k=f"{ik('req-1')}|PART_OF|{ik('feat-2')}",
    )
    gateway = judge(scripted_gateway, {key: v for key, v in accept_all().items() if key[0] != ORPH.prompt})
    report = await run(neo4j_project, graph_a, gateway)
    # req-1 now holds an exclusive PART_OF pair (requirement -> feature): no subject, nothing written.
    assert by_rule(report)[ORPH.id].subjects == 0 and judged(gateway, ORPH) == []
    rows = await neo4j_project.run(
        "MATCH (:SpecNode {group_id: $p, uuid: 'req-1'})-[r:PART_OF]->(:SpecNode {group_id: $p, uuid: 'feat-2'}) "
        "RETURN r.basis AS basis",
        p=graph_a,
    )
    assert rows == [{"basis": "stated"}]
    assert set(await inferred(neo4j_project, graph_a)) == {EDGE_INT, EDGE_CAP}


# Realization (INF-CAP-01)


def seed_realization() -> Seed:
    """``cap-x`` with members feat-1 (GROUPED_BY) and req-1 (a requirement GROUPED_BY): candidates
    m-hop3 (feat-1 -> feat-2 -> feat-3 -> m-hop3, 3 hops) and m-req (req-1 -> m-req); not m-hop4
    (4 hops), m-done (already stated REALIZED_BY) or m-inf (reached only by an inferred edge)."""

    seed = Seed().fragment("f-audit", "Audit logging: the export module writes every download to the audit log.")
    for key, name in (
        ("hop3", "Export module"),
        ("hop4", "Deep module"),
        ("req", "Audit store"),
        ("done", "Admin console"),
        ("inf", "Inferred module"),
    ):
        seed.node(f"m-{key}", "Module", name, "f-audit", spec_id=f"X::MOD-{key.upper()}")
    seed.node("cap-x", "Capability", "Audit logging", "f-audit")
    for index in range(1, 5):
        seed.node(f"feat-{index}", "Feature", f"Feature level {index}", "f-audit")
    seed.node("req-1", "FunctionalRequirement", "System logs every download", "f-audit")
    seed.edge("GROUPED_BY", "feat-1", "cap-x").edge("GROUPED_BY", "req-1", "cap-x")
    seed.edge("PART_OF", "feat-1", "feat-2").edge("PART_OF", "feat-2", "feat-3").edge("PART_OF", "feat-3", "m-hop3")
    seed.edge("PART_OF", "feat-3", "feat-4").edge("PART_OF", "feat-4", "m-hop4")
    seed.edge("PART_OF", "req-1", "m-req")
    seed.edge("PART_OF", "feat-2", "m-done").edge("REALIZED_BY", "cap-x", "m-done")
    seed.edge("PART_OF", "feat-1", "m-inf", basis="inferred", inference_rule=ORPH.id)
    return seed


async def test_realization_candidates_follow_members_up_to_max_hops(
    neo4j_project: Neo4jProject, scripted_gateway: GatewayFactory
) -> None:
    pid = neo4j_project.project_id
    await seed_realization().write(neo4j_project, pid)
    assert CAP.max_hops == 3
    answers = {
        (CAP.prompt, "Audit logging"): realized("C1", quote=("f-audit", "writes every download to the audit log"))
    }
    gateway = judge(scripted_gateway, answers)
    report = await run(neo4j_project, pid, gateway, ontology=only(CAP))
    payload = payload_of(gateway.calls_for(CAP.prompt)[0])
    assert [(c["id"], c["name"], c["via"]) for c in payload["candidates"]] == [
        ("C1", "Export module", ["M1"]),  # X::MOD-HOP3, via feat-1
        ("C2", "Audit store", ["M2"]),  # X::MOD-REQ, via req-1
    ]
    assert [(m["id"], m["name"]) for m in payload["members"]] == [
        ("M1", "Feature level 1"),
        ("M2", "System logs every download"),
    ]
    assert counts(report.rules[0], "subjects", "accepted", "truncated", "edges_added") == (1, 1, 0, 1)
    assert set(await inferred(neo4j_project, pid)) == {"cap-x>REALIZED_BY>m-hop3", "feat-1>PART_OF>m-inf"}


async def test_realization_payload_shows_at_most_twelve_members_and_candidates(
    neo4j_project: Neo4jProject, scripted_gateway: GatewayFactory
) -> None:
    pid = neo4j_project.project_id
    seed = Seed().fragment("f-t", "Every module takes part in tracing.")
    seed.node("cap-t", "Capability", "Tracing", "f-t")
    seed.node("m-shared", "Module", "Shared module", "f-t", spec_id="X::MOD-ZZ")  # reached from two members
    for index in range(13):
        seed.node(f"f-{index:02d}", "Feature", f"Feature {index:02d}", "f-t")
        seed.node(f"m-{index:02d}", "Module", f"Module {index:02d}", "f-t")
        seed.edge("GROUPED_BY", f"f-{index:02d}", "cap-t").edge("PART_OF", f"f-{index:02d}", f"m-{index:02d}")
    seed.edge("PART_OF", "f-11", "m-shared").edge("PART_OF", "f-12", "m-shared")
    await seed.write(neo4j_project, pid)
    gateway = judge(scripted_gateway, {(CAP.prompt, "Tracing"): NO_LINK[CAP.prompt]})
    report = await run(neo4j_project, pid, gateway, ontology=only(CAP))
    payload = payload_of(gateway.calls_for(CAP.prompt)[0])
    assert len(payload["members"]) == 12 and len(payload["candidates"]) == 12
    assert "Shared module" in [c["name"] for c in payload["candidates"]]  # reached from the most members
    member_ids = {m["id"] for m in payload["members"]}
    assert all(set(c["via"]) <= member_ids and c["via"] for c in payload["candidates"])
    assert counts(report.rules[0], "subjects", "truncated", "declined") == (1, 1, 1)


# Attach orphan (INF-CONT-01)


def seed_orphans() -> Seed:
    """The orphan r-main (axis 0) and its candidates by exact cosine: feat-near 0.9, mod-mid 0.8, then
    feat-tie-a and feat-tie-b at 0.7 (ties by spec_id); cap-close (0.95) is no target label. Other
    requirements: r-excl holds an exclusive pair (no subject), r-excl-inf holds it only by an inferred
    edge (a subject), r-novec has no embedding (no candidate), r-rel and r-cap reach a module (no
    orphan), r-inf reaches one only by an inferred edge (an orphan)."""

    seed = Seed().fragment("f-main", "Customer can export the orders of one month as a spreadsheet.")
    seed.fragment("f-misc", "Further requirements of the shop.")
    seed.node("r-main", "FunctionalRequirement", "Customer exports orders", "f-main", embedding=vector((0, 1.0)))
    seed.node("feat-near", "Feature", "Order export", "f-misc", spec_id="X::FEAT-NEAR", embedding=near(0, 0.9, 40))
    seed.node("mod-mid", "Module", "Orders service", "f-misc", spec_id="X::MOD-MID", embedding=near(0, 0.8, 41))
    seed.node("feat-tie-b", "Feature", "Tie B", "f-misc", spec_id="X::FEAT-TIE-B", embedding=near(0, 0.7, 42))
    seed.node("feat-tie-a", "Feature", "Tie A", "f-misc", spec_id="X::FEAT-TIE-A", embedding=near(0, 0.7, 43))
    seed.node("cap-close", "Capability", "Reporting", "f-misc", embedding=near(0, 0.95, 44))
    seed.node("feat-lonely", "Feature", "Lonely feature", "f-misc", embedding=vector((45, 1.0)))
    seed.node("mod-x", "Module", "Mail gateway", "f-misc", embedding=vector((46, 1.0)))
    seed.node("r-excl", "FunctionalRequirement", "Part of a lonely feature", "f-misc", embedding=vector((50, 1.0)))
    seed.node(
        "r-excl-inf", "FunctionalRequirement", "Inferred into a lonely feature", "f-misc", embedding=vector((51, 1.0))
    )
    seed.node("r-novec", "FunctionalRequirement", "Requirement without a vector", "f-misc")
    seed.node("r-rel", "QualityRequirement", "Relates to the mail gateway", "f-misc", embedding=vector((52, 1.0)))
    seed.node("r-cap", "FunctionalRequirement", "Grouped under reporting", "f-misc", embedding=vector((53, 1.0)))
    seed.node("r-inf", "FunctionalRequirement", "Inferred into the mail gateway", "f-misc", embedding=vector((54, 1.0)))
    seed.edge("PART_OF", "r-excl", "feat-lonely")
    seed.edge("PART_OF", "r-excl-inf", "feat-lonely", basis="inferred", inference_rule=ORPH.id)
    seed.edge("RELATES_TO", "r-rel", "mod-x")
    seed.edge("GROUPED_BY", "r-cap", "cap-close").edge("REALIZED_BY", "cap-close", "mod-x")
    seed.edge("PART_OF", "r-inf", "mod-x", basis="inferred", inference_rule=ORPH.id)
    return seed


ORPHAN_ANSWERS: dict[tuple[str, str], Any] = {
    (ORPH.prompt, "Customer exports orders"): attach(
        "C1", quote=("f-main", "export the orders of one month")
    ),  # feat-near
    (ORPH.prompt, "Inferred into a lonely feature"): NO_LINK[ORPH.prompt],
    (ORPH.prompt, "Inferred into the mail gateway"): NO_LINK[ORPH.prompt],
}


async def test_orphan_subjects_and_their_exact_project_scoped_candidates(
    neo4j_project: Neo4jProject, scripted_gateway: GatewayFactory
) -> None:
    pid = neo4j_project.project_id
    await seed_orphans().write(neo4j_project, pid)
    gateway = judge(scripted_gateway, ORPHAN_ANSWERS)
    report = await run(neo4j_project, pid, gateway, ontology=only(ORPH))
    assert sorted(judged(gateway)) == sorted(name for _, name in ORPHAN_ANSWERS)
    assert report.orphans_without_candidate == ("X::R-NOVEC",)
    assert report.spine_unreachable_before == 5  # r-main, r-excl, r-excl-inf, r-novec, r-inf
    rule = report.rules[0]
    assert counts(rule, "subjects", "accepted", "declined", "edges_added", "edges_removed") == (3, 1, 2, 1, 2)
    main = payload_of(next(c for c in gateway.calls if c.key == "Customer exports orders"))
    expected = [("C1", "Order export"), ("C2", "Orders service"), ("C3", "Tie A")]
    assert [(c["id"], c["name"]) for c in main["candidates"]] == expected
    assert set(await inferred(neo4j_project, pid)) == {"r-main>PART_OF>feat-near"}

    # Another project in the same database with nodes nearer than any of this project's: the same
    # candidates (exact cosine over this project's targets, never the shared vector index).
    other = neo4j_project.new_project()
    await neo4j_project.run(
        "UNWIND range(1, 150) AS i CREATE (:SpecNode:Feature {group_id: $o, uuid: 'o-' + toString(i), "
        "spec_id: 'X::OTHER-' + toString(i), name: 'Other ' + toString(i), identity_key: 'o|' + toString(i), "
        "statements: '[]', embedding: $e})",
        o=other,
        e=vector((0, 1.0)),
    )
    gateway = judge(scripted_gateway, ORPHAN_ANSWERS)
    report = await run(neo4j_project, pid, gateway, ontology=only(ORPH))
    main = payload_of(next(c for c in gateway.calls if c.key == "Customer exports orders"))
    assert [(c["id"], c["name"]) for c in main["candidates"]] == expected
    assert report.orphans_without_candidate == ("X::R-NOVEC",)


async def test_candidates_already_linked_by_the_written_relation_are_left_out(
    neo4j_project: Neo4jProject, scripted_gateway: GatewayFactory
) -> None:
    """G4. With the shipped rule this cannot occur (an orphan's stated PART_OF is an exclusive pair or
    reaches a module), so a variant of the rule attaches quality requirements to functional ones by the
    symmetric RELATES_TO:
    the orphan's nearest target, already related to it (stored the other way), is no candidate; one
    related only by an inferred edge still is."""

    pid = neo4j_project.project_id
    rule = dataclasses.replace(
        ORPH,
        writes="RELATES_TO",
        subject=frozenset({"QualityRequirement"}),
        targets=frozenset({"FunctionalRequirement"}),
    )
    seed = Seed().fragment("f-1", "Customer can export orders and print invoices.")
    seed.node("r-1", "QualityRequirement", "Exports are fast", "f-1", embedding=vector((0, 1.0)))
    seed.node("r-near", "FunctionalRequirement", "Customer exports orders", "f-1", embedding=near(0, 0.95, 60))
    seed.node("r-mid", "FunctionalRequirement", "Customer prints invoices", "f-1", embedding=near(0, 0.9, 61))
    seed.node("r-far", "FunctionalRequirement", "Customer pays", "f-1", embedding=near(0, 0.5, 62))
    seed.edge("RELATES_TO", "r-near", "r-1")  # stated, stored towards the subject
    seed.edge("RELATES_TO", "r-1", "r-mid", basis="inferred", inference_rule=rule.id)
    await seed.write(neo4j_project, pid)
    gateway = judge(scripted_gateway, {(rule.prompt, "Exports are fast"): NO_LINK[rule.prompt]})
    await run(neo4j_project, pid, gateway, ontology=only(rule))
    candidates = [c["name"] for c in payload_of(gateway.calls_for(rule.prompt)[0])["candidates"]]
    assert candidates == ["Customer prints invoices", "Customer pays"]


async def test_an_orphan_without_any_target_vector_has_no_candidate(
    neo4j_project: Neo4jProject, scripted_gateway: GatewayFactory
) -> None:
    pid = neo4j_project.project_id
    seed = Seed().fragment("f-1", "Customer can print an invoice.")
    seed.node("r-1", "FunctionalRequirement", "Customer prints invoices", "f-1", embedding=vector((0, 1.0)))
    seed.node("feat-1", "Feature", "Invoices", "f-1")  # no embedding
    await seed.write(neo4j_project, pid)
    gateway = judge(scripted_gateway)
    report = await run(neo4j_project, pid, gateway, ontology=only(ORPH))
    assert judged(gateway) == [] and report.rules[0].subjects == 0
    assert report.orphans_without_candidate == ("X::R-1",)


# Whole builds of the shop corpus with inference on


# Hand-written verdicts for the shop's orphan requirements: (candidate name, quote) or None (no link).
SHOP_VERDICTS: dict[str, tuple[str, str] | None] = {
    "System cannot store full card numbers": ("Payment flow", "never stores full card numbers"),
    "System can leave cancelled orders out of the order file": (
        "Order history export",
        "cancelled orders are left out of the file",
    ),
    "Staff with the finance role can request the order file": None,
    "Staff can reach the download screen from the finance section": None,
    "System can revoke download access when staff lose the finance role": None,
    "Calls between internal modules use HTTPS": None,
}


def shop_judge(gateway: ScriptedGateway, failure: Exception | None = None) -> ScriptedGateway:
    """Script the attach-orphan judgements of the shop builds from ``SHOP_VERDICTS`` (keyed by subject
    name; the candidate's local id and the quoted fragment are looked up in the request), or answer
    every judgement with ``failure``. The other rules have no subject in the shop corpus."""

    def handle(request: ScriptedRequest) -> Any:
        name = subject_name(request)
        if name not in SHOP_VERDICTS:
            return NO_ANSWER
        if failure is not None:
            return failure
        verdict = SHOP_VERDICTS[name]
        if verdict is None:
            return NO_LINK[ORPH.prompt]
        target, quote = verdict
        ids = [c["id"] for c in payload_of(request)["candidates"] if c["name"] == target]
        fragments = [
            label
            for label, body in untrusted_blocks(request.user)
            if label != "payload" and quote in " ".join(body.split())
        ]
        if len(ids) != 1 or not fragments:
            raise ScriptError(f"shop verdict for {name!r}: {target!r} or its quote not in the request")
        return attach(ids[0], quote=(fragments[0], quote))

    for rule in RULES.values():
        gateway.key_by(rule.prompt, subject_name)
    gateway.handle(ORPH.prompt, handle)
    return gateway


@dataclass
class ShopProject:
    project: Neo4jProject
    root: Path
    factory: GatewayFactory
    stores: FileStores = field(init=False)

    def __post_init__(self) -> None:
        self.stores = open_file_stores(self.root)

    async def build(
        self,
        snapshot: int,
        *,
        failure: Exception | None = None,
        judge_calls: bool = True,
        ontology: Ontology | None = None,
    ) -> tuple[BuildResult, ScriptedGateway]:
        answers = shop_answers(snapshot)
        gateway = answers.install(self.factory(live_llm_config()))
        if judge_calls:
            shop_judge(gateway, failure)
        else:
            for rule in RULES.values():
                gateway.key_by(rule.prompt, subject_name)
        ctx = BuildContext(
            project_id=self.project.project_id,
            ontology=ontology or self.project.ontology,
            driver=self.project.driver,
            gateway=gateway,
            registry=self.stores.registry,
            corpus=shop_corpus(snapshot, self.root),
            extraction=self.stores.extraction,
            builds=self.stores.builds,
            options=answers.options,
            database=self.project.database,
            settings=BuildSettings(inference=True),
        )
        return await run_build(ctx), gateway

    async def report(self, result: BuildResult) -> InferenceReport:
        payload = await self.stores.builds.get_artifact(self.project.project_id, result.build_id, "infer")
        assert payload is not None
        return InferenceReport.model_validate(payload)

    async def refs(self) -> tuple[str, ...]:
        rows = await self.project.run(
            "MATCH (s:SpecNode)-[r]->(t:SpecNode) WHERE r.group_id = $p AND r.basis = 'inferred' "
            "RETURN s.spec_id + '|' + type(r) + '|' + t.spec_id AS ref ORDER BY ref",
            p=self.project.project_id,
        )
        return tuple(row["ref"] for row in rows)


@pytest.fixture
def shop(neo4j_project: Neo4jProject, scripted_gateway: GatewayFactory, tmp_path: Path) -> ShopProject:
    return ShopProject(neo4j_project, tmp_path / "stores", scripted_gateway)


async def test_shop_build_infers_and_a_no_change_update_makes_no_judge_call(
    shop: ShopProject, neo4j_project: Neo4jProject
) -> None:
    result, gateway = await shop.build(1)
    assert (result.status, result.failed_step, result.error_code) == ("partially_succeeded", None, None)
    assert result.verification is not None and result.verification.ok
    report = await shop.report(result)
    assert tuple(rule.rule for rule in report.rules) == RULE_IDS  # FR-INF-01: each rule once per build
    orphan = by_rule(report)[ORPH.id]
    assert counts(orphan, "subjects", "judged", "accepted", "declined", "edges_added") == (6, 6, 2, 4, 2)
    assert sorted(judged(gateway)) == sorted(SHOP_VERDICTS)  # each subject once
    refs = await shop.refs()
    assert len(refs) == 2 and all("|PART_OF|" in ref and "k|" not in ref for ref in refs)
    assert result.spec_changes.inferred_added == refs and result.spec_changes.inferred_removed == ()
    assert "infer" in result.metrics.llm and "infer" in result.metrics.duration_s
    edges = await inferred(neo4j_project, neo4j_project.project_id)
    assert {props["last_build_id"] for props in edges.values()} == {result.build_id}
    assert {props["judge_model"] for props in edges.values()} == {live_llm_config().model_for("judge")}

    again, gateway = await shop.build(1, judge_calls=False)
    assert again.mode == "update" and (again.status, again.failed_step) == (result.status, None)
    assert judged(gateway) == []
    assert (again.spec_changes.inferred_added, again.spec_changes.inferred_removed) == ((), ())
    report = await shop.report(again)
    assert tuple(rule.rule for rule in report.rules) == RULE_IDS
    assert counts(by_rule(report)[ORPH.id], "subjects", "reused", "judged") == (6, 6, 0)
    assert await inferred(neo4j_project, neo4j_project.project_id) == edges


async def test_shop_update_with_a_failing_judge_succeeds_and_keeps_the_inferred_edges(
    shop: ShopProject, neo4j_project: Neo4jProject
) -> None:
    await shop.build(1)
    edges = await inferred(neo4j_project, neo4j_project.project_id)
    assert len(edges) == 2
    result, gateway = await shop.build(2, failure=LLMError("exhausted"))
    assert (result.failed_step, result.error_code) == (None, None)
    assert result.status == "partially_succeeded"
    report = await shop.report(result)
    orphan = by_rule(report)[ORPH.id]
    assert orphan.failed == len(judged(gateway)) > 0 and orphan.judged == 0
    assert (orphan.error_code, report.error_code) == (None, None)
    assert await inferred(neo4j_project, neo4j_project.project_id) == edges
    assert (result.spec_changes.inferred_added, result.spec_changes.inferred_removed) == ((), ())


@pytest.mark.parametrize(
    "error",
    [LLMFatalError(), InfrastructureUnavailableError("llm_unreachable"), ConformanceError("test_conformance_failure")],
    ids=["llm-fatal", "infrastructure-unavailable", "conformance"],
)
async def test_shop_update_fails_on_a_fatal_judge_error_with_the_inferred_edges_intact(
    shop: ShopProject, neo4j_project: Neo4jProject, error: Exception
) -> None:
    first, _ = await shop.build(1)
    edges = await inferred(neo4j_project, neo4j_project.project_id)
    with pytest.raises(type(error)):
        await shop.build(2, failure=error)
    assert await inferred(neo4j_project, neo4j_project.project_id) == edges
    pid = neo4j_project.project_id
    record = await shop.stores.builds.latest_build(pid)
    assert record is not None and record.build_id != first.build_id
    assert record.status == "failed" and record.progress.get("verify") != "done"
    stored = await shop.stores.builds.get_result(pid, record.build_id)
    assert stored is not None and (stored.status, stored.failed_step) == ("failed", "infer")


async def test_shop_no_change_update_leaves_the_backlog_and_a_changing_update_works_it_off(
    shop: ShopProject, neo4j_project: Neo4jProject
) -> None:
    capped = only(cap=2)
    first, gateway = await shop.build(1, ontology=capped)
    assert len(judged(gateway)) == 2
    assert counts(by_rule(await shop.report(first))[ORPH.id], "subjects", "judged", "deferred") == (6, 2, 4)
    edges = await inferred(neo4j_project, neo4j_project.project_id)

    again, gateway = await shop.build(1, judge_calls=False, ontology=capped)
    assert again.mode == "update" and judged(gateway) == []
    assert counts(by_rule(await shop.report(again))[ORPH.id], "reused", "judged", "deferred") == (2, 0, 4)
    assert await inferred(neo4j_project, neo4j_project.project_id) == edges

    changed, gateway = await shop.build(2, ontology=capped)
    assert changed.spec_changes.model_dump() != dict.fromkeys(changed.spec_changes.model_dump(), ())
    orphan = by_rule(await shop.report(changed))[ORPH.id]
    assert len(judged(gateway)) == orphan.judged == 2
    assert orphan.reused + orphan.judged + orphan.deferred == orphan.subjects
