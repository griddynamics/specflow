
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

class CommitStatus(str, Enum):
    """Commit status options."""
    PENDING = "pending"
    PROCESSED = "PROCESSED"
    FAILED = "failed"

class GroupBy(str, Enum):
    """Grouping interval options for time-series data."""
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


class GitType(str, Enum):
    """Git provider types."""
    GITHUB = "github"
    GITLAB = "gitlab"


class RepositoryStatus(str, Enum):
    """Repository status options."""
    ACTIVE = "active"
    INACTIVE = "inactive"
    DISABLED = "disabled"


class EmployeeStatus(str, Enum):
    """Employee status options."""
    ACTIVE = "active"
    INACTIVE = "inactive"
    DEPARTED = "departed"


class EmploymentType(str, Enum):
    """Employment type options."""
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACTOR = "contractor"


class SeniorityLevel(str, Enum):
    """Seniority level options."""
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"


@dataclass
class UserSelfResponse:
    """Details of the currently authenticated user."""
    id: int
    email: str
    organisationId: int
    role: str
    isMfaActive: Optional[bool] = None


@dataclass
class TeamDetails:
    """Full details of an organization team."""
    id: int
    name: str
    created_at: str
    status: str
    contributor_count: int
    parentId: Optional[int] = None
    children: Optional[List['TeamDetails']] = None


@dataclass
class RepositoryDetails:
    """Details of a repository."""
    id: int
    id_organisation: int
    repository_name: str
    git_url: str
    status: str
    technology: Optional[List[str]] = None
    branch: Optional[str] = None
    internal_status: Optional[int] = None
    webhook_id: Optional[str] = None
    exclude_files: Optional[List[str]] = None
    last_commit_date: Optional[str] = None
    git_provider: Optional[str] = None
    last_metric: Optional[str] = None
    id_project: Optional[int] = None


@dataclass
class ContributorInfo:
    """Basic contributor information."""
    id_contributor: int
    username: str
    email: str


@dataclass
class MetricsResponse:
    """Generic metrics response container."""
    data: List[Dict[str, Any]]
    offset: Optional[int] = None
    limit: Optional[int] = None
    maxResults: Optional[int] = None
    total: Optional[int] = None
    low: Optional[float] = None
    medium: Optional[float] = None
    high: Optional[float] = None
    page: Optional[int] = None
    pageSize: Optional[int] = None
    totalPages: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
