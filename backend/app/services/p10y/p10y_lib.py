
from dataclasses import dataclass
import asyncio
import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

from app.services.p10y.p10y_api_client import P10YInternalAPIClient

# Commits whose first line starts with this prefix are excluded from P10Y / component breakdown
# (e.g. user-provided initial seed: SKIP_initial_user_source, SKIP_generation_baseline).
SKIP_COMMIT_PREFIX = "SKIP_"

# Valid component tokens — kept in sync with backend/app/standards/commit_standards.md.
KNOWN_COMPONENTS = frozenset({
    "backend", "frontend", "database", "api", "auth",
    "infrastructure", "testing", "documentation", "pipeline", "ml", "common",
})


commit_stats_fields = set(["sha", "ep_total", "fp_delta_total", "fp_delta_positive_total", "fp_delta_negative_total", "ep_total_refactor", "commit_quality_score", 
        "churn_rate", "technologies", "id_contributor", "refactor", "rework", "new_work", "removed_work", "quality_score", "effective_output", "total_output"])

# Predefined list of valid technologies for filtering commits
VALID_TECHNOLOGIES = {
    "abap", "c", "c#", "c++", "css", "go", "html", "java", "js", "kotlin",
    "less", "lua", "objective-c", "php", "python", "ruby", "rust", "sass",
    "scala", "shell", "solidity", "sql", "swift", "typescript", "vue", "hcl"
}

def has_valid_technology(commit_stats: Dict[str, Any], logger: logging.Logger) -> bool:
    """
    Check if commit has at least one valid technology from the predefined list.
    
    Args:
        commit_stats: P10Y commit stats with 'technologies' field
        logger: Logger instance
    
    Returns:
        True if commit has at least one valid technology, False otherwise
    """
    technologies = commit_stats.get("technologies", {})

    if not technologies:
        return False
    
    supported_technologies = technologies.get("supported", [])
    # Handle None or empty technologies list
    if not supported_technologies:
        return False
    
    # Normalize technologies to lowercase for comparison
    normalized_technologies = [tech.lower() for tech in supported_technologies]
    
    # Check if at least one technology is in the valid list
    has_valid = any(tech in VALID_TECHNOLOGIES for tech in normalized_technologies)
    
    if not has_valid:
        sha = commit_stats.get("sha", "unknown")
        logger.warning(
            f"Skipping commit {sha} from final calculation: "
            f"no valid technologies found (technologies: {technologies})"
        )
    
    return has_valid

@dataclass
class CommitInfo:
    sha: str
    message: str
    component: List[str]


@dataclass
class CodeGenerationMetadata:
    commits: List[CommitInfo]

    def __str__(self) -> str:
        messages = os.linesep.join([commit.message for commit in self.commits])
        return f"""
        Code Generation Commit Messages:
        {messages}
        """

@dataclass
class Estimation:
    function_points: float
    commit_quality_score: float
    churn_rate: float
    technologies: List[str]
    id_contributor: int
    refactor: float
    rework: float
    new_work: float
    removed_work: float
    quality_score: float
    effective_output: float
    total_output: float

    @staticmethod
    def prepare_for_estimation() -> "Estimation":
        return Estimation(
            function_points=0,
            commit_quality_score=0,
            churn_rate=0,
            technologies=[],
            id_contributor=0,
            refactor=0,
            rework=0,
            new_work=0,
            removed_work=0,
            quality_score=0,
            effective_output=0,
            total_output=0,
        )

async def calculate_estimation(commit_stats_data: List[Dict[str, Any]]) -> Estimation:
    estimation = Estimation.prepare_for_estimation()

    for commit in commit_stats_data:
        estimation = extract_estimation_from_commit_stats(estimation, commit)
    
    return estimation

def extract_estimation_from_commit_stats(estimation: Estimation, commit_stats: Dict[str, Any]) -> Estimation:
    estimation.function_points += float(commit_stats.get("fp_delta_total", 0))
    estimation.commit_quality_score += float(commit_stats.get("commit_quality_score", 0))
    estimation.churn_rate += float(commit_stats.get("churn_rate", 0))
    estimation.id_contributor += int(commit_stats.get("id_contributor", 0))
    estimation.refactor += float(commit_stats.get("refactor", 0))
    estimation.rework += float(commit_stats.get("rework", 0))
    estimation.new_work += float(commit_stats.get("new_work", 0))
    estimation.removed_work += float(commit_stats.get("removed_work", 0))
    estimation.quality_score += float(commit_stats.get("quality_score", 0))
    estimation.effective_output += float(commit_stats.get("effective_output", 0))
    estimation.total_output += float(commit_stats.get("total_output", 0))
    return estimation

def generate_component_breakdown(
    metadata: CodeGenerationMetadata, 
    commit_stats_data: List[Dict[str, Any]]
) -> Dict[str, Estimation]:
    """
    Generate component-level breakdown of estimation metrics.
    
    Args:
        metadata: Code generation metadata with commit info including components
        commit_stats_data: P10Y commit statistics data
    
    Returns:
        Dictionary mapping component names to their aggregated Estimation
    """
    commit_stats_map = {
        _normalize_git_sha(commit.get("sha") or ""): commit
        for commit in commit_stats_data
        if commit.get("sha")
    }

    component_breakdown: Dict[str, List[Dict[str, Any]]] = {}

    for commit_info in metadata.commits:
        commit_sha = _normalize_git_sha(commit_info.sha or "")
        if not commit_sha or commit_sha not in commit_stats_map:
            continue

        commit_stats = commit_stats_map[commit_sha]
        for component in commit_info.component:
            if component not in component_breakdown:
                component_breakdown[component] = []
            component_breakdown[component].append(commit_stats)
    
    component_estimations: Dict[str, Estimation] = {}
    for component, stats_list in component_breakdown.items():
        estimation = Estimation.prepare_for_estimation()
        
        for commit_stats in stats_list:
            estimation = extract_estimation_from_commit_stats(estimation, commit_stats)
        
        component_estimations[component] = estimation
    
    return component_estimations

def apply_productivity_multiplier(estimation: Estimation, multiplier: float = 2.0) -> float:
    """
    Apply productivity multiplier to convert function points to estimated hours.
    
    Args:
        estimation: Estimation object with function points
        multiplier: Productivity multiplier (default 2.0 for senior developers)
    
    Returns:
        Estimated hours
    """
    # Simple conversion: function points * multiplier = hours
    # This assumes 1 function point = base unit of effort
    # The multiplier accounts for developer proficiency, tooling (Claude Code), etc.
    return estimation.function_points * multiplier

def format_component_breakdown(component_breakdown: Dict[str, Estimation], multiplier: float = 2.0) -> str:
    """
    Format component breakdown for display in estimation summary.
    
    Args:
        component_breakdown: Dictionary of component estimations
        multiplier: Productivity multiplier for hours calculation
    
    Returns:
        Formatted markdown string
    """
    lines = []
    lines.append("## Component Breakdown\n")
    
    for component, estimation in sorted(component_breakdown.items()):
        hours = apply_productivity_multiplier(estimation, multiplier)
        lines.append(f"### {component.title()}")
        lines.append(f"- Estimated Hours: {hours:.1f}")
        lines.append(f"- New Work Units: {estimation.new_work:.1f}")
        lines.append(f"- Refactor Units: {estimation.refactor:.1f}")
        lines.append(f"- Rework Units: {estimation.rework:.1f}")
        lines.append(f"- Removed Work Units: {estimation.removed_work:.1f}")
        lines.append(f"- Quality Score: {estimation.quality_score:.2f}")
        lines.append("")
    
    return "\n".join(lines)


def _subject_excluded_from_estimation(subject: str, logger: Optional[logging.Logger] = None) -> bool:
    s = (subject or "").strip()
    if not s:
        if logger:
            logger.debug("Empty commit subject encountered — excluding from P10Y metadata")
        return True
    return s.upper().startswith(SKIP_COMMIT_PREFIX)


def _parse_component_from_subject(subject: str, logger: logging.Logger) -> List[str]:
    """
    Parse `{component}_{message}` commit subject (first line only).

    The substring before the first underscore is the component bucket; the rest is free text.
    If there is no underscore, or the token is not in KNOWN_COMPONENTS, logs a warning
    and falls back to "common".
    """
    s = (subject or "").strip()
    if "_" not in s:
        logger.warning(
            "Commit subject has no underscore component prefix (expected component_message): %s — using [common]",
            s[:80],
        )
        return ["common"]
    comp, _rest = s.split("_", 1)
    comp = comp.lower().strip()
    if not comp:
        return ["common"]
    if comp not in KNOWN_COMPONENTS:
        logger.warning(
            "Commit subject has unknown component token %r (not in KNOWN_COMPONENTS): %s — using as-is",
            comp,
            s[:80],
        )
    return [comp]


def _git_log_subject_lines(repo_root: str, logger: logging.Logger) -> List[tuple[str, str]]:
    """Return (full_sha, first_line_subject) for each commit, oldest first, no merges."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                repo_root,
                "log",
                "--reverse",
                "--no-merges",
                "--format=%H\t%s",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        logger.error("Cannot run git log in %s: %s", repo_root, e)
        return []

    if proc.returncode != 0:
        logger.error(
            "git log failed in %s (exit %s): %s",
            repo_root,
            proc.returncode,
            (proc.stderr or proc.stdout or "").strip(),
        )
        return []

    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    out: List[tuple[str, str]] = []
    for ln in lines:
        parts = ln.split("\t", 1)
        if len(parts) != 2:
            continue
        sha, subj = parts[0].strip(), parts[1].strip()
        if sha:
            out.append((sha, subj))
    return out


def build_code_generation_metadata_from_git(
    repo_root: str,
    logger: logging.Logger,
) -> Optional[CodeGenerationMetadata]:
    """
    Build commit metadata from `git log` (no JSON file).

    Skips commits whose subject starts with SKIP_ (case-insensitive), e.g. initial user seed.
    """
    if not repo_root or not os.path.isdir(repo_root):
        logger.warning("build_code_generation_metadata_from_git: not a directory: %s", repo_root)
        return None
    git_dir = os.path.join(repo_root, ".git")
    if not os.path.exists(git_dir):
        logger.warning("build_code_generation_metadata_from_git: not a git repo: %s", repo_root)
        return None

    rows = _git_log_subject_lines(repo_root, logger)
    commits: List[CommitInfo] = []
    for sha, subject in rows:
        if _subject_excluded_from_estimation(subject, logger):
            if subject.strip():
                logger.debug(
                    "Skipping commit from P10Y metadata (SKIP prefix): %s %s",
                    sha,
                    subject[:60],
                )
            continue
        component = _parse_component_from_subject(subject, logger)
        commits.append(
            CommitInfo(sha=sha, message=subject, component=component),
        )

    if not commits:
        return None
    return CodeGenerationMetadata(commits=commits)


def format_commits_metadata_for_prompt(metadata: CodeGenerationMetadata) -> str:
    """Compact JSON for janitor / prompts (derived list, not an on-disk agent file)."""
    data: List[Dict[str, Any]] = [
        {"sha": c.sha, "message": c.message, "component": c.component}
        for c in metadata.commits
        if c.sha
    ]
    return json.dumps(data, indent=2)


async def load_code_generation_metadata(repo_root: str, logger: logging.Logger) -> Optional[CodeGenerationMetadata]:
    """
    Load code-generation commit metadata from the workspace git history.

    Args:
        repo_root: Path to the git repository root (workspace isolated root).
        logger: Logger instance.
    """
    return await asyncio.to_thread(build_code_generation_metadata_from_git, repo_root, logger)


async def trigger_and_poll_p10y_metrics(
    client: P10YInternalAPIClient,
    repository_id: int,
    organisation_id: int,
    workspace_name: str,
    logger: logging.Logger,
) -> None:
    """
    Trigger P10Y metrics calculation and poll until processing is complete.
    
    Args:
        client: P10Y API client
        repository_id: P10Y repository ID
        organisation_id: P10Y organisation ID
        workspace_name: Workspace name for logging
        logger: Logger instance
    """
    # Trigger P10Y metrics calculation
    logger.info(f"Triggering P10Y metrics for repository {repository_id}")
    await client.run_metrics(
        organisation_id=organisation_id,
        repository_ids=[repository_id]
    )
    
    # Poll for commit stats to be processed
    logger.info(f"Polling for commit stats for repository {repository_id}")
    
    all_commits_processed = False
    poll_counter = 0
    poll_limit = 10
    
    while not all_commits_processed and poll_counter < poll_limit:
        logger.info(f"Polling attempt {poll_counter + 1} / {poll_limit}")
        
        commit_stats_polled = await client.get_commit_stats(
            organisation_id=organisation_id,
            repository_ids=[repository_id],
            at_least=10
        )
        poll_counter += 1
        
        metadata = commit_stats_polled.metadata
        pending = metadata.get("pending", 0)
        processed = metadata.get("processed", 0)
        
        if int(processed) == 0:
            logger.warning(
                f"No commits processed yet for workspace {workspace_name} "
                f"(attempt {poll_counter}). Waiting..."
            )
            await asyncio.sleep(10)
        elif int(pending) == 0 and int(processed) > 0:
            all_commits_processed = True
            logger.info(
                f"All commits processed for workspace {workspace_name} ({processed} processed)"
            )
        else:
            logger.info(
                f"Waiting for commits to be processed for workspace {workspace_name}: "
                f"{pending} pending, {processed} processed. Waiting..."
            )
            await asyncio.sleep(10)
    
    if poll_counter >= poll_limit:
        logger.warning(
            f"Reached poll limit for workspace {workspace_name}. "
            f"Proceeding with available data..."
        )


def _normalize_git_sha(sha: str) -> str:
    return sha.strip().lower()


async def fetch_and_filter_commit_stats(
    client: P10YInternalAPIClient,
    repository_id: int,
    organisation_id: int,
    allowed_commit_shas: List[str],
    workspace_name: str,
    logger: logging.Logger,
) -> List[dict]:
    """
    Fetch commit stats from P10Y and keep only rows whose ``sha`` is in the local git
    allowlist (full hash, case-insensitive). P10Y's API may return a longer history than
    the current workspace; we intersect strictly so estimation uses only current commits.

    Args:
        client: P10Y API client
        repository_id: P10Y repository ID
        organisation_id: P10Y organisation ID
        allowed_commit_shas: Full SHAs from local git (after SKIP_* filtering)
        workspace_name: Workspace name for logging
        logger: Logger instance

    Returns:
        Filtered list of commit stats (at most one row per allowed SHA, deduped)
    """
    allowed = {_normalize_git_sha(h) for h in allowed_commit_shas if h}
    count_of_commits = len(allowed)

    # Request enough history pages for P10Y to include our SHAs; response may still carry
    # unrelated historical commits — we intersect strictly below.
    commit_stats = await client.get_commit_stats(
        organisation_id=organisation_id,
        repository_ids=[repository_id],
        at_least=count_of_commits * 2 if count_of_commits * 2 > 50 else 50
    )
    commit_stats_data = commit_stats.data

    logger.info(
        f"Retrieved {len(commit_stats_data)} commit stats from P10Y for workspace {workspace_name}"
    )

    seen: set[str] = set()
    hash_filtered_commits: List[dict] = []
    for p10y_commit in commit_stats_data:
        raw_sha = p10y_commit.get("sha")
        if raw_sha is None:
            continue
        norm = _normalize_git_sha(str(raw_sha))
        if norm not in allowed:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        hash_filtered_commits.append(p10y_commit)

    logger.info(
        f"Hash-filtered commits: {len(hash_filtered_commits)} out of {len(commit_stats_data)} "
        f"P10Y rows (allowlist size {count_of_commits} from local git)"
    )
    
    # Then filter by valid technologies
    filtered_commit_stats_data = [
        p10y_commit
        for p10y_commit in hash_filtered_commits
        if has_valid_technology(p10y_commit, logger)
    ]
    
    logger.info(
        f"Technology-filtered commits: {len(filtered_commit_stats_data)} out of {len(hash_filtered_commits)} hash-filtered commits"
    )
    
    if len(filtered_commit_stats_data) != count_of_commits:
        logger.warning(
            f"Commit count mismatch for workspace {workspace_name}: "
            f"expected {count_of_commits}, found {len(filtered_commit_stats_data)}. "
            f"Continuing with available data..."
        )
    
    return filtered_commit_stats_data