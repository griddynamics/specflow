"""
Read and write helpers for per-workspace model overrides stored on the generation session.

All writes in this module go through StateMachineDBAdapter, satisfying Commandment VII.
``set_workspace_model_override`` is called directly by ``_apply_model_override_if_switched``
in ``claude_code.py`` (not via GenerationSessionStateMachine) — the write path does not
pass through the state machine, so callers are responsible for the RUNNING-status guard
enforced at the top of that function.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from app.schemas.generation_workflow_enums import GenerationStatus
from app.state.db_adapter import StateMachineDBAdapter
from app.state.exceptions import InvalidGenerationSessionStateError

logger = logging.getLogger(__name__)


async def load_workspace_model_overrides(
    generation_id: str,
    db_adapter: Optional[StateMachineDBAdapter],
    logger: logging.Logger,
) -> Dict[str, str]:
    """Return the workspace_model_overrides map from the session document, or {} on error/missing."""
    if not db_adapter or not generation_id:
        return {}
    try:
        doc = await db_adapter.get_generation_session(generation_id)
        return (doc or {}).get("workspace_model_overrides") or {}
    except Exception as e:
        logger.warning("Failed to read workspace_model_overrides for %s: %s", generation_id, e)
        return {}


def resolve_workspace_model(
    workspace_id: str,
    base_model: str,
    overrides: Dict[str, str],
    logger: logging.Logger,
) -> str:
    """Return the effective model for *workspace_id*, applying any persisted override."""
    override = overrides.get(workspace_id)
    if override:
        logger.info(
            "  Workspace %s: using persisted model_override %s (was %s)",
            workspace_id, override, base_model,
        )
        return override
    return base_model


async def set_workspace_model_override(
    generation_id: str,
    workspace_id: str,
    model: str,
    triggered_by: str,
    db: StateMachineDBAdapter,
) -> None:
    """RUNNING → RUNNING: persist a per-workspace model override on the generation session.

    Called when a routing failure forces a fallback model mid-run so that a crash-retry
    picks up the override instead of re-assigning the broken model.  Stored as
    ``workspace_model_overrides[workspace_id]`` on the session document — not on the
    workspace document, which is shared across generations.
    """
    doc = await db.get_generation_session(generation_id)
    current_status = (doc or {}).get("status")
    if current_status != GenerationStatus.RUNNING:
        raise InvalidGenerationSessionStateError(
            generation_id=generation_id,
            current_status=current_status,
            attempted_transition="set_workspace_model_override",
            allowed_from=[GenerationStatus.RUNNING],
        )

    now = datetime.now(timezone.utc)
    history_entry = {
        "status": GenerationStatus.RUNNING,
        "at": now,
        "triggered_by": triggered_by,
        "metadata": {"workspace_id": workspace_id, "model_override": model},
    }
    # Two-step write: field-path update for the override (atomic per workspace_id),
    # then array_union for state_history (atomic append, no clobber from concurrent
    # workspaces hitting routing failures at the same time).
    update = {
        f"workspace_model_overrides.{workspace_id}": model,
        "last_activity_at": now,
    }
    await db.update_generation_session(generation_id, update)
    await db.array_union_generation_session(generation_id, "state_history", [history_entry])
    logger.info(
        "generation %s workspace %s model_override → %s (by %s)",
        generation_id, workspace_id, model, triggered_by,
    )
