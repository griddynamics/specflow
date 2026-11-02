"""Judge runner and verdict checks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from graph.core.errors import ConfigurationError, LLMError, LLMErrorReason, LLMFatalError
from graph.core.llm.gateway import Gateway, untrusted
from graph.core.ontology import Ontology
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("graph.core.llm.judge")

FRAGMENTS_KEY: Final = "fragments"
MIN_QUOTE_CHARS: Final = 8
"""Non-space characters a quote needs, unless it is the whole (shorter) fragment text."""
PAYLOAD_LABEL: Final = "payload"
EVIDENCE_RULE: Final = (
    "Answer with the JSON object the schema describes. Set confidence between 0 and 1. For evidence, "
    "cite the fragment id and a quote copied verbatim from that fragment; cite nothing you cannot quote."
)


class Evidence(BaseModel):
    """One cited quote: verbatim text from the fragment ``fragment_id``."""

    model_config = ConfigDict(frozen=True)

    fragment_id: str
    quote: str


class Verdict(BaseModel):
    """Base of every verdict type: confidence and the quotes it rests on."""

    model_config = ConfigDict(frozen=True)

    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence]

    _prompt_ref: str | None = PrivateAttr(default=None)

    @property
    def prompt_ref(self) -> str | None:
        return self._prompt_ref

    def attach_prompt_ref(self, prompt: str) -> None:
        """Record the prompt (``name.version``) whose call gave this verdict."""

        self._prompt_ref = prompt


class JudgeFailure(BaseModel):
    """A judgement without a verdict: the call failed with a non-fatal ``LLMError``."""

    model_config = ConfigDict(frozen=True)

    prompt_ref: str
    reason: LLMErrorReason
    code: str


V = TypeVar("V", bound=Verdict)


def _normalise(text: str) -> str:
    return " ".join(text.split())


def quote_in(quote: str, text: str) -> bool:
    """Whether ``quote``, whitespace-normalised and not empty, is a substring of ``text``."""

    found = _normalise(quote)
    return bool(found) and found in _normalise(text)


def quotes_valid(verdict: Verdict, fragments: Mapping[str, str]) -> bool:
    """True when the verdict cites at least one quote and every quote, whitespace-normalised, is a substring of its
    fragment's text with at least ``MIN_QUOTE_CHARS`` non-space characters, or the whole text of a fragment shorter than
    that; an unknown fragment id fails the verdict.
    """

    if not verdict.evidence:
        return False
    for item in verdict.evidence:
        text = fragments.get(item.fragment_id)
        if text is None:
            return False
        if not quote_in(item.quote, text):
            return False
        quote, whole = _normalise(item.quote), _normalise(text)
        if _non_space(quote) < MIN_QUOTE_CHARS and quote != whole:
            return False
    return True


def _non_space(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def meets_floor(verdict: Verdict, floor: float) -> bool:
    return verdict.confidence >= floor


def _user_message(payload: Mapping[str, Any]) -> str:
    fragments = payload.get(FRAGMENTS_KEY, {})
    if not isinstance(fragments, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in fragments.items()
    ):
        raise ConfigurationError("judge_payload_invalid", key=FRAGMENTS_KEY)
    rest = {key: value for key, value in payload.items() if key != FRAGMENTS_KEY}
    blocks = [untrusted(PAYLOAD_LABEL, json.dumps(rest, sort_keys=True, ensure_ascii=False, indent=1, default=str))]
    blocks.extend(untrusted(fragment_id, fragments[fragment_id]) for fragment_id in sorted(fragments))
    return "\n\n".join(blocks)


def judge_messages(ontology: Ontology, prompt: str, payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """The messages of one judgement (module docstring), as ``JudgeRunner.judge`` sends them; the cost
    estimate renders them too."""

    return [
        {"role": "system", "content": f"{ontology.judge_prompt(prompt).strip()}\n\n{EVIDENCE_RULE}"},
        {"role": "user", "content": _user_message(payload)},
    ]


class JudgeRunner:
    """Runs judgements through the gateway with prompts from the ontology."""

    def __init__(self, gateway: Gateway, ontology: Ontology) -> None:
        self._gateway = gateway
        self._ontology = ontology

    @property
    def concurrency(self) -> int:
        """The gateway's request concurrency (``LLMConfig.concurrency``): how many judge calls it runs
        at once."""

        return self._gateway.config.concurrency

    async def judge(self, prompt: str, payload: Mapping[str, Any], verdict_type: type[V]) -> V | JudgeFailure:
        """One structured ``judge`` call; a non-fatal ``LLMError`` becomes ``JudgeFailure``."""

        messages = judge_messages(self._ontology, prompt, payload)
        try:
            verdict = await self._gateway.structured("judge", None, messages, verdict_type, prompt=prompt)
        except LLMFatalError:
            raise
        except LLMError as error:
            log_event(logger, WARNING, "graph.llm.judge_failed", prompt=prompt, reason=error.reason, code=error.code)
            return JudgeFailure(prompt_ref=prompt, reason=error.reason, code=error.code)
        verdict.attach_prompt_ref(prompt)
        return verdict
