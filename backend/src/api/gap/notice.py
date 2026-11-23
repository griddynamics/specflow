"""``GapNotice``: after a gap run that asks people new questions, one POST to a Slack webhook, which posts it to a
channel (docs/slack/gap-answering.md "Before the app is approved"): the webhook trigger of a Workflow Builder workflow
(``/triggers/``, paid plans) gets the variables, an app's incoming webhook (``/services/``, any plan) a ready text.

The notice holds the project name, counts and the link to the Gaps screen: no gap text reaches Slack (decision O2).
Only the projects in ``SLACK_WORKFLOW_PROJECTS`` are announced. A failed POST is logged and never fails the run;
the webhook URL is a secret and never logged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

import httpx

from api.contained import Contained
from graph.gap import GapRunResult
from logger.logger import INFO, WARNING, get_logger, log_event
from settings import SlackNoticeSettings

logger = get_logger(__name__)

NOTICE_TIMEOUT_SECONDS: Final = 10.0

ProjectName = Callable[[str], Awaitable[str | None]]


def notice_payload(result: GapRunResult, *, project: str, url: str) -> dict[str, str] | None:
    """The workflow's variables for a run's new ``ask`` gaps, all text as webhook variables are; ``None`` when the run
    asks nobody anything."""

    asked = _count(_mapping(result.counts.get("routes")).get("ask"))
    if asked == 0:
        return None
    severity = _mapping(_mapping(result.counts.get("gaps_new")).get("by_severity"))
    return {
        "project": project,
        "new_questions": str(asked),
        "blockers": str(_count(severity.get("blocker"))),
        "majors": str(_count(severity.get("major"))),
        "url": url,
    }


def notice_text(payload: Mapping[str, str]) -> str:
    """The message an incoming webhook posts: the variables in one line, the project name escaped for Slack."""

    project = payload["project"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = (
        f"{project}: {payload['new_questions']} new questions "
        f"({payload['blockers']} blockers, {payload['majors']} major)."
    )
    return f"{text} <{payload['url']}|Answer in SpecFlow>" if payload["url"] else text


class GapNotice:
    """Posts the notice of a finished gap run; held by the gap runner."""

    def __init__(
        self,
        settings: SlackNoticeSettings,
        project_name: ProjectName,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._project_name = project_name
        self._client = client or httpx.AsyncClient(timeout=NOTICE_TIMEOUT_SECONDS)

    async def send(self, project_id: str, result: GapRunResult) -> None:
        """Announce ``result`` when its project is listed and it asked new questions; never raises."""

        if project_id not in self._settings.projects or result.status == "failed":
            return
        sent: tuple[httpx.Response, int] | None = None
        with Contained() as posted:  # the notice never fails the run
            sent = await self._post(project_id, result)
        if posted.error is not None or (sent is not None and sent[0].is_error):
            log_event(
                logger,
                WARNING,
                "api.gap.notice_failed",
                project_id=project_id,
                run_id=result.run_id,
                error_type=None if posted.error is None else type(posted.error).__name__,
                status_code=None if sent is None else sent[0].status_code,
            )
        elif sent is not None:
            log_event(
                logger,
                INFO,
                "api.gap.notice_sent",
                project_id=project_id,
                run_id=result.run_id,
                new_questions=sent[1],
            )

    async def _post(self, project_id: str, result: GapRunResult) -> tuple[httpx.Response, int] | None:
        """The webhook's response and the number of new questions; ``None`` when the run asked nobody anything."""

        name = await self._project_name(project_id) or project_id
        payload = notice_payload(result, project=name, url=self._gaps_url(project_id))
        if payload is None:
            return None
        hook = self._settings.webhook_url.get_secret_value()
        body = payload if "/triggers/" in hook else {"text": notice_text(payload)}
        response = await self._client.post(hook, json=body)
        return response, int(payload["new_questions"])

    async def aclose(self) -> None:
        await self._client.aclose()

    def _gaps_url(self, project_id: str) -> str:
        base = self._settings.public_url
        return "" if base is None else f"{base}/projects/{project_id}/gaps"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
