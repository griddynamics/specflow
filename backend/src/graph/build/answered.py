"""Answered elsewhere: Conform's check for open questions another part of the corpus answers."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from graph.build.consolidate import DECISION_KIND, evidence_hash, evidence_matches
from graph.build.models import AnsweredElsewhere, FragmentRec
from graph.build.ports import ExtractionStore
from graph.build.prompts import BREADCRUMB_SEPARATOR
from graph.build.terms import fragment_vectors
from graph.build.vectors import VectorCache, get_many, put_many
from graph.core.errors import LLMError, LLMFatalError
from graph.core.llm import Gateway
from graph.core.llm.judge import Evidence, Verdict, judge_messages, quotes_valid
from graph.core.ontology import AnsweredElsewhereSpec, Ontology
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("graph.build.answered")

ANSWERED_SEGMENT: Final = "answered"
"""The middle segment of an answered-elsewhere memory key."""
ANSWERED_CLASS: Final = "small"
"""The call class of an answered-elsewhere call (``run`` counts it as the ``answered_check`` judge)."""
QUOTE_MAX_CHARS: Final = 600
"""Longest question quote shown to the judge."""
_TABLE_SEPARATOR: Final = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
"""A markdown table's separator line (``|---|---|``), the line under its header row."""
DEFAULT_HEADER: Final = re.compile(r"\b(default|assumption|fallback|if unanswered)", re.IGNORECASE)
"""A table header cell whose column holds what applies while the row's question stays open."""
NEIGHBOURS: Final = 1
"""Fragments on each side of a question's evidence fragment (plan order, same document) that are no
candidates."""

Answer = Literal["answered", "partly", "open"]


class AnswerVerdict(BaseModel):
    """The judge's answer on one question (prompt ``judge_answered_elsewhere``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: Answer
    fragment: str | None = Field(default=None, description="id of the candidate fragment holding the answer")
    quote: str | None = Field(default=None, description="the passage stating the answer, verbatim from that fragment")
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True)
class Question:
    """A node the check judges: identity key, name, quote (its defining text), search text and its
    evidence fragments (they and their neighbours are no candidates)."""

    key: str
    name: str
    quote: str
    text: str
    fragments: frozenset[str]


@dataclass(frozen=True)
class Context:
    """Where a question sits: the heading path of its first evidence fragment (``section``) and, when its quote is a row
    of a table there, that table's header row (``columns``), the row's first cell (``row``, such as its id) and its cell
    under a header naming a default (``default``: ``DEFAULT_HEADER``, such as "Default if unanswered").
    """

    section: str = ""
    columns: str | None = None
    row: str | None = None
    default: str | None = None


@dataclass(frozen=True)
class Passage:
    """A candidate fragment: the plan's record."""

    rec: FragmentRec


@dataclass(frozen=True)
class AnsweredCheck:
    """What the check found: the questions to drop (``dropped``, by key) and its counts. ``judged``:
    questions with a verdict decision this build, fresh calls (failed ones too) and stored verdicts;
    ``partly``: answered ``partly`` (kept)."""

    dropped: tuple[AnsweredElsewhere, ...] = ()
    judged: int = 0
    partly: int = 0
    reused: int = 0
    expired: int = 0
    failed: int = 0
    over_cap: int = 0
    candidates: int = 0
    """Questions with at least one candidate fragment."""
    kept_open: int = 0
    """Questions answered at the floor but kept: a table row with a default whose answer does not name
    the row (``names_row``)."""

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(item.key for item in self.dropped)


@dataclass(frozen=True)
class _Stored:
    verdict: AnswerVerdict
    fragment_hash: str | None


class AnsweredMemory:
    """Stored answered-elsewhere verdicts of one project (module docstring, "Memory")."""

    def __init__(self, store: ExtractionStore, project_id: str, prompt: str, model: str) -> None:
        self._store = store
        self._project_id = project_id
        self.fingerprint = f"{prompt}|{model}"

    def key(self, question: str) -> str:
        return f"{self._project_id}|{ANSWERED_SEGMENT}|{question}"

    async def recall(self, evidence: Mapping[str, str]) -> tuple[dict[str, _Stored], int]:
        """Valid stored verdicts by question key (``evidence``: each question's current evidence hash)
        and the number that expired (hash differs); an entry without a hash or that does not parse is a
        miss."""

        wanted = {self.key(question): question for question in sorted(evidence)}
        found = await get_many(self._store, DECISION_KIND, list(wanted), self.fingerprint)
        result: dict[str, _Stored] = {}
        expired = 0
        for store_key, payload in found.items():
            question = wanted[store_key]
            match = evidence_matches(payload, evidence[question])
            if match is False:
                expired += 1
                continue
            stored = _parse(payload) if match else None
            if stored is not None:
                result[question] = stored
        return result, expired

    async def write(
        self, verdicts: Mapping[str, tuple[AnswerVerdict, str | None]], evidence: Mapping[str, str]
    ) -> None:
        """Store fresh verdicts by question key, each with its cited fragment's content hash and the
        question's evidence hash."""

        await put_many(
            self._store,
            DECISION_KIND,
            self.fingerprint,
            {
                self.key(question): {
                    "answer": verdict.answer,
                    "fragment": fragment_hash,
                    "quote": verdict.quote,
                    "confidence": verdict.confidence,
                    "evidence": evidence[question],
                }
                for question, (verdict, fragment_hash) in sorted(verdicts.items())
            },
        )


def _parse(payload: Mapping[str, Any]) -> _Stored | None:
    try:
        verdict = AnswerVerdict.model_validate(
            {key: payload.get(key) for key in ("answer", "quote", "confidence")} | {"fragment": None}
        )
    except ValueError:
        return None
    fragment_hash = payload.get("fragment")
    return _Stored(verdict, fragment_hash if isinstance(fragment_hash, str) else None)


def question_hash(question: Question, candidates: Sequence[Passage], context: Context | None = None) -> str:
    """The evidence hash of one question's verdict: its key, name, quote and context and its
    candidates' content hashes (sorted); an edit to the question, its context or any candidate
    expires the verdict."""

    shown = context or Context()
    return evidence_hash(
        [
            question.key,
            question.name,
            question.quote,
            shown.section,
            shown.columns or "",
            shown.row or "",
            shown.default or "",
            sorted(passage.rec.content_hash for passage in candidates),
        ]
    )


def answered_payload(
    question: Question, candidates: Sequence[Passage], context: Context | None = None
) -> dict[str, Any]:
    """The judge payload of one question (``judge_messages``): the question (with its ``section`` and,
    for a table row, the table's ``columns``) and the candidate fragments."""

    shown = context or Context()
    asked: dict[str, Any] = {"name": question.name, "quote": question.quote[:QUOTE_MAX_CHARS]}
    asked["section"] = shown.section
    if shown.columns:
        asked["columns"] = shown.columns
    return {
        "question": asked,
        "fragments": {passage.rec.fragment_id: passage.rec.text for passage in candidates},
    }


def question_context(question: Question, passages: Sequence[Passage]) -> Context:
    """The question's ``Context`` from its first evidence fragment in ``passages`` (plan order): that fragment's heading
    path and, when the first line of its quote is a table row there (not the header), the header row of that table; an
    empty context when no evidence fragment is found.
    """

    found = next((passage.rec for passage in passages if passage.rec.fragment_id in question.fragments), None)
    if found is None:
        return Context()
    section = BREADCRUMB_SEPARATOR.join(part.strip() for part in found.heading_path if part.strip())
    table = _table_row(found.text, question.quote)
    if table is None:
        return Context(section)
    header, row = table
    titles, values = _split_row(header), _split_row(row)
    default = next(
        (value for title, value in zip(titles, values, strict=False) if DEFAULT_HEADER.search(title) and value),
        None,
    )
    key = values[0] if values and values[0] else None
    return Context(section, header, key, default)


def _table_row(text: str, quote: str) -> tuple[str, str] | None:
    """The header row of the markdown table whose row holds the first line of ``quote`` and that row,
    else None (also when the quote is the header itself)."""

    needle = _words(next((line for line in quote.splitlines() if line.strip()), ""))
    if not needle:
        return None
    lines = text.splitlines()
    row = next((index for index, line in enumerate(lines) if _is_row(line) and needle in _words(line)), None)
    if row is None:
        return None
    top = row
    while top > 0 and _is_row(lines[top - 1]):
        top -= 1
    if top + 1 < row and _TABLE_SEPARATOR.match(lines[top + 1]):
        return lines[top].strip(), lines[row].strip()
    return None


def _is_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def _words(line: str) -> str:
    """A table row or quote line as its cell texts, lower case, one space between words."""

    return " ".join(line.replace("|", " ").split()).lower()


def _split_row(line: str) -> list[str]:
    """The cells of a markdown table row, stripped (outer pipes dropped)."""

    inner = line.strip()
    inner = inner.removeprefix("|").removesuffix("|")
    return [cell.strip() for cell in inner.split("|")]


async def candidate_passages(
    questions: Sequence[Question], passages: Sequence[Passage], spec: AnsweredElsewhereSpec, vectors: VectorCache
) -> dict[str, list[Passage]]:
    """Each question's candidate fragments (module docstring), by question key; a question without
    any is absent. Embeds nothing that the vector store already holds."""

    if not questions or not passages:
        return {}
    by_id = await fragment_vectors([passage.rec for passage in passages], vectors)
    shown = [passage for passage in passages if passage.rec.fragment_id in by_id]
    if not shown:
        return {}
    matrix = _unit_rows([by_id[passage.rec.fragment_id] for passage in shown])
    texts = await vectors.mapping(question.text for question in questions)
    result: dict[str, list[Passage]] = {}
    for question in questions:
        scores = matrix @ _unit_rows([texts[question.text]])[0]
        own = own_context(question, passages)
        ranked = sorted(
            (
                (-float(scores[index]), index)
                for index, passage in enumerate(shown)
                if passage.rec.fragment_id not in own and float(scores[index]) >= spec.min_cosine
            ),
        )
        chosen = [shown[index] for _, index in ranked[: spec.candidates]]
        if chosen:
            result[question.key] = chosen
    return result


def own_context(question: Question, passages: Sequence[Passage]) -> set[str]:
    """The fragment ids that are no candidates for ``question``: its evidence fragments, the ``NEIGHBOURS`` on each side
    of each in ``passages`` (plan order) within the same document, and the fragments of that document under the same
    heading path as one of them (its own section).
    """

    position = {passage.rec.fragment_id: index for index, passage in enumerate(passages)}
    own = set(question.fragments)
    sections: set[tuple[str, tuple[str, ...]]] = set()
    for fragment_id in question.fragments:
        index = position.get(fragment_id)
        if index is None:
            continue
        document_id = passages[index].rec.document_id
        sections.add((document_id, passages[index].rec.heading_path))
        for near in range(max(index - NEIGHBOURS, 0), min(index + NEIGHBOURS + 1, len(passages))):
            if passages[near].rec.document_id == document_id:
                own.add(passages[near].rec.fragment_id)
    if sections:
        own.update(
            passage.rec.fragment_id
            for passage in passages
            if (passage.rec.document_id, passage.rec.heading_path) in sections
        )
    return own


def _unit_rows(rows: Sequence[Any]) -> np.ndarray:
    matrix = np.asarray(rows, dtype=np.float64)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit: np.ndarray = matrix / norms
    return unit


async def answered_elsewhere(
    questions: Sequence[Question],
    passages: Sequence[Passage],
    *,
    spec: AnsweredElsewhereSpec,
    gateway: Gateway,
    ontology: Ontology,
    vectors: VectorCache,
    memory: AnsweredMemory | None = None,
) -> AnsweredCheck:
    """Run the check (module docstring) over ``questions`` with ``passages`` (the groundable fragments
    of the plan, in plan order) as the candidate pool."""

    ordered = sorted(questions, key=lambda question: question.key)
    candidates = await candidate_passages(ordered, passages, spec, vectors)
    judged = [question for question in ordered if question.key in candidates]
    contexts = {question.key: question_context(question, passages) for question in judged}
    evidence = {
        question.key: question_hash(question, candidates[question.key], contexts[question.key]) for question in judged
    }
    stored, expired = await memory.recall(evidence) if memory is not None and judged else ({}, 0)
    decided: dict[str, tuple[AnswerVerdict, Passage | None]] = {}
    for question in judged:
        found = stored.get(question.key)
        if found is not None:
            decided[question.key] = (found.verdict, _by_hash(candidates[question.key], found.fragment_hash))
    pending = [question for question in judged if question.key not in decided]
    asked, over_cap = pending[: spec.cap], len(pending) - min(len(pending), spec.cap)
    answers = await _ask_all(asked, candidates, spec, gateway, ontology, contexts=contexts)
    fresh: dict[str, tuple[AnswerVerdict, str | None]] = {}
    failed = 0
    for question, answer in zip(asked, answers, strict=True):
        if answer is None:
            failed += 1
            continue
        cited = _by_id(candidates[question.key], answer.fragment)
        decided[question.key] = (answer, cited)
        fresh[question.key] = (answer, cited.rec.content_hash if cited is not None else None)
    if memory is not None and fresh:
        await memory.write(fresh, evidence)
    dropped: list[AnsweredElsewhere] = []
    partly = 0
    kept_open = 0
    for key, (verdict, cited) in sorted(decided.items()):
        partly += int(verdict.answer == "partly")
        listed = _dropped(key, verdict, cited, spec.floor)
        if listed is not None and not names_row(listed.quote, contexts[key]):
            kept_open += 1
        elif listed is not None:
            dropped.append(listed)
    check = AnsweredCheck(
        dropped=tuple(dropped),
        judged=len(decided) + failed,
        partly=partly,
        reused=len(decided) - len(fresh),
        expired=expired,
        failed=failed,
        over_cap=over_cap,
        candidates=len(judged),
        kept_open=kept_open,
    )
    log_event(
        logger,
        INFO,
        "graph.conform.answered_elsewhere",
        questions=len(ordered),
        with_candidates=check.candidates,
        judged=check.judged,
        calls=len(asked),
        reused=check.reused,
        expired=check.expired,
        failed=check.failed,
        over_cap=check.over_cap,
        dropped=len(check.dropped),
        partly=check.partly,
        kept_open=check.kept_open,
    )
    return check


def names_row(quote: str, context: Context) -> bool:
    """Whether an answer ``quote`` may drop a question with ``context``: always, unless the question is
    a table row with a default (``Context.default``); then only when the quote names the row's first
    cell (case and spacing aside)."""

    if not context.default:
        return True
    return bool(context.row) and _words(context.row or "") in _words(quote)


def _dropped(key: str, verdict: AnswerVerdict, cited: Passage | None, floor: float) -> AnsweredElsewhere | None:
    """The listing of question ``key`` when its verdict drops it: ``answered`` at or above ``floor``
    with a quote found in the cited candidate fragment; else None."""

    if verdict.answer != "answered" or verdict.confidence < floor or cited is None or not verdict.quote:
        return None
    fragment_id = cited.rec.fragment_id
    proof = Verdict(confidence=verdict.confidence, evidence=[Evidence(fragment_id=fragment_id, quote=verdict.quote)])
    if not quotes_valid(proof, {fragment_id: cited.rec.text}):
        return None
    return AnsweredElsewhere(key=key, fragment=fragment_id, quote=verdict.quote)


def _by_id(candidates: Iterable[Passage], fragment_id: str | None) -> Passage | None:
    return next((passage for passage in candidates if passage.rec.fragment_id == fragment_id), None)


def _by_hash(candidates: Iterable[Passage], content_hash: str | None) -> Passage | None:
    return next((passage for passage in candidates if passage.rec.content_hash == content_hash), None)


async def _ask_all(
    questions: Sequence[Question],
    candidates: Mapping[str, Sequence[Passage]],
    spec: AnsweredElsewhereSpec,
    gateway: Gateway,
    ontology: Ontology,
    *,
    contexts: Mapping[str, Context] | None = None,
) -> list[AnswerVerdict | None]:
    """One call per question, concurrently, in order, each with its ``contexts`` entry (none: an empty
    ``Context``); a failed call is None (logged with its error code; ``LLMFatalError`` propagates and
    cancels the others)."""

    async def ask(question: Question) -> tuple[AnswerVerdict | None, str | None]:
        payload = answered_payload(question, candidates[question.key], (contexts or {}).get(question.key))
        messages = judge_messages(ontology, spec.prompt, payload)
        try:
            verdict = await gateway.structured(ANSWERED_CLASS, None, messages, AnswerVerdict, prompt=spec.prompt)
        except LLMFatalError:
            raise
        except LLMError as error:
            return None, error.code
        return verdict, None

    if not questions:
        return []
    tasks = [asyncio.ensure_future(ask(question)) for question in questions]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    failures = sorted({code for _, code in results if code is not None})
    if failures:
        log_event(
            logger,
            WARNING,
            "graph.conform.answered_failed",
            failed=sum(1 for _, code in results if code is not None),
            calls=len(results),
            error_codes=failures,
        )
    return [verdict for verdict, _ in results]


def passages_of(fragments: Iterable[FragmentRec]) -> list[Passage]:
    """The candidate pool: each fragment, in the order given (plan order)."""

    return [Passage(fragment) for fragment in fragments]
