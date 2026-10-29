"""Shared prompt and structured-response helpers for PDF enrichers."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ValidationError

from llm.client import Client
from llm.content import Attachment

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL)


def load_prompt(prompt_id: str) -> str:
    return (_PROMPTS_DIR / f"{prompt_id}.md").read_text(encoding="utf-8")


def render_prompt(template: str, **values: str) -> str:
    """Replace named placeholders without interpreting JSON braces in prompts."""
    for name, value in values.items():
        template = template.replace(f"{{{name}}}", value)
    return template


def request_json[T: BaseModel](
    client: Client,
    *,
    prompt: str,
    model: str,
    max_tokens: int,
    attachment: Attachment,
    schema: type[T],
    attempts: int,
) -> T | None:
    for _ in range(attempts):
        raw = client.message(
            prompt,
            model=model,
            max_tokens=max_tokens,
            attachments=[attachment],
        )
        try:
            return schema.model_validate_json(_strip_json_fence(raw))
        except (ValidationError, json.JSONDecodeError):
            continue
    return None


def _strip_json_fence(text: str) -> str:
    match = _JSON_FENCE.match(text)
    return match.group("body") if match else text.strip()
