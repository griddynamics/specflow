from enum import Enum
from typing import Optional

from pydantic import BaseModel


class SpecReadiness(str, Enum):
    """Single axis: NOT_READY < LOCAL_ONLY < INTEGRATION_TESTS_READY."""
    NOT_READY = "NOT_READY"
    LOCAL_ONLY = "LOCAL_ONLY"
    INTEGRATION_TESTS_READY = "INTEGRATION_TESTS_READY"


class SpecificationIndexRequest(BaseModel):
    spec_path: str
    outputs_dir: str = "specflow"
    session_id: Optional[str] = None


class SpecificationCheckCompletenessRequest(BaseModel):
    spec_path: str
    outputs_dir: str = "specflow"
    session_id: Optional[str] = None


class SpecificationCompletenessResult(BaseModel):
    """Result from specification completeness analysis."""
    readiness: SpecReadiness = SpecReadiness.NOT_READY
    summary: str
    result_file_path: Optional[str] = None
    session_id: Optional[str] = None


class GenerateAppRequest(BaseModel):
    spec_path: str
    outputs_dir: str = "specflow"
    src_dir: str = "src"
    session_id: Optional[str] = None
    generation_id: str
    workspace_count: Optional[int] = None  # 1, 2, or 3; None → env var → 3


class GenerationWorkflowRequest(BaseModel):
    spec_path: str
    outputs_dir: str = "specflow"
    session_id: Optional[str] = None
    generation_id: Optional[str] = None
    workspace_count: Optional[int] = None  # 1, 2, or 3; None → env var → 3