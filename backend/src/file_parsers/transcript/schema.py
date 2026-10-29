from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class TranscriptDocument(BaseModel):
    source_path: Path = Field(description="Path to the sorce Document")
    text: str = Field(description="Transcript text")
    clean_requirements: str = Field(description="requirements prompt", default="")
