from __future__ import annotations

import base64
import sys
from collections.abc import Sequence
from typing import Any, cast

from anthropic import Anthropic, not_given, omit
from anthropic.types import MessageParam, TextBlock

from llm.content import (
    IMAGE_MEDIA_TYPE_PREFIX,
    PDF_MEDIA_TYPE,
    TEXT_MEDIA_TYPE,
    Attachment,
)


def _base64_source(attachment: Attachment) -> dict[str, str]:
    return {
        "type": "base64",
        "media_type": attachment.media_type,
        "data": base64.standard_b64encode(attachment.data).decode("utf-8"),
    }


def _to_content_block(attachment: Attachment) -> dict[str, Any]:
    if attachment.media_type.startswith(IMAGE_MEDIA_TYPE_PREFIX):
        return {"type": "image", "source": _base64_source(attachment)}

    if attachment.media_type == PDF_MEDIA_TYPE:
        source: dict[str, str] = _base64_source(attachment)
    elif attachment.media_type == TEXT_MEDIA_TYPE:
        # Plain-text documents are sent verbatim, not base64-encoded.
        source = {
            "type": "text",
            "media_type": attachment.media_type,
            "data": attachment.data.decode("utf-8"),
        }
    else:
        raise ValueError(f"Unsupported attachment media type: {attachment.media_type!r}")

    block: dict[str, Any] = {"type": "document", "source": source}
    if attachment.name is not None:
        block["title"] = attachment.name
    return block


class Client:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        auth_token: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int = 2,
    ) -> None:
        self._client = Anthropic(
            api_key=api_key,
            auth_token=auth_token,
            base_url=base_url,
            timeout=timeout if timeout is not None else not_given,
            max_retries=max_retries,
        )
        # Running totals populated from each response.usage so callers can
        # report token consumption per pipeline stage. Reset via reset_usage().
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0

    def reset_usage(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0

    def message(
        self,
        prompt: str,
        model: str,
        system: str | None = None,
        max_tokens: int = 1024,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        message_content: str | list[dict[str, Any]] = prompt
        if attachments:
            message_content = [_to_content_block(a) for a in attachments]
            message_content.append({"type": "text", "text": prompt})

        messages: list[MessageParam] = [cast(MessageParam, {"role": "user", "content": message_content})]
        response = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
            system=system if system is not None else omit,
        )
        self._accumulate_usage(response.usage)

        return "".join(block.text for block in response.content if isinstance(block, TextBlock))

    def _accumulate_usage(self, usage: object) -> None:
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_input_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_creation_input_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0


if __name__ == "__main__":
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv()

    # Resolved from this file, so the demo works from any working directory.
    pdf_path = Path(__file__).resolve().parents[2] / "tests/data/specs/extraction.pdf"

    client = Client()
    answer = client.message(
        "What is this document about? Answer in two sentences.",
        model="openrouter/glm-5.2",
        attachments=[Attachment.from_path(pdf_path)],
    )
    sys.stdout.write(f"{answer}\n")
