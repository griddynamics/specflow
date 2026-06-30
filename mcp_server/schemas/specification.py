from pydantic import BaseModel
from typing import Optional

class SpecificationIndexRequest(BaseModel):
    spec_path: str
    outputs_dir: str = "specflow"
    session_id: Optional[str] = None

class SpecificationCheckCompletenessRequest(BaseModel):
    spec_path: str
    outputs_dir: str = "specflow"
    session_id: Optional[str] = None

class GenerateAppRequest(BaseModel):
    spec_path: str
    outputs_dir: str = "specflow"
    src_dir: str = "src"
    session_id: Optional[str] = None
    generation_id: str
    workspace_count: Optional[int] = None


class GenerationWorkflowRequest(BaseModel):
    spec_path: str
    outputs_dir: str = "specflow"
    session_id: Optional[str] = None
    generation_id: Optional[str] = None
    workspace_count: Optional[int] = None
