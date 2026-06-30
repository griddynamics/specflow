from typing import Any, Dict, List, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

from app.schemas.model_token_usage import ModelTokenUsage

# Legacy generation models (AWUS-based)
class EstimateGenerateRequest(BaseModel):
    specification_file_path: str

class EstimateItem(BaseModel):
    task: str
    hours: float
    cost: float

class EstimatePhase(BaseModel):
    name: str
    items: List[EstimateItem]
    total_hours: float
    total_cost: float

class EstimateGenerateResponse(BaseModel):
    total_cost: float
    total_hours: float
    timeline_weeks: float
    phases: List[EstimatePhase]
    currency: str = "USD"
    raw_output: Optional[str] = None


# Multi-workspace P10Y estimation models
class ComponentEstimation(BaseModel):
    """Estimation metrics for a single component."""
    component_name: str
    hours: float
    new_work: float
    refactor: float
    rework: float
    quality_score: float


class EstimationMetrics(BaseModel):
    """Aggregated estimation metrics from P10Y."""
    new_work: float
    refactor: float
    rework: float
    removed_work: float
    quality_score: float
    effective_output: float
    total_output: float


class WorkspaceEstimation(BaseModel):
    """Estimation results for a single workspace."""
    model_config = ConfigDict(extra="ignore")

    workspace_name: str
    workspace_path: str
    total_hours: float
    total_effective_output: float
    component_breakdown: Dict[str, ComponentEstimation]
    estimation_metrics: EstimationMetrics
    commits_count: int
    p10y_scored_commits: Optional[int] = None
    model_usage: Optional[ModelTokenUsage] = None
    total_usd_cost: Optional[float] = Field(
        default=None,
        description="Cumulative LLM API spend in USD for this workspace (tracked in Firestore).",
    )

    @field_validator("model_usage", mode="before")
    @classmethod
    def _parse_model_usage(cls, v: Any) -> Any:
        if v is None or isinstance(v, ModelTokenUsage):
            return v
        if isinstance(v, dict):
            return ModelTokenUsage.from_dict(v)
        return v

    @field_serializer("model_usage")
    def _serialize_model_usage(self, v: Optional[ModelTokenUsage]) -> Optional[Dict[str, Any]]:
        return None if v is None else v.to_dict()


class ComponentComparison(BaseModel):
    """Comparison of a component across workspaces."""
    component_name: str
    hours_by_workspace: Dict[str, float]  # workspace_name -> hours
    average: float
    std_deviation: float
    variance_percentage: float


class ComparativeAnalysis(BaseModel):
    """Comparative analysis across all workspaces."""
    component_comparison: Dict[str, ComponentComparison]
    high_variance_components: List[str]
    insights: List[str]


class RiskAssessment(BaseModel):
    """Risk assessment results from the risk model."""
    status: str  # "Approved" or "Rejected"
    instability_ratio: float  # sigma / minimum
    rejection_threshold: float  # Maximum allowed instability
    base_component: float  # Base buffer percentage
    var_component: float  # Variance penalty component
    size_component: float  # Size penalty component
    total_buffer_pct: float  # Total buffer as percentage
    final_estimate: float  # Final buffered estimate in hours


class EstimationSummary(BaseModel):
    """Statistical summary of multi-workspace generation."""
    average_hours: float
    std_deviation: float
    min_hours: float
    max_hours: float
    coefficient_of_variation: float  # CV = std_dev / mean
    variance_assessment: str  # "low", "medium", "high"
    risk_assessment: Optional[RiskAssessment] = None  # Risk model results


class SkippedWorkspaceP10Y(BaseModel):
    """A workspace that did not produce a P10Y-backed estimate (best-effort run)."""
    workspace_name: str
    reason: str


class MultiWorkspaceEstimationResponse(BaseModel):
    """Response containing estimation results from all workspaces."""
    summary: EstimationSummary
    workspace_estimations: List[WorkspaceEstimation]
    comparative_analysis: ComparativeAnalysis
    timestamp: str
    skipped_workspaces: List[SkippedWorkspaceP10Y] = Field(
        default_factory=list,
        description="Workspaces with no usable P10Y row (repo missing, API error, no commits, etc.).",
    )
    aggregate_p10y_commit_coverage_pct: Optional[float] = Field(
        default=None,
        description="Eligible git commits that had P10Y scores, across successful workspaces only (%).",
    )
    total_usd_cost: Optional[float] = Field(
        default=None,
        description="Cumulative LLM API spend in USD for the whole generation run (Firestore).",
    )


class SimplifiedEstimationResponse(BaseModel):
    """Simplified response with only essential information."""
    status: str  # "Approved" or "Rejected"
    final_estimate_hours: float  # Final buffered estimate


class ResendEmailResponse(BaseModel):
    """Response for resend email endpoint."""
    generation_id: str
    email_sent: bool
    recipient: str
    message: str
