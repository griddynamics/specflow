"""The Slack routes (docs/slack/gap-answering.md "The channel flow").

- ``POST /integrations/slack/link``: the web app's route handler forwards the one-time nonce that Slack gave the
  person, from their signed-in session; the Slack user is then linked to the caller.
- ``/projects/{pid}/slack``: the project's Slack channel. A member reads it and disconnects it. A channel is connected
  from inside Slack (``/specflow connect <project id>``), so no other channel is ever listed here.

No nonce or Slack text is logged; events carry ids and a code.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from api.contained import Contained
from api.deps import MemberDep, RuntimeDep, UserDep
from api.errors import ApiError
from api.slack.links import LinkRefusedError
from api.slack.threads import SlackThreads
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("api.routers.slack")
router = APIRouter(tags=["slack"])


class SlackLinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nonce: str = Field(min_length=1, max_length=200)


class SlackLinkOut(BaseModel):
    linked: bool


class SlackChannelOut(BaseModel):
    id: str
    name: str
    private: bool


class SlackSettingsOut(BaseModel):
    """``available``: the server runs the Slack app; ``channel`` the connected one (none when not connected);
    ``editable``: the caller may disconnect it (every member)."""

    available: bool
    channel: SlackChannelOut | None
    editable: bool


def slack_unavailable() -> ApiError:
    return ApiError(503, "slack_unavailable", "Slack is not configured on this server.")


@router.post("/integrations/slack/link", response_model=SlackLinkOut)
async def link_slack(body: SlackLinkIn, runtime: RuntimeDep, user: UserDep) -> SlackLinkOut:
    """Link the Slack user the nonce was made for to the caller, and tell them in Slack."""

    slack_app = runtime.slack
    if slack_app is None:
        raise slack_unavailable()
    try:
        slack = await slack_app.links.link(body.nonce, user.id)
    except LinkRefusedError:
        log_event(logger, INFO, "api.slack.link.refused", user_id=str(user.id))
        raise ApiError(400, "slack_link_invalid", "The link expired or was used. Run /specflow link again.") from None
    log_event(logger, INFO, "api.slack.linked", user_id=str(user.id))
    with Contained() as told:  # the link is made; the Slack message is a courtesy
        await slack_app.linked(slack)
    if told.error is not None:
        log_event(
            logger, WARNING, "api.slack.notify_failed", user_id=str(user.id), error_type=type(told.error).__name__
        )
    return SlackLinkOut(linked=True)


async def _settings(threads: SlackThreads | None, project_id: str, *, editable: bool) -> SlackSettingsOut:
    if threads is None:
        return SlackSettingsOut(available=False, channel=None, editable=editable)
    bound = await threads.channels.binding(project_id)
    channel = (
        None if bound is None else SlackChannelOut(id=bound.channel_id, name=bound.channel_name, private=bound.private)
    )
    return SlackSettingsOut(available=True, channel=channel, editable=editable)


def _threads(runtime: RuntimeDep) -> SlackThreads | None:
    return None if runtime.slack is None else runtime.slack.threads


@router.get("/projects/{pid}/slack", response_model=SlackSettingsOut)
async def get_slack_settings(runtime: RuntimeDep, access: MemberDep) -> SlackSettingsOut:
    return await _settings(_threads(runtime), str(access.project.id), editable=True)


@router.delete("/projects/{pid}/slack", status_code=204)
async def delete_slack_settings(runtime: RuntimeDep, user: UserDep, access: MemberDep) -> Response:
    if runtime.slack is None:
        raise slack_unavailable()
    await runtime.slack.disconnect(str(access.project.id))
    log_event(logger, INFO, "api.slack.unbound", project_id=str(access.project.id), user_id=str(user.id))
    return Response(status_code=204)
