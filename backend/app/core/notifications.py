from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from enum import Enum
import html
import logging
import smtplib
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.config import EmailConfig
from app.core.config import settings
from app.database.interface import IDatabase
from app.schemas.estimate import (
    ComparativeAnalysis,
    EstimationMetrics,
    EstimationSummary,
    MultiWorkspaceEstimationResponse,
    RiskAssessment,
    WorkspaceEstimation,
)
from app.schemas.workflow_usage_metrics import aggregate_model_usage_by_workspace
from app.schemas.workspace_model_usage_store import (
    SESSION_TOTAL_USD_COST_FIELD,
    WORKSPACE_MODEL_USAGE_SUBCOLLECTION,
    WORKSPACE_TOTAL_USD_COST_FIELD,
)
from app.services.git_archive_service import GitArchiveService
from app.state.db_adapter import COL_GENERATION_SESSIONS
from app.utils.formatting import format_token_usage_lines

class GenerationSessionNotificationKind(str, Enum):
    COMPLETE = "complete"
    CODING_COMPLETE_PRE_DEPLOY = "coding_complete_pre_deploy"


class MilestoneNotificationKind(str, Enum):
    SPEC_CHECK = "spec_check"
    PLANNING = "planning"

logger = logging.getLogger("utils.notifications")


def enrich_multi_workspace_result_with_llm_costs_from_db(
    db: Optional[IDatabase],
    generation_id: str,
    result: Any,
) -> Any:
    """Attach ``total_usd_cost`` from Firestore subcollection for emails/Slack."""
    if db is None or not generation_id:
        return result
    if not isinstance(result, MultiWorkspaceEstimationResponse):
        return result
    try:
        doc = db.get(COL_GENERATION_SESSIONS, generation_id)
        if not doc:
            return result
        rows = db.list_subcollection(
            COL_GENERATION_SESSIONS,
            generation_id,
            WORKSPACE_MODEL_USAGE_SUBCOLLECTION,
        )
        by_ws = {
            str(r["_id"]): float(r.get(WORKSPACE_TOTAL_USD_COST_FIELD) or 0.0)
            for r in rows
            if r.get("_id")
        }
        raw_st = doc.get(SESSION_TOTAL_USD_COST_FIELD)
        session_total: Optional[float] = float(raw_st) if raw_st is not None else None
        if session_total is None and by_ws:
            session_total = sum(by_ws.values())

        new_ws: list[WorkspaceEstimation] = []
        for ws in result.workspace_estimations:
            c = by_ws.get(ws.workspace_name)
            if c is None:
                new_ws.append(ws)
            else:
                new_ws.append(ws.model_copy(update={"total_usd_cost": c}))

        return result.model_copy(
            update={
                "workspace_estimations": new_ws,
                "total_usd_cost": session_total,
            }
        )
    except Exception as exc:
        logger.warning(
            "Could not merge Firestore LLM cost fields for notify (%s): %s",
            generation_id,
            exc,
            exc_info=True,
        )
        return result


def _session_llm_cost_display(result: Any) -> Optional[str]:
    """Human-readable cumulative LLM spend for the run, when available."""
    try:
        st = getattr(result, "total_usd_cost", None)
        if st is not None:
            return f"USD {float(st):,.2f}"
        ws_list = getattr(result, "workspace_estimations", None) or []
        s = sum(float(getattr(ws, "total_usd_cost") or 0.0) for ws in ws_list)
        if s > 0.0:
            return f"USD {s:,.2f}"
    except (TypeError, ValueError):
        return None
    return None


def _format_workspace_llm_cost_line(ws_est: Any) -> Optional[str]:
    v = getattr(ws_est, "total_usd_cost", None)
    if v is None:
        return None
    try:
        return f"LLM API cost (cumulative): USD {float(v):,.2f}"
    except (TypeError, ValueError):
        return None


def total_usd_cost_from_doc(doc: Optional[dict]) -> Optional[float]:
    """Read cumulative session LLM spend (USD) from a generation session document."""
    if not doc:
        return None
    raw = doc.get(SESSION_TOTAL_USD_COST_FIELD)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _optional_usd_label(v: Optional[float], *, label: str) -> Optional[tuple[str, str]]:
    """Return a (label, formatted USD) summary row, or None if value is missing."""
    if v is None:
        return None
    try:
        return (label, f"USD {float(v):,.2f}")
    except (TypeError, ValueError):
        return None


def _build_milestone_email_html_plain(
    kind: MilestoneNotificationKind,
    *,
    generation_id: str,
    models_summary: str,
    agent_models_line: str,
    usage_block: str,
    total_usd_cost: Optional[float],
    spec_path: Optional[str] = None,
    coding_plan_phase_count: int | str | None = None,
    e2e_plan_phase_count: int | str | None = None,
    workflow_run_cost_usd: Optional[float] = None,
) -> tuple[str, str]:
    """HTML + plain text for spec-check or planning milestone emails (shared layout)."""
    if kind == MilestoneNotificationKind.SPEC_CHECK:
        page_title = "SpecFlow — Spec check complete"
        header = "Spec check complete"
        accent = "#1565c0"
    else:
        page_title = "SpecFlow — Planning complete"
        header = "Planning complete"
        accent = "#2e7d32"

    rows: list[tuple[str, str]] = [
        ("Run ID", generation_id),
        ("Models (tier config)", models_summary),
        ("Resolved agent models", agent_models_line),
    ]
    if spec_path:
        rows.insert(1, ("Specification", spec_path))
    if kind == MilestoneNotificationKind.PLANNING and coding_plan_phase_count is not None:
        rows.append(("Coding plan phases", str(coding_plan_phase_count)))
    if kind == MilestoneNotificationKind.PLANNING and e2e_plan_phase_count is not None:
        rows.append(("E2E plan phases", str(e2e_plan_phase_count)))

    usd_session = _optional_usd_label(
        total_usd_cost,
        label="Total LLM cost (session, cumulative)",
    )
    if usd_session:
        rows.append(usd_session)
    usd_wf = _optional_usd_label(
        workflow_run_cost_usd,
        label="This planning run (workflow stats)",
    )
    if kind == MilestoneNotificationKind.PLANNING and usd_wf:
        rows.append(usd_wf)

    plain_lines = [page_title, "=" * 60, ""]
    for label, val in rows:
        plain_lines.append(f"{label}: {val}")
    plain_lines.extend(["", "Token / usage details:", usage_block])
    plain_content = "\n".join(plain_lines)

    html_parts: list[str] = [
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"/>",
        "<style>",
        "body { font-family: Arial, sans-serif; line-height: 1.55; color: #222; margin: 0; padding: 0; }",
        f".header {{ background-color: {accent}; color: #fff; padding: 18px 22px; border-radius: 6px 6px 0 0; }}",
        ".header h1 { margin: 0; font-size: 1.35rem; font-weight: 600; }",
        ".content { padding: 20px 22px; background: #f5f5f5; }",
        ".card { background: #fff; border-radius: 6px; padding: 16px 18px; margin-bottom: 14px; "
        "box-shadow: 0 1px 2px rgba(0,0,0,.06); }",
        ".row { margin: 8px 0; }",
        ".k { font-weight: bold; color: #444; display: inline-block; min-width: 220px; vertical-align: top; }",
        ".v { color: #111; }",
        "pre.usage { background: #fafafa; border: 1px solid #e0e0e0; border-radius: 4px; padding: 12px; "
        "font-size: 13px; white-space: pre-wrap; word-break: break-word; margin: 0; }",
        ".footer { padding: 14px 22px; color: #666; font-size: 12px; }",
        "</style></head><body>",
        f"<div class=\"header\"><h1>{html.escape(header)}</h1></div>",
        "<div class=\"content\">",
        "<div class=\"card\">",
    ]
    for label, val in rows:
        html_parts.append(
            f"<div class=\"row\"><span class=\"k\">{html.escape(label)}:</span> "
            f"<span class=\"v\">{html.escape(val)}</span></div>"
        )
    html_parts.append("</div>")
    html_parts.append("<div class=\"card\">")
    html_parts.append("<div class=\"row\"><span class=\"k\">Usage</span></div>")
    html_parts.append(f"<pre class=\"usage\">{html.escape(usage_block)}</pre>")
    html_parts.append("</div></div>")
    html_parts.append(
        f"<div class=\"footer\">Run ID: {html.escape(generation_id)}</div>"
        "</body></html>"
    )
    html_content = "\n".join(html_parts)
    return html_content, plain_content


def _milestone_slack_blocks(
    kind: MilestoneNotificationKind,
    *,
    generation_id: str,
    models_summary: str,
    agent_models_line: str,
    usage_block: str,
    recipient_email: Optional[str],
    total_usd_cost: Optional[float],
    spec_path: Optional[str] = None,
    coding_plan_phase_count: int | str | None = None,
    e2e_plan_phase_count: int | str | None = None,
    workflow_run_cost_usd: Optional[float] = None,
) -> list:
    """Block Kit payload for spec-check or planning milestone (Slack)."""
    if kind == MilestoneNotificationKind.SPEC_CHECK:
        header_text = "SpecFlow — Spec check complete"
    else:
        header_text = "SpecFlow — Planning complete"

    fields: list[dict] = [
        {"type": "mrkdwn", "text": f"*Run ID:*\n`{generation_id}`"},
        {"type": "mrkdwn", "text": f"*User:*\n{recipient_email or 'unknown'}"},
        {"type": "mrkdwn", "text": f"*Models (tier config):*\n{models_summary}"},
        {"type": "mrkdwn", "text": f"*Resolved agent models:*\n{agent_models_line}"},
    ]
    if spec_path:
        fields.insert(
            1,
            {"type": "mrkdwn", "text": f"*Specification:*\n`{spec_path}`"},
        )
    if kind == MilestoneNotificationKind.PLANNING and coding_plan_phase_count is not None:
        fields.append(
            {"type": "mrkdwn", "text": f"*Coding plan phases:*\n{coding_plan_phase_count}"}
        )
    if kind == MilestoneNotificationKind.PLANNING and e2e_plan_phase_count is not None:
        fields.append(
            {"type": "mrkdwn", "text": f"*E2E plan phases:*\n{e2e_plan_phase_count}"}
        )
    usd_s = _optional_usd_label(
        total_usd_cost,
        label="Total LLM cost (session, cumulative)",
    )
    if usd_s:
        fields.append({"type": "mrkdwn", "text": f"*{usd_s[0]}:*\n{usd_s[1]}"})
    usd_w = _optional_usd_label(
        workflow_run_cost_usd,
        label="This planning run (workflow stats)",
    )
    if kind == MilestoneNotificationKind.PLANNING and usd_w:
        fields.append({"type": "mrkdwn", "text": f"*{usd_w[0]}:*\n{usd_w[1]}"})

    blocks: list = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text, "emoji": True},
        },
        {"type": "section", "fields": fields},
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _slack_usage_block_text(usage_block),
            },
        },
    ]
    return blocks


def _slack_usage_block_text(usage_block: str) -> str:
    """Make usage text safe inside a Slack mrkdwn fenced block (length + fence chars)."""
    u = usage_block.replace("```", "``\u200b`")
    if len(u) > 2800:
        u = u[:2800] + "\n…(truncated)"
    return f"*Usage (tokens / turns)*\n```\n{u}\n```"


def _format_tokens_millions(input_tokens: int | None, output_tokens: int | None) -> str | None:
    """Format combined input+output tokens as millions with 1 decimal place."""
    if input_tokens is None and output_tokens is None:
        return None
    total = (input_tokens or 0) + (output_tokens or 0)
    return f"{total / 1_000_000:.1f}M"


def _workspace_codegen_usage_lines(ws_est: Any) -> list[str]:
    """Slack/email lines for per-workspace codegen LLM usage (optional fields)."""
    mu = getattr(ws_est, "model_usage", None)
    if mu is None or mu.is_empty():
        return []
    return format_token_usage_lines(mu)


@dataclass(frozen=True)
class SummaryStat:
    label: str
    value: str


def _p10y_variance_summary(result: Any, *, pre_deploy: bool) -> SummaryStat:
    """Summary row: P10Y variance stats only — not approval/rejection or buffer."""
    if pre_deploy:
        return SummaryStat(
            label="Phase",
            value="Coding complete — deployment & QA next (P10Y hours follow after the full run)",
        )
    try:
        summary = result.summary
        va = getattr(summary, "variance_assessment", None) or "unknown"
        cv = getattr(summary, "coefficient_of_variation", None)
        cv_s = f"{float(cv) * 100:.1f}%" if cv is not None else "n/a"
        return SummaryStat(label="Variance (P10Y)", value=f"{va} (CV {cv_s})")
    except (AttributeError, TypeError, ValueError):
        return SummaryStat(label="Variance (P10Y)", value="unknown")


def build_coding_complete_pre_deploy_response(
    *,
    workspace_ids: List[str],
    workflow_usage_metrics: Dict[str, Any] | None,
    total_usd_cost: Optional[float] = None,
    workspace_llm_cost_usd: Optional[Dict[str, float]] = None,
) -> MultiWorkspaceEstimationResponse:
    """
    Synthetic multi-workspace result for the pre-deploy milestone email/Slack.

    Matches the shape of :class:`MultiWorkspaceEstimationResponse` so the same
    templates render cumulative LLM usage (from ``workflow_usage_metrics``) as the
    final notification; P10Y hour fields are absent and shown as pending.
    """
    by_ws = aggregate_model_usage_by_workspace(workflow_usage_metrics)
    zero_metrics = EstimationMetrics(
        new_work=0.0,
        refactor=0.0,
        rework=0.0,
        removed_work=0.0,
        quality_score=0.0,
        effective_output=0.0,
        total_output=0.0,
    )
    workspace_estimations: list[WorkspaceEstimation] = []
    ws_costs = workspace_llm_cost_usd or {}
    for ws_id in workspace_ids:
        mu = by_ws.get(ws_id)
        wc = ws_costs.get(ws_id)
        workspace_estimations.append(
            WorkspaceEstimation(
                workspace_name=ws_id,
                workspace_path="",
                total_hours=0.0,
                total_effective_output=0.0,
                component_breakdown={},
                estimation_metrics=zero_metrics,
                commits_count=0,
                p10y_scored_commits=0,
                model_usage=mu if mu is not None and not mu.is_empty() else None,
                total_usd_cost=wc,
            )
        )
    summary = EstimationSummary(
        average_hours=0.0,
        std_deviation=0.0,
        min_hours=0.0,
        max_hours=0.0,
        coefficient_of_variation=0.0,
        variance_assessment="low",
        risk_assessment=RiskAssessment(
            status="Coding complete — starting deployment & QA",
            instability_ratio=0.0,
            rejection_threshold=1.0,
            base_component=0.0,
            var_component=0.0,
            size_component=0.0,
            total_buffer_pct=0.0,
            final_estimate=0.0,
        ),
    )
    comparative_analysis = ComparativeAnalysis(
        component_comparison={},
        high_variance_components=[],
        insights=[
            "P10Y hour estimates and component breakdown are not available yet; "
            "they will be included in the final notification after deployment and the P10Y phase.",
        ],
    )
    return MultiWorkspaceEstimationResponse(
        summary=summary,
        workspace_estimations=workspace_estimations,
        comparative_analysis=comparative_analysis,
        timestamp=datetime.now(timezone.utc).isoformat(),
        total_usd_cost=total_usd_cost,
    )


class Notifier:
    def notify(self, message: str, recipient_email: str | None = None):
        raise NotImplementedError("Subclasses must implement this method")

class Notifications:
    def __init__(self):
        self.notifiers = []

    def add_notifier(self, notifier: Notifier):
        self.notifiers.append(notifier)

    def notify(self, message: str, recipient_email: str | None = None):
        for notifier in self.notifiers:
            notifier.notify(message, recipient_email=recipient_email)
    
    def notify_generation_session_complete(
        self,
        generation_id: str,
        workspace_ids: List[str],
        result: Any,  # MultiWorkspaceEstimationResponse
        spec_path: str,
        recipient_email: str | None = None,
        db: Optional[IDatabase] = None,
        notification_kind: GenerationSessionNotificationKind = GenerationSessionNotificationKind.COMPLETE,
    ):
        """
        Send notification when a generation session completes with rich content.

        Args:
            generation_id: The generation session ID (used as branch name)
            workspace_ids: List of workspace IDs
            result: MultiWorkspaceEstimationResponse object
            spec_path: Specification path
            recipient_email: Email recipient
            db: Database interface for retrieving workspace documents
            notification_kind: ``complete`` (final) or ``coding_complete_pre_deploy`` (milestone)
        
        Note: This method catches all exceptions from individual notifiers to prevent
        notification failures from affecting the generation workflow. Failures are logged
        but do not propagate.
        """
        result = enrich_multi_workspace_result_with_llm_costs_from_db(db, generation_id, result)
        for notifier in self.notifiers:
            try:
                if isinstance(notifier, (EmailNotifier, SlackNotifier)):
                    notifier.notify_generation_session_complete(
                        generation_id=generation_id,
                        workspace_ids=workspace_ids,
                        result=result,
                        spec_path=spec_path,
                        recipient_email=recipient_email,
                        db=db,
                        notification_kind=notification_kind,
                    )
                else:
                    try:
                        status = result.summary.risk_assessment.status if result.summary.risk_assessment else "Unknown"
                        final_estimate = result.summary.risk_assessment.final_estimate if result.summary.risk_assessment else result.summary.average_hours
                    except (AttributeError, KeyError):
                        status = "Unknown"
                        final_estimate = 0.0

                    message = (
                        f"Multi-workspace P10Y completed for {spec_path}. "
                        f"Status: {status}, "
                        f"Final Estimate: {final_estimate:.1f}h"
                    )
                    notifier.notify(message, recipient_email=recipient_email)
            except Exception as e:
                logger.error(
                    f"Failed to send notification via {type(notifier).__name__} "
                    f"for generation session {generation_id} (non-fatal): {e}",
                    exc_info=True
                )

    def notify_spec_check_complete(
        self,
        *,
        generation_id: str,
        models_summary: str,
        usage_block: str,
        agent_models_line: str,
        recipient_email: str | None = None,
        total_usd_cost: Optional[float] = None,
        spec_path: Optional[str] = None,
    ) -> None:
        for notifier in self.notifiers:
            try:
                if isinstance(notifier, (EmailNotifier, SlackNotifier)):
                    notifier.notify_spec_check_complete(
                        generation_id=generation_id,
                        models_summary=models_summary,
                        usage_block=usage_block,
                        agent_models_line=agent_models_line,
                        recipient_email=recipient_email,
                        total_usd_cost=total_usd_cost,
                        spec_path=spec_path,
                    )
                else:
                    extras: list[str] = []
                    usd = _optional_usd_label(
                        total_usd_cost,
                        label="Total LLM cost (session, cumulative)",
                    )
                    if usd:
                        extras.append(f"{usd[0]}: {usd[1]}")
                    tail = ("\n" + "\n".join(extras)) if extras else ""
                    notifier.notify(
                        f"Spec check complete — {generation_id}\n{usage_block}{tail}",
                        recipient_email=recipient_email,
                    )
            except Exception as e:
                logger.error(
                    "Failed spec-check notification via %s: %s",
                    type(notifier).__name__,
                    e,
                    exc_info=True,
                )

    def notify_planning_complete(
        self,
        *,
        generation_id: str,
        models_summary: str,
        coding_plan_phase_count: int | str,
        e2e_plan_phase_count: int | str,
        usage_block: str,
        agent_models_line: str,
        recipient_email: str | None = None,
        total_usd_cost: Optional[float] = None,
        workflow_run_cost_usd: Optional[float] = None,
    ) -> None:
        for notifier in self.notifiers:
            try:
                if isinstance(notifier, (EmailNotifier, SlackNotifier)):
                    notifier.notify_planning_complete(
                        generation_id=generation_id,
                        models_summary=models_summary,
                        coding_plan_phase_count=coding_plan_phase_count,
                        e2e_plan_phase_count=e2e_plan_phase_count,
                        usage_block=usage_block,
                        agent_models_line=agent_models_line,
                        recipient_email=recipient_email,
                        total_usd_cost=total_usd_cost,
                        workflow_run_cost_usd=workflow_run_cost_usd,
                    )
                else:
                    extras: list[str] = []
                    for pair in (
                        _optional_usd_label(
                            total_usd_cost,
                            label="Total LLM cost (session, cumulative)",
                        ),
                        _optional_usd_label(
                            workflow_run_cost_usd,
                            label="This planning run (workflow stats)",
                        ),
                    ):
                        if pair:
                            extras.append(f"{pair[0]}: {pair[1]}")
                    tail = ("\n" + "\n".join(extras)) if extras else ""
                    notifier.notify(
                        f"Planning complete — {generation_id} — coding phases={coding_plan_phase_count}{tail}",
                        recipient_email=recipient_email,
                    )
            except Exception as e:
                logger.error(
                    "Failed planning notification via %s: %s",
                    type(notifier).__name__,
                    e,
                    exc_info=True,
                )


class SlackNotifier(Notifier):
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.logger = logging.getLogger(__name__)

    def notify(self, message: str, recipient_email: str | None = None):
        """
        Send a nicely formatted notification to Slack using Block Kit format.
        
        Args:
            message: Message content
            recipient_email: Ignored (Slack notifications don't use email addresses)
        """
        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"🔔 SpecFlow Backend Notification / {recipient_email}",
                        "emoji": True
                    }
                },
                {
                    "type": "divider"
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Message:*\n{message}"
                    }
                }
            ]
        }

        self._post_payload(payload)

    def notify_generation_session_complete(
        self,
        generation_id: str,
        workspace_ids: List[str],
        result: Any,
        spec_path: str,
        recipient_email: str | None = None,
        db: Optional[IDatabase] = None,
        notification_kind: GenerationSessionNotificationKind = GenerationSessionNotificationKind.COMPLETE,
    ) -> None:
        """Send a rich Slack notification when a generation session completes."""
        blocks = self._build_generation_session_slack_blocks(
            generation_id=generation_id,
            workspace_ids=workspace_ids,
            result=result,
            spec_path=spec_path,
            recipient_email=recipient_email,
            notification_kind=notification_kind,
        )
        self._post_payload({"blocks": blocks})

    def notify_spec_check_complete(
        self,
        *,
        generation_id: str,
        models_summary: str,
        usage_block: str,
        agent_models_line: str,
        recipient_email: str | None = None,
        total_usd_cost: Optional[float] = None,
        spec_path: Optional[str] = None,
    ) -> None:
        blocks = _milestone_slack_blocks(
            MilestoneNotificationKind.SPEC_CHECK,
            generation_id=generation_id,
            models_summary=models_summary,
            agent_models_line=agent_models_line,
            usage_block=usage_block,
            recipient_email=recipient_email,
            total_usd_cost=total_usd_cost,
            spec_path=spec_path,
        )
        self._post_payload({"blocks": blocks})

    def notify_planning_complete(
        self,
        *,
        generation_id: str,
        models_summary: str,
        coding_plan_phase_count: int | str,
        e2e_plan_phase_count: int | str,
        usage_block: str,
        agent_models_line: str,
        recipient_email: str | None = None,
        total_usd_cost: Optional[float] = None,
        workflow_run_cost_usd: Optional[float] = None,
    ) -> None:
        blocks = _milestone_slack_blocks(
            MilestoneNotificationKind.PLANNING,
            generation_id=generation_id,
            models_summary=models_summary,
            agent_models_line=agent_models_line,
            usage_block=usage_block,
            recipient_email=recipient_email,
            total_usd_cost=total_usd_cost,
            coding_plan_phase_count=coding_plan_phase_count,
            e2e_plan_phase_count=e2e_plan_phase_count,
            workflow_run_cost_usd=workflow_run_cost_usd,
        )
        self._post_payload({"blocks": blocks})

    def _build_generation_session_slack_blocks(
        self,
        generation_id: str,
        workspace_ids: List[str],
        result: Any,
        spec_path: str,
        recipient_email: str | None = None,
        notification_kind: GenerationSessionNotificationKind = GenerationSessionNotificationKind.COMPLETE,
    ) -> list:
        """Build Slack Block Kit blocks for generation session completion (incl. P10Y summary)."""
        pre_deploy = notification_kind == GenerationSessionNotificationKind.CODING_COMPLETE_PRE_DEPLOY
        variance = _p10y_variance_summary(result, pre_deploy=pre_deploy)

        try:
            final_estimate = (
                result.summary.risk_assessment.final_estimate
                if result.summary.risk_assessment
                else result.summary.average_hours
            )
        except (AttributeError, KeyError):
            final_estimate = 0.0

        try:
            min_hours = result.summary.min_hours
            max_hours = result.summary.max_hours
        except (AttributeError, KeyError):
            min_hours = max_hours = 0.0

        try:
            workspace_estimations: list = result.workspace_estimations or []
        except (AttributeError, KeyError):
            workspace_estimations = []

        ws_count = len(workspace_estimations) or len(workspace_ids)

        header_text = (
            "🚀 SpecFlow — Coding complete, starting deployment & QA"
            if pre_deploy
            else "✅ SpecFlow Iteration Complete"
        )
        final_estimate_text = (
            "Pending (after P10Y phase)" if pre_deploy else f"*{final_estimate:.1f}h*"
        )
        range_text = (
            "Pending (P10Y after deploy)" if pre_deploy else f"{min_hours:.1f}h – {max_hours:.1f}h"
        )

        cost_display = _session_llm_cost_display(result)

        fields_inner = [
                    {"type": "mrkdwn", "text": f"*Spec:*\n{spec_path}"},
                    {"type": "mrkdwn", "text": f"*User:*\n{recipient_email or 'unknown'}"},
                    {"type": "mrkdwn", "text": f"*Run ID:*\n`{generation_id}`"},
                    {"type": "mrkdwn", "text": f"*{variance.label}:*\n{variance.value}"},
                    {"type": "mrkdwn", "text": f"*Final Estimate:*\n{final_estimate_text}"},
                    {"type": "mrkdwn", "text": f"*Range:*\n{range_text}"},
                    {"type": "mrkdwn", "text": f"*Workspaces Done:*\n{ws_count}"},
        ]
        if cost_display:
            fields_inner.append(
                {"type": "mrkdwn", "text": f"*Total LLM cost (cumulative):*\n{cost_display}"}
            )

        blocks: list = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": header_text, "emoji": True},
            },
            {
                "type": "section",
                "fields": fields_inner,
            },
            {"type": "divider"},
        ]

        if workspace_estimations:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Workspace Results:*"},
            })

            for ws_est in workspace_estimations:
                try:
                    ws_name = getattr(ws_est, "workspace_name", "?")
                    total_hours = getattr(ws_est, "total_hours", 0.0)
                    mu_ws = getattr(ws_est, "model_usage", None)
                    model = (mu_ws.model_name if mu_ws else None) or None
                    if mu_ws and not mu_ws.is_empty():
                        input_tokens = mu_ws.input_tokens
                        output_tokens = mu_ws.output_tokens
                    else:
                        input_tokens = None
                        output_tokens = None

                    if pre_deploy:
                        lines = [f"*{ws_name}* — *0.0h* (P10Y pending)"]
                    else:
                        lines = [f"*{ws_name}* — *{total_hours:.1f}h*"]
                    if model:
                        lines.append(f"Model: `{model}`")
                    codegen_lines = _workspace_codegen_usage_lines(ws_est)
                    if codegen_lines:
                        lines.extend(codegen_lines)
                    else:
                        tokens_str = _format_tokens_millions(input_tokens, output_tokens)
                        if tokens_str:
                            lines.append(f"Tokens: {tokens_str}")
                    cost_ln = _format_workspace_llm_cost_line(ws_est)
                    if cost_ln:
                        lines.append(cost_ln)

                    # Component breakdown: show top components by hours
                    component_breakdown = getattr(ws_est, "component_breakdown", None)
                    if component_breakdown:
                        sorted_comps = sorted(
                            component_breakdown.items(),
                            key=lambda kv: getattr(kv[1], "hours", 0.0),
                            reverse=True,
                        )
                        comp_parts = [
                            f"{name}: {getattr(comp, 'hours', 0.0):.1f}h"
                            for name, comp in sorted_comps
                        ]
                        if comp_parts:
                            lines.append("  " + " | ".join(comp_parts))

                    blocks.append({
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
                    })
                except (AttributeError, TypeError):
                    continue

        return blocks

    def _post_payload(self, payload: dict) -> None:
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(
                    self.webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
        except httpx.HTTPError as e:
            self.logger.error(f"Failed to send Slack notification: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error sending Slack notification: {e}")


class EmailNotifier(Notifier):
    def __init__(self, email_config: EmailConfig):
        self.email_config = email_config
        self.logger = logging.getLogger(__name__)

    def notify(self, message: str, recipient_email: str | None = None):
        """
        Send email notification.
        
        Args:
            message: Message content
            recipient_email: Recipient email address. If provided, overrides config recipient.
        """
        # Treat "unknown" as None to fall back to config recipient
        if recipient_email == "unknown":
            recipient_email = None
        
        # Use recipient_email if provided, otherwise fall back to config recipient
        recipient = recipient_email or settings.NOTIFY_EMAIL_USERNAME
        
        if not recipient:
            self.logger.warning("No recipient email specified, skipping email notification")
            return
        
        msg = EmailMessage()
        msg.set_content(message)
        msg['Subject'] = "SpecFlow Backend Notification"
        msg['From'] = self.email_config.username  # Sender is always from config
        msg['To'] = recipient  # Recipient can be dynamic

        try:
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                server.login(self.email_config.username, self.email_config.password)
                server.send_message(msg)
                self.logger.info(f"Email notification sent to {recipient}")
        except Exception as e:
            self.logger.error(f"Failed to send email to {recipient}: {e}")
    
    def notify_generation_session_complete(
        self,
        generation_id: str,
        workspace_ids: List[str],
        result: Any,  # MultiWorkspaceEstimationResponse
        spec_path: str,
        recipient_email: str | None = None,
        db: Optional[IDatabase] = None,
        notification_kind: GenerationSessionNotificationKind = GenerationSessionNotificationKind.COMPLETE,
    ):
        """
        Send rich email notification when a generation session completes.

        Args:
            generation_id: The generation session ID (used as branch name)
            workspace_ids: List of workspace IDs
            result: MultiWorkspaceEstimationResponse object
            spec_path: Specification path
            recipient_email: Email recipient
            db: Database interface for retrieving workspace documents
            notification_kind: ``complete`` or pre-deploy milestone variant
        """
        recipient = recipient_email or settings.NOTIFY_EMAIL_USERNAME
        
        if not recipient:
            self.logger.warning("No recipient email specified, skipping email notification")
            return
        
        # Build email content
        html_content, plain_content = self._build_generation_session_email(
            generation_id=generation_id,
            workspace_ids=workspace_ids,
            result=result,
            spec_path=spec_path,
            db=db,
            notification_kind=notification_kind,
        )
        
        msg = EmailMessage()
        msg.set_content(plain_content)
        msg.add_alternative(html_content, subtype='html')
        subject = (
            f"SpecFlow: Coding complete — deployment starting: {spec_path}"
            if notification_kind == GenerationSessionNotificationKind.CODING_COMPLETE_PRE_DEPLOY
            else f"SpecFlow Iteration Complete: {spec_path}"
        )
        msg['Subject'] = subject
        msg['From'] = self.email_config.username
        msg['To'] = recipient

        try:
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                server.login(self.email_config.username, self.email_config.password)
                server.send_message(msg)
                self.logger.info(f"Generation session completion email sent to {recipient}")
        except Exception as e:
            self.logger.error(f"Failed to send generation session completion email to {recipient}: {e}")

    def _send_simple_email(self, subject: str, body: str, recipient: str) -> None:
        msg = EmailMessage()
        msg.set_content(body)
        msg["Subject"] = subject
        msg["From"] = self.email_config.username
        msg["To"] = recipient
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self.email_config.username, self.email_config.password)
                server.send_message(msg)
            self.logger.info("Email sent to %s: %s", recipient, subject)
        except Exception as e:
            self.logger.error("Failed to send email to %s (%s): %s", recipient, subject, e)

    def _send_multipart_email(
        self, subject: str, plain_body: str, html_body: str, recipient: str
    ) -> None:
        msg = EmailMessage()
        msg.set_content(plain_body)
        msg.add_alternative(html_body, subtype="html")
        msg["Subject"] = subject
        msg["From"] = self.email_config.username
        msg["To"] = recipient
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self.email_config.username, self.email_config.password)
                server.send_message(msg)
            self.logger.info("Multipart email sent to %s: %s", recipient, subject)
        except Exception as e:
            self.logger.error("Failed to send email to %s (%s): %s", recipient, subject, e)

    def notify_spec_check_complete(
        self,
        *,
        generation_id: str,
        models_summary: str,
        usage_block: str,
        agent_models_line: str,
        recipient_email: str | None = None,
        total_usd_cost: Optional[float] = None,
        spec_path: Optional[str] = None,
    ) -> None:
        recipient = recipient_email or settings.NOTIFY_EMAIL_USERNAME
        if not recipient:
            self.logger.warning("No recipient email specified, skipping email notification")
            return
        html_content, plain_content = _build_milestone_email_html_plain(
            MilestoneNotificationKind.SPEC_CHECK,
            generation_id=generation_id,
            models_summary=models_summary,
            agent_models_line=agent_models_line,
            usage_block=usage_block,
            total_usd_cost=total_usd_cost,
            spec_path=spec_path,
        )
        subj = "SpecFlow: Spec check complete"
        if spec_path:
            subj = f"{subj} — {spec_path}"
        self._send_multipart_email(subj, plain_content, html_content, recipient)

    def notify_planning_complete(
        self,
        *,
        generation_id: str,
        models_summary: str,
        coding_plan_phase_count: int | str,
        e2e_plan_phase_count: int | str,
        usage_block: str,
        agent_models_line: str,
        recipient_email: str | None = None,
        total_usd_cost: Optional[float] = None,
        workflow_run_cost_usd: Optional[float] = None,
    ) -> None:
        recipient = recipient_email or settings.NOTIFY_EMAIL_USERNAME
        if not recipient:
            self.logger.warning("No recipient email specified, skipping email notification")
            return
        html_content, plain_content = _build_milestone_email_html_plain(
            MilestoneNotificationKind.PLANNING,
            generation_id=generation_id,
            models_summary=models_summary,
            agent_models_line=agent_models_line,
            usage_block=usage_block,
            total_usd_cost=total_usd_cost,
            coding_plan_phase_count=coding_plan_phase_count,
            e2e_plan_phase_count=e2e_plan_phase_count,
            workflow_run_cost_usd=workflow_run_cost_usd,
        )
        self._send_multipart_email(
            "SpecFlow: Planning complete",
            plain_content,
            html_content,
            recipient,
        )

    def _build_generation_session_email(
        self,
        generation_id: str,
        workspace_ids: List[str],
        result: Any,
        spec_path: str,
        db: Optional[IDatabase],
        notification_kind: GenerationSessionNotificationKind = GenerationSessionNotificationKind.COMPLETE,
    ) -> Tuple[str, str]:
        """
        Build HTML and plain text email content for generation session completion.
        
        Returns:
            Tuple of (html_content, plain_content)
        """
        pre_deploy = notification_kind == GenerationSessionNotificationKind.CODING_COMPLETE_PRE_DEPLOY
        variance = _p10y_variance_summary(result, pre_deploy=pre_deploy)

        try:
            final_estimate = result.summary.risk_assessment.final_estimate if result.summary.risk_assessment else result.summary.average_hours
        except (AttributeError, KeyError):
            final_estimate = 0.0
        
        try:
            average_hours = result.summary.average_hours
        except (AttributeError, KeyError):
            average_hours = 0.0
        
        try:
            min_hours = result.summary.min_hours
        except (AttributeError, KeyError):
            min_hours = 0.0
        
        try:
            max_hours = result.summary.max_hours
        except (AttributeError, KeyError):
            max_hours = 0.0

        final_estimate_display = (
            "Pending (reported after P10Y phase)" if pre_deploy else f"{final_estimate:.1f} hours"
        )
        average_display = (
            "Pending" if pre_deploy else f"{average_hours:.1f} hours"
        )
        range_display = (
            "Pending (P10Y after deploy)" if pre_deploy else f"{min_hours:.1f} - {max_hours:.1f} hours"
        )
        cost_display = _session_llm_cost_display(result)
        
        # Extract timestamp with safe fallback
        try:
            timestamp = result.timestamp if hasattr(result, 'timestamp') else "Unknown"
        except (AttributeError, KeyError):
            timestamp = "Unknown"
        
        # Extract workspace generations
        try:
            workspace_estimations = result.workspace_estimations if hasattr(result, 'workspace_estimations') else None
        except (AttributeError, KeyError):
            workspace_estimations = None
        
        # Build per-workspace model/token lookup from workspace_estimations
        ws_est_by_name = {}
        if workspace_estimations:
            for ws_est in workspace_estimations:
                try:
                    ws_name = getattr(ws_est, 'workspace_name', None)
                    if ws_name:
                        ws_est_by_name[ws_name] = ws_est
                except (AttributeError, TypeError):
                    continue

        # Pre-compute codegen usage lines once per workspace (reused in HTML + plain-text)
        ws_codegen_lines: dict[str, list[str]] = {
            ws_id: _workspace_codegen_usage_lines(ws_est)
            for ws_id, ws_est in ws_est_by_name.items()
        }

        # Build repository links
        repo_links = []
        if db:
            for workspace_id in workspace_ids:
                try:
                    ws_doc = db.get("workspaces", workspace_id)
                    if ws_doc and ws_doc.get("repo_url"):
                        repo_url = ws_doc["repo_url"]
                        # Remove .git suffix if present
                        if repo_url.endswith(".git"):
                            repo_url = repo_url[:-4]
                        branch_name = GitArchiveService.branch_name(generation_id)
                        branch_url = f"{repo_url}/tree/{branch_name}"
                        
                        # Get model/token info from matching WorkspaceEstimation
                        ws_est = ws_est_by_name.get(workspace_id)
                        mu_html = getattr(ws_est, "model_usage", None) if ws_est else None
                        model_name = (mu_html.model_name if mu_html else None) or None
                        input_tokens = mu_html.input_tokens if mu_html else None
                        output_tokens = mu_html.output_tokens if mu_html else None
                        agent_num_turns = mu_html.num_turns if mu_html else None
                        cache_write = mu_html.cache_write_tokens if mu_html else None
                        cache_read = mu_html.cache_read_tokens if mu_html else None
                        llm_cost = getattr(ws_est, "total_usd_cost", None) if ws_est else None

                        repo_links.append({
                            "workspace_id": workspace_id,
                            "repo_url": repo_url,
                            "branch_url": branch_url,
                            "model_name": model_name,
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "agent_num_turns": agent_num_turns,
                            "cache_write_tokens": cache_write,
                            "cache_read_tokens": cache_read,
                            "total_usd_cost": llm_cost,
                        })
                except Exception as e:
                    self.logger.warning(f"Failed to retrieve workspace {workspace_id}: {e}")
        
        # Extract component breakdown
        component_breakdown = {}
        if workspace_estimations:
            # Collect all unique components across all workspaces
            all_components = set()
            for ws_est in workspace_estimations:
                try:
                    if hasattr(ws_est, 'component_breakdown') and ws_est.component_breakdown:
                        all_components.update(ws_est.component_breakdown.keys())
                except (AttributeError, TypeError):
                    continue  # Skip this workspace if component_breakdown is missing or invalid
            
            # Build component breakdown with hours per workspace
            for component_name in sorted(all_components):
                component_data = {
                    "name": component_name,
                    "workspaces": {}
                }
                for ws_est in workspace_estimations:
                    try:
                        if (hasattr(ws_est, 'component_breakdown') and 
                            component_name in ws_est.component_breakdown):
                            comp = ws_est.component_breakdown[component_name]
                            workspace_name = getattr(ws_est, 'workspace_name', 'unknown')
                            component_data["workspaces"][workspace_name] = {
                                "hours": getattr(comp, 'hours', 0.0),
                                "new_work": getattr(comp, 'new_work', 0.0),
                                "refactor": getattr(comp, 'refactor', 0.0),
                                "rework": getattr(comp, 'rework', 0.0),
                                "quality_score": getattr(comp, 'quality_score', 0.0)
                            }
                    except (AttributeError, TypeError, KeyError):
                        continue  # Skip this workspace/component if data is missing
                component_breakdown[component_name] = component_data
        
        # Build HTML content
        html_parts = []
        html_parts.append("""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
                .header { background-color: #4CAF50; color: white; padding: 20px; border-radius: 5px 5px 0 0; }
                .content { padding: 20px; background-color: #f9f9f9; }
                .section { background-color: white; padding: 15px; margin: 10px 0; border-radius: 5px; border-left: 4px solid #4CAF50; }
                .section h2 { margin-top: 0; color: #4CAF50; }
                .summary-item { margin: 10px 0; }
                .summary-label { font-weight: bold; display: inline-block; width: 150px; }
                .repo-links-row {
                    display: flex;
                    flex-direction: row;
                    flex-wrap: wrap;
                    gap: 12px;
                    align-items: stretch;
                }
                .repo-link {
                    flex: 1 1 200px;
                    min-width: 0;
                    margin: 0;
                    padding: 10px;
                    background-color: #f0f0f0;
                    border-radius: 3px;
                    box-sizing: border-box;
                }
                .repo-link a { color: #0066cc; text-decoration: none; font-weight: bold; }
                .repo-link a:hover { text-decoration: underline; }
                table { width: 100%; border-collapse: collapse; margin: 10px 0; }
                table th, table td { padding: 8px; text-align: left; border-bottom: 1px solid #ddd; }
                table th { background-color: #4CAF50; color: white; }
                table tr:hover { background-color: #f5f5f5; }
                .footer { padding: 20px; text-align: center; color: #666; font-size: 12px; }
            </style>
        </head>
        <body>
        """)
        
        html_parts.append('<div class="header">')
        html_parts.append(
            '<h1>🚀 SpecFlow — Coding complete, starting deployment & QA</h1>'
            if pre_deploy
            else '<h1>✅ SpecFlow Iteration Complete</h1>'
        )
        html_parts.append('</div>')
        
        html_parts.append('<div class="content">')
        
        # Summary section
        html_parts.append('<div class="section">')
        html_parts.append('<h2>Summary</h2>')
        html_parts.append(f'<div class="summary-item"><span class="summary-label">Specification:</span> {spec_path}</div>')
        html_parts.append(f'<div class="summary-item"><span class="summary-label">Run ID:</span> {generation_id}</div>')
        html_parts.append(
            f'<div class="summary-item"><span class="summary-label">{variance.label}:</span> '
            f"<strong>{variance.value}</strong></div>"
        )
        html_parts.append(
            f'<div class="summary-item"><span class="summary-label">Final Estimate:</span> '
            f"<strong>{final_estimate_display}</strong></div>"
        )
        html_parts.append(
            f'<div class="summary-item"><span class="summary-label">Average:</span> {average_display}</div>'
        )
        html_parts.append(
            f'<div class="summary-item"><span class="summary-label">Range:</span> {range_display}</div>'
        )
        if cost_display:
            html_parts.append(
                '<div class="summary-item"><span class="summary-label">Total LLM cost (cumulative):</span> '
                f"<strong>{cost_display}</strong></div>"
            )
        html_parts.append('</div>')
        
        # Variants section
        if repo_links:
            html_parts.append('<div class="section">')
            html_parts.append('<h2>Variants</h2>')
            html_parts.append('<p>Click the links below to view the generation branches:</p>')
            html_parts.append('<div class="repo-links-row">')
            for repo_link in repo_links:
                html_parts.append('<div class="repo-link">')
                html_parts.append(f'<strong>{repo_link["workspace_id"]}:</strong> ')
                html_parts.append(
                    f'<a href="{repo_link["branch_url"]}" target="_blank">Branch link</a>'
                )
                model = repo_link.get("model_name")
                codegen_detail = ws_codegen_lines.get(repo_link["workspace_id"], [])
                if codegen_detail:
                    html_parts.append('<br/><span style="color:#666;font-size:13px;">')
                    if model:
                        html_parts.append(f"model: {model}<br/>")
                    html_parts.append("<br/>".join(codegen_detail))
                    html_parts.append("</span>")
                else:
                    tokens_str = _format_tokens_millions(
                        repo_link.get("input_tokens"), repo_link.get("output_tokens")
                    )
                    if model or tokens_str:
                        detail_parts = []
                        if model:
                            detail_parts.append(f"model: {model}")
                        if tokens_str:
                            detail_parts.append(f"tokens used: {tokens_str}")
                        html_parts.append(
                            f'<br/><span style="color:#666;font-size:13px;">'
                            f'{" | ".join(detail_parts)}</span>'
                        )
                llm_c = repo_link.get("total_usd_cost")
                if llm_c is not None:
                    try:
                        html_parts.append(
                            f'<br/><span style="color:#666;font-size:13px;">'
                            f"LLM API cost (cumulative): USD {float(llm_c):,.2f}</span>"
                        )
                    except (TypeError, ValueError):
                        pass
                html_parts.append('</div>')
            html_parts.append('</div>')
            html_parts.append('</div>')
        
        # Component breakdown section
        if component_breakdown:
            html_parts.append('<div class="section">')
            html_parts.append('<h2>Component Complexity Metrics Breakdown</h2>')
            html_parts.append('<table>')
            html_parts.append('<thead><tr>')
            html_parts.append('<th>Component</th>')
            # Add workspace columns
            if workspace_estimations:
                for ws_est in workspace_estimations:
                    try:
                        ws_name = getattr(ws_est, 'workspace_name', 'unknown')
                        html_parts.append(f'<th>{ws_name}<br/>(hours)</th>')
                    except (AttributeError, TypeError):
                        continue
            html_parts.append('</tr></thead>')
            html_parts.append('<tbody>')
            
            for component_name, component_data in component_breakdown.items():
                html_parts.append('<tr>')
                html_parts.append(f'<td><strong>{component_name}</strong></td>')
                # Add hours for each workspace
                if workspace_estimations:
                    for ws_est in workspace_estimations:
                        try:
                            ws_name = getattr(ws_est, 'workspace_name', 'unknown')
                            if ws_name in component_data["workspaces"]:
                                hours = component_data["workspaces"][ws_name]["hours"]
                                html_parts.append(f'<td>{hours:.1f}</td>')
                            else:
                                html_parts.append('<td>-</td>')
                        except (AttributeError, TypeError, KeyError):
                            html_parts.append('<td>-</td>')
                html_parts.append('</tr>')
            
            html_parts.append('</tbody>')
            html_parts.append('</table>')
            html_parts.append('</div>')
        
        html_parts.append('</div>')
        
        html_parts.append('<div class="footer">')
        html_parts.append(f'<p>Run ID: {generation_id} | Generated at: {timestamp}</p>')
        html_parts.append('</div>')
        
        html_parts.append('</body></html>')
        
        html_content = '\n'.join(html_parts)
        
        # Build plain text content
        plain_parts = []
        plain_parts.append("=" * 60)
        plain_parts.append(
            "SpecFlow — CODING COMPLETE, STARTING DEPLOYMENT & QA"
            if pre_deploy
            else "SpecFlow ITERATION COMPLETE"
        )
        plain_parts.append("=" * 60)
        plain_parts.append("")
        plain_parts.append(f"Specification: {spec_path}")
        plain_parts.append(f"Run ID: {generation_id}")
        plain_parts.append(f"{variance.label}: {variance.value}")
        plain_parts.append(f"Final Estimate: {final_estimate_display}")
        plain_parts.append(f"Average: {average_display}")
        plain_parts.append(f"Range: {range_display}")
        if cost_display:
            plain_parts.append(f"Total LLM cost (cumulative): {cost_display}")
        plain_parts.append("")
        
        if repo_links:
            plain_parts.append("VARIANTS:")
            plain_parts.append("-" * 60)
            for repo_link in repo_links:
                plain_parts.append(f"{repo_link['workspace_id']}: {repo_link['branch_url']}")
                model = repo_link.get("model_name")
                codegen_lines = ws_codegen_lines.get(repo_link["workspace_id"], [])
                if codegen_lines:
                    if model:
                        plain_parts.append(f"  model: {model}")
                    for ln in codegen_lines:
                        plain_parts.append(f"  {ln}")
                else:
                    tokens_str = _format_tokens_millions(
                        repo_link.get("input_tokens"), repo_link.get("output_tokens")
                    )
                    if model or tokens_str:
                        detail_parts = []
                        if model:
                            detail_parts.append(f"model: {model}")
                        if tokens_str:
                            detail_parts.append(f"tokens used: {tokens_str}")
                        plain_parts.append(f"  {' | '.join(detail_parts)}")
                llm_c = repo_link.get("total_usd_cost")
                if llm_c is not None:
                    try:
                        plain_parts.append(
                            f"  LLM API cost (cumulative): USD {float(llm_c):,.2f}"
                        )
                    except (TypeError, ValueError):
                        pass
            plain_parts.append("")
        
        if component_breakdown:
            plain_parts.append("COMPONENT BREAKDOWN:")
            plain_parts.append("-" * 60)
            # Build header
            header = "Component"
            if workspace_estimations:
                for ws_est in workspace_estimations:
                    try:
                        ws_name = getattr(ws_est, 'workspace_name', 'unknown')
                        header += f" | {ws_name}"
                    except (AttributeError, TypeError):
                        continue
            plain_parts.append(header)
            plain_parts.append("-" * len(header))
            
            for component_name, component_data in component_breakdown.items():
                row = component_name
                if workspace_estimations:
                    for ws_est in workspace_estimations:
                        try:
                            ws_name = getattr(ws_est, 'workspace_name', 'unknown')
                            if ws_name in component_data["workspaces"]:
                                hours = component_data["workspaces"][ws_name]["hours"]
                                row += f" | {hours:.1f}"
                            else:
                                row += " | -"
                        except (AttributeError, TypeError, KeyError):
                            row += " | -"
                plain_parts.append(row)
            plain_parts.append("")
        
        plain_parts.append(f"Generated at: {timestamp}")
        plain_content = '\n'.join(plain_parts)
        
        return html_content, plain_content


# Initialize Notifications
notifications = Notifications()

# Add SlackNotifier by default if webhook URL is configured
if settings.SLACK_WEBHOOK_URL:
    notifications.add_notifier(SlackNotifier(settings.SLACK_WEBHOOK_URL))
    logger.info("Notifications initialized with Slack webhook")

# Add EmailNotifier if email config is available
email_config = settings.get_email_config()
if email_config:
    notifications.add_notifier(EmailNotifier(email_config))
    logger.info(f"Notifications initialized with email config: sender:{email_config.username}")
