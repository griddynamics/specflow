"""
Extract JSON from messy LLM text, validate with Pydantic, optionally repair via OpenRouter (LLM_LOW).

General utility: no domain logic beyond model_type validation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.services.llm_apis.open_router_api import (
    openrouter_first_model_from_llm_low_csv,
    openrouter_one_shot_text,
)

logger = logging.getLogger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)

# Max OpenRouter repair calls after initial parse failure (up to 2 retries).
DEFAULT_JSON_REPAIR_LLM_ATTEMPTS: int = 2


@dataclass(frozen=True)
class JsonParseRepairResult(Generic[TModel]):
    """Structured parse outcome; ``repair_returned_empty`` if any repair HTTP call yielded no assistant text."""

    value: Optional[TModel]
    repair_returned_empty: bool = False

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _balanced_object_at(text: str, start: int) -> Optional[str]:
    """Return substring from first `{` at or after start through matching `}` (strings/escapes aware)."""
    i = text.find("{", start)
    if i < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for j in range(i, len(text)):
        c = text[j]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
    return None


def extract_json_string_candidates(text: str) -> list[str]:
    """
    Ordered unique candidates to try with json.loads + Pydantic.

    1. Whole stripped text if it looks like an object.
    2. Balanced `{...}` inside each ``` / ```json fence.
    3. Balanced `{...}` from first `{` in the full text.
    4. Greedy `{` … `}` span (last resort; can be wrong on nested extras).
    """
    if not text or not str(text).strip():
        return []
    raw = str(text).strip()
    seen: set[str] = set()
    out: list[str] = []

    def add(s: str) -> None:
        s = s.strip()
        if len(s) < 2 or not s.startswith("{") or not s.endswith("}"):
            return
        if s in seen:
            return
        seen.add(s)
        out.append(s)

    if raw.startswith("{") and raw.endswith("}"):
        add(raw)

    for m in _FENCE_RE.finditer(raw):
        inner = m.group(1).strip()
        bal = _balanced_object_at(inner, 0)
        if bal:
            add(bal)

    bal = _balanced_object_at(raw, 0)
    if bal:
        add(bal)

    loose = re.search(r"\{[\s\S]*\}", raw)
    if loose:
        add(loose.group(0).strip())

    return out


def try_validate_json_model(candidates: list[str], model_type: type[TModel]) -> Optional[TModel]:
    """Try json.loads + model_type.model_validate on each candidate."""
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        try:
            return model_type.model_validate(data)
        except ValidationError:
            continue
    return None


def _repair_system_prompt(schema_json: str) -> str:
    return (
        "You fix malformed JSON. Reply with a single valid JSON object only — no markdown, "
        "no code fences, no commentary. The output must satisfy this JSON Schema:\n"
        f"{schema_json}"
    )


def _repair_user_prompt(broken_snippet: str) -> str:
    return (
        "Fix the following text into one valid JSON object that matches the schema from the system message. "
        "Preserve intended field names and values when obvious.\n\n---\n\n"
        f"{broken_snippet[:24_000]}"
    )


async def parse_json_with_pydantic_and_repair(
    raw_text: str,
    model_type: type[TModel],
    settings: Optional[Settings],
    *,
    max_llm_repairs: int = DEFAULT_JSON_REPAIR_LLM_ATTEMPTS,
    log: Optional[logging.Logger] = None,
) -> JsonParseRepairResult[TModel]:
    """
    Extract JSON candidates, validate with Pydantic; on failure optionally call OpenRouter to repair.

    Repair uses ``settings.OPENROUTER_API_KEY``, ``settings.OPENROUTER_BASE_URL``, first model in
    ``settings.LLM_LOW``, and ``settings.OPENROUTER_APP_NAME`` (X-Title) when set.

    Runs only when the OpenRouter key is set and max_llm_repairs > 0. Each repair uses
    ``asyncio.to_thread`` around the sync httpx client.

    ``repair_returned_empty`` is True if any repair call returned no assistant text (callers may
    treat that as “keep inputs unchanged”, e.g. full MCP candidate list).
    """
    log = log or logger
    candidates = extract_json_string_candidates(raw_text)
    parsed = try_validate_json_model(candidates, model_type)
    if parsed is not None:
        return JsonParseRepairResult(parsed, repair_returned_empty=False)

    api_key = (settings.OPENROUTER_API_KEY or "").strip() if settings else ""
    if not api_key or max_llm_repairs <= 0:
        log.debug("json_llm_repair: no repair (missing OpenRouter API key or max_llm_repairs=0)")
        return JsonParseRepairResult(None, repair_returned_empty=False)

    model_id = openrouter_first_model_from_llm_low_csv(settings.LLM_LOW if settings else "")

    schema_json = json.dumps(model_type.model_json_schema(), indent=2)[:12_000]
    system = _repair_system_prompt(schema_json)
    base_url = (settings.OPENROUTER_BASE_URL or "").strip() or None
    app_name = (settings.OPENROUTER_APP_NAME or "").strip() if settings else None

    working = raw_text[:32_000]
    repair_returned_empty = False
    for attempt in range(max_llm_repairs):
        try:
            fixed = await asyncio.to_thread(
                openrouter_one_shot_text,
                api_key=api_key,
                model=model_id,
                user_message=_repair_user_prompt(working),
                system=system,
                max_tokens=2048,
                base_url=base_url,
                app_name=app_name,
                log=log,
            )
        except Exception as e:
            log.warning("json_llm_repair: OpenRouter repair attempt %s failed: %s", attempt + 1, e)
            continue

        if not (fixed or "").strip():
            log.warning(
                "json_llm_repair: OpenRouter repair attempt %s returned empty body",
                attempt + 1,
            )
            repair_returned_empty = True
            continue

        new_candidates = extract_json_string_candidates(fixed)
        parsed = try_validate_json_model(new_candidates, model_type)
        if parsed is not None:
            log.info("json_llm_repair: repaired JSON on attempt %s", attempt + 1)
            return JsonParseRepairResult(parsed, repair_returned_empty=False)
        working = fixed or working

    return JsonParseRepairResult(None, repair_returned_empty=repair_returned_empty)
