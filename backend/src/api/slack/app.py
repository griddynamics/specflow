"""``SlackGaps``: the SpecFlow Slack app in Socket Mode, run by the API process (docs/slack/gap-answering.md "The
design", the phase 0 spike).

Socket Mode opens an outbound WebSocket to Slack: no public endpoint. What it handles:

- ``/specflow link``: a one-time link (``SlackLinks``) that the person opens while signed in to SpecFlow.
- ``/specflow connect <project id>``: run in a channel by a member of the project (the command is copied from
  SpecFlow), it connects the channel to the project, records the member as the project's Slack integration
  (``project_integrations``) and posts its open questions there. A caller not linked yet gets
  the link first, and the channel is connected as soon as they have signed in. ``/specflow disconnect`` undoes it.
- ``/specflow answer [project]``: a modal opened at once ("Loading…", within the 3 s of the trigger) and filled with
  the caller's open questions, theirs first, then unassigned ones (``blocks.answer_view``).
- The modal's submit: acknowledged at once with "Saving…", then each answer goes through ``record_answer`` as the
  linked user with ``via`` ``slack``, exactly as a web answer, and "Apply now" calls ``apply_now``.

Every action checks that the Slack user is linked and that the linked user is a member of the project. Logs carry
ids, codes and counts, never gap or Slack text.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from agent_contracts import GapDetail
from api.agent_views import holders_of
from api.contained import Contained
from api.gap.answering import record_answer
from api.gap.cards import detail_user_ids, gap_detail
from api.plan.start import (
    GRAPH_BUILD_RUNNING,
    NO_GRAPH_BUILD,
    PLAN_UNAVAILABLE,
    PlanStartRefusedError,
    start_plan_run,
)
from api.slack.blocks import CALLBACK_ID, PAGE_SIZE, SlackAnswer, answer_view, escaped, message_view, read_answers
from api.slack.channels import SlackChannels
from api.slack.links import NONCE_TTL, SlackLinks, SlackUser
from api.slack.threads import (
    NOT_A_CHANNEL,
    NOT_IN_CHANNEL,
    SHARED,
    TAKEN,
    ChannelRefusedError,
    SlackThreads,
    assign_answerer,
)
from db import integrations as integrations_db
from db import repositories as repo
from db.integrations import SLACK as SLACK_APP
from db.integrations import WorkspaceMismatchError
from graph.gap import AnswerInput, AnswerInvalidError, GapNotFoundError, GapStateError, StaleGapError
from graph.gap.assignment import NO_ASSIGNEE
from logger.logger import INFO, WARNING, get_logger, log_event
from settings import SlackAppSettings

logger = get_logger(__name__)

COMMAND: Final = "/specflow"
LINK_PATH: Final = "/api/integrations/slack/link"
ASK: Final = "ask"
PROJECTS_MAX: Final = 100
STAGED_MAX: Final = 200
HELP: Final = (
    "`/specflow link`: connect your Slack account to SpecFlow (once).\n"
    "`/specflow connect <project id>`: connect this channel to a SpecFlow project you are a member of, so its open "
    "questions are posted here (copy the command from SpecFlow); `/specflow disconnect` undoes it.\n"
    "`/specflow plan [project]`: start the implementation plan, as the Plan screen's button does "
    "(in a project's channel the project is that one); `/specflow replan [project]` recreates it.\n"
    "`/specflow answer <project>`: answer your open questions of a project in a form."
)
PLAN_REFUSED: Final = {
    PLAN_UNAVAILABLE: "Plans are not set up on this SpecFlow server.",
    GRAPH_BUILD_RUNNING: "The project's knowledge is being built; start the plan when the build has finished.",
    NO_GRAPH_BUILD: "The project has no built knowledge yet; build it in SpecFlow first.",
}
CONNECT_USAGE: Final = "Copy the command from SpecFlow: `/specflow connect <project id>`."
NOT_MEMBER: Final = "Only a member of the project can connect it, with the command from SpecFlow."
WORKSPACE_MISMATCH: Final = (
    "This project is connected to another Slack workspace; remove that connection in SpecFlow first."
)
CONNECT_REFUSED: Final = {
    NOT_A_CHANNEL: "Run the command in the channel to connect, not in a direct message.",
    NOT_IN_CHANNEL: "Add SpecFlow to this channel first (`/invite @SpecFlow`), then run the command again.",
    SHARED: "This channel is shared outside the workspace; connect an internal channel.",
    TAKEN: "This channel is connected to another SpecFlow project; a member must run `/specflow disconnect` first.",
}


class Say(Protocol):
    """Answers the caller: ``public`` goes to the channel, else only the caller sees it."""

    async def __call__(self, text: str, *, public: bool = False) -> None: ...


@dataclass(frozen=True)
class _Connecting:
    """A connect waiting for its caller to sign in (in this process, as long as the link nonce)."""

    project: str
    channel_id: str
    until: datetime


class SlackGaps:
    """The Slack app of the runtime (``Runtime.slack``)."""

    def __init__(self, settings: SlackAppSettings, runtime: Any, state_dir: Path) -> None:
        self._settings = settings
        self._runtime = runtime
        self.links = SlackLinks(state_dir)
        self._connecting: dict[str, _Connecting] = {}
        self._team: tuple[str, str] | None = None  # the workspace of the bot token: its id and name
        self._app = AsyncApp(token=settings.bot_token.get_secret_value(), logger=get_logger("api.slack.bolt"))
        self.threads = SlackThreads(
            self._app.client,
            runtime,
            self.links,
            SlackChannels(state_dir),
            public_url=settings.public_url,
            link_text=self._link_text,
        )
        self._app.command(COMMAND)(self._command)
        self._app.view(CALLBACK_ID)(self._submit)
        self._app.event("message")(self.threads.on_message)
        # slack-bolt leaves connect_async and close_async untyped
        self._handler: Any = AsyncSocketModeHandler(self._app, settings.app_token.get_secret_value())

    async def start(self) -> None:
        await self._handler.connect_async()
        log_event(logger, INFO, "api.slack.connected")
        with Contained() as filled:  # a failure here never turns Slack off; the next start tries again
            await self._record_bindings()
        if filled.failed:
            log_event(logger, WARNING, "api.slack.integrations_failed", error_type=filled.error_type)

    async def aclose(self) -> None:
        await self._handler.close_async()

    async def linked(self, slack: SlackUser) -> None:
        """Tell the Slack user that the link is made (a DM from the app) and record the thread replies they sent
        before linking."""

        await self._app.client.chat_postMessage(
            channel=slack.user_id, text="Your Slack account is linked to SpecFlow. You can answer questions in threads."
        )
        await self.threads.replay(slack)
        waiting = self._connecting.pop(slack.key, None)
        user_id = await self.links.user_of(slack)
        if waiting is not None and user_id is not None and waiting.until > datetime.now(UTC):
            await self._connect_as(slack, user_id, waiting.project, waiting.channel_id, self._poster(slack, waiting))

    # /specflow ---------------------------------------------------------------------------------------------------

    async def _command(self, ack: Any, body: dict[str, Any], respond: Any, client: Any) -> None:
        await ack()
        slack = SlackUser(str(body.get("team_id", "")), str(body.get("user_id", "")))
        words = str(body.get("text") or "").split(maxsplit=1)
        action = words[0].lower() if words else "help"
        if action == "link":
            await respond(text=self._link_text(slack), response_type="ephemeral")
        elif action == "answer":
            await self._open(slack, words[1] if len(words) > 1 else "", str(body.get("trigger_id")), respond, client)
        elif action == "connect":
            wanted = words[1].strip() if len(words) > 1 else ""
            await self._connect(slack, wanted, str(body.get("channel_id", "")), _responder(respond))
        elif action == "disconnect":
            await self._disconnect(slack, str(body.get("channel_id", "")), _responder(respond))
        elif action in {"plan", "replan"}:
            wanted = words[1].strip() if len(words) > 1 else ""
            await self._plan(slack, wanted, str(body.get("channel_id", "")), respond, replan=action == "replan")
        else:
            await respond(text=HELP, response_type="ephemeral")

    async def _connect(self, slack: SlackUser, wanted: str, channel_id: str, say: Say) -> None:
        if not wanted:
            await say(CONNECT_USAGE)
            return
        user_id = await self.links.user_of(slack)
        if user_id is None:
            self._connecting[slack.key] = _Connecting(wanted, channel_id, datetime.now(UTC) + NONCE_TTL)
            link = self._link_text(slack)
            await say(f"Sign in to SpecFlow to connect this channel: {link} It is connected once you have.")
            return
        await self._connect_as(slack, user_id, wanted, channel_id, say)

    async def _connect_as(self, slack: SlackUser, user_id: uuid.UUID, wanted: str, channel_id: str, say: Say) -> None:
        """Connect the channel to the project ``wanted`` (its id) when ``user_id`` is a member, record them as the
        project's Slack integration, then post its open questions there. A project the caller is not a member of and
        one that does not exist get the same answer."""

        project = await self._member(user_id, wanted)
        if project is None:
            await say(NOT_MEMBER)
            return
        project_id = str(project.id)
        team = await self._workspace() or (slack.team_id, slack.team_id)
        if await self._other_workspace(project.id, team[0]):
            await say(WORKSPACE_MISMATCH)
            return
        try:
            earlier = await self.threads.connect(project_id, channel_id, by=str(user_id))
        except ChannelRefusedError as error:
            log_event(
                logger,
                INFO,
                "api.slack.connect_refused",
                user_id=str(user_id),
                project_id=project_id,
                error_code=error.code,
            )
            await say(CONNECT_REFUSED[error.code])
            return
        await self._bind_integration(project.id, user_id, team)
        name = escaped(project.name, 150)
        if earlier is not None and earlier.channel_id == channel_id:
            await say(f"This channel is already connected to *{name}*.")
        else:
            await say(
                f"🔗 <@{slack.user_id}> connected this channel to the SpecFlow project *{name}*. Its open questions "
                "follow; reply in a question's thread to answer it.",
                public=True,
            )
        await self.threads.post_open(project_id)

    async def _disconnect(self, slack: SlackUser, channel_id: str, say: Say) -> None:
        user_id = await self.links.user_of(slack)
        if user_id is None:
            await say(f"First connect your account: {self._link_text(slack)}")
            return
        project_id = await self.threads.channels.project_of(channel_id)
        if project_id is None:
            await say("This channel is not connected to a SpecFlow project.")
            return
        if await self._member(user_id, project_id) is None:
            await say("Only a member of the connected SpecFlow project can disconnect it.")
            return
        await self.disconnect(project_id)
        log_event(logger, INFO, "api.slack.unbound", project_id=project_id, user_id=str(user_id))
        await say(
            f"🔌 <@{slack.user_id}> disconnected this channel from SpecFlow; no more questions are posted here.",
            public=True,
        )

    async def disconnect(self, project_id: str) -> None:
        """Unbind the project's channel and remove its Slack integration (the web route and the command)."""

        await self.threads.channels.unbind(project_id)
        async with self._runtime.transaction() as session:
            await integrations_db.delete_integration(session, uuid.UUID(project_id), SLACK_APP)

    async def _workspace(self) -> tuple[str, str] | None:
        """The id and name of the Slack workspace of the bot token (asked once); ``None`` when Slack does not say."""

        if self._team is None:
            with Contained() as asked:
                answer = await self._app.client.auth_test()
            team_id = None if asked.failed else str(answer.get("team_id") or "")
            if not team_id:
                return None
            self._team = (team_id, str(answer.get("team") or team_id))
        return self._team

    async def _other_workspace(self, project_id: uuid.UUID, team_id: str) -> bool:
        """The project's Slack integration is bound to another workspace and a member acts on it."""

        async with self._runtime.transaction() as session:
            bound = await integrations_db.get_integration(session, project_id, SLACK_APP)
        return bound is not None and bound.user_id is not None and bound.workspace_id != team_id

    async def _bind_integration(self, project_id: uuid.UUID, user_id: uuid.UUID, team: tuple[str, str]) -> None:
        """Record ``user_id`` as the member the project's Slack channel is connected by."""

        try:
            async with self._runtime.transaction() as session:
                await integrations_db.bind_integration(
                    session,
                    project_id,
                    SLACK_APP,
                    user_id=user_id,
                    workspace_id=team[0],
                    workspace_name=team[1],
                    now=datetime.now(UTC),
                )
        except WorkspaceMismatchError:  # another connect moved the workspace since the check; the channel stays bound
            log_event(logger, WARNING, "api.slack.integration_mismatch", project_id=str(project_id))

    async def _record_bindings(self) -> None:
        """Give every channel binding made before the integrations table existed its row: the member who ran the
        connect, while they are still a member. A binding that has a row is left as it is."""

        team = await self._workspace()
        if team is None:
            return
        for bound in await self.threads.channels.bindings():
            project_id, user_id = uuid.UUID(bound.project_id), uuid.UUID(bound.bound_by)
            async with self._runtime.transaction() as session:
                if await repo.get_membership(session, user_id, project_id) is None:
                    continue
                await integrations_db.add_missing_integration(
                    session,
                    project_id,
                    SLACK_APP,
                    user_id=user_id,
                    workspace_id=team[0],
                    workspace_name=team[1],
                    now=datetime.now(UTC),
                )

    async def _member(self, user_id: uuid.UUID, wanted: str) -> Any:
        """The project with the id ``wanted`` when ``user_id`` is a member of it; ``None`` otherwise."""

        try:
            project_id = uuid.UUID(wanted)
        except ValueError:
            return None
        async with self._runtime.transaction() as session:
            found = await repo.get_membership(session, user_id, project_id)
        return None if found is None else found[0]

    def _poster(self, slack: SlackUser, waiting: _Connecting) -> Say:
        """``Say`` for a connect finished after the link: the slash command's reply is gone, so the app posts."""

        async def say(text: str, *, public: bool = False) -> None:
            if public:
                await self._app.client.chat_postMessage(channel=waiting.channel_id, text=text)
            else:
                await self._app.client.chat_postEphemeral(channel=waiting.channel_id, user=slack.user_id, text=text)

        return say

    async def _plan(self, slack: SlackUser, wanted: str, channel_id: str, respond: Any, *, replan: bool) -> None:
        """Start the project's plan run as the Plan screen's button does (``start_plan_run``), as the linked user, and
        say so in the channel. The project: the one named, else the one bound to this channel, else the caller's
        only one."""

        user_id = await self.links.user_of(slack)
        if user_id is None:
            await respond(text=f"First connect your account: {self._link_text(slack)}", response_type="ephemeral")
            return
        if not wanted:
            wanted = await self.threads.channels.project_of(channel_id) or ""
        project = await self._project(user_id, wanted)
        if project is None:
            await respond(text="Name the project: `/specflow plan <project>`.", response_type="ephemeral")
            return
        project_id = str(project.id)
        try:
            await start_plan_run(self._runtime, project_id, user_id, replan=replan)
        except PlanStartRefusedError as error:
            await respond(text=PLAN_REFUSED[error.code], response_type="ephemeral")
            return
        verb = "recreating" if replan else "creating"
        text = f"📋 <@{slack.user_id}> started {verb} the implementation plan of *{escaped(project.name, 150)}*."
        waiting = await self._open_count(project_id)
        if waiting:
            text += f" {waiting} questions are still open."
        await respond(text=text, response_type="in_channel")
        log_event(logger, INFO, "api.slack.plan_started", user_id=str(user_id), project_id=project_id, replan=replan)

    async def _open_count(self, project_id: str) -> int:
        if self._runtime.gaps is None:
            return 0
        service = await self._runtime.gaps.service()
        page = await service.list_gaps(project_id, status=["open"], route=[ASK], limit=STAGED_MAX)
        return len(page.gaps)

    def _link_text(self, slack: SlackUser) -> str:
        base = self._settings.public_url
        if base is None:
            return "Linking needs the SpecFlow address (PUBLIC_URL), which this server does not have."
        nonce = self.links.nonce_for(slack)
        return f"<{base}{LINK_PATH}?nonce={nonce}|Connect your SpecFlow account> (valid 10 minutes, for you only)."

    async def _open(self, slack: SlackUser, wanted: str, trigger_id: str, respond: Any, client: Any) -> None:
        user_id = await self.links.user_of(slack)
        if user_id is None:
            await respond(text=f"First connect your account: {self._link_text(slack)}", response_type="ephemeral")
            return
        project = await self._project(user_id, wanted.strip())
        if project is None:
            await respond(text="No such project of yours. Use its name or id.", response_type="ephemeral")
            return
        opened = await client.views_open(trigger_id=trigger_id, view=message_view("Answer gaps", ["Loading…"]))
        view_id = opened["view"]["id"]
        gaps, staged, ai = await self._questions(str(project.id), user_id)
        if not gaps:
            view = message_view("Answer gaps", [f"No open questions for you in {project.name}."])
        else:
            view = answer_view(str(project.id), project.name, gaps, staged_by_others=staged, ai_staged=ai)
        await client.views_update(view_id=view_id, view=view)
        log_event(logger, INFO, "api.slack.opened", user_id=str(user_id), project_id=str(project.id), gaps=len(gaps))

    async def _project(self, user_id: uuid.UUID, wanted: str) -> Any:
        """The caller's project named ``wanted`` (its id, or its name ignoring case); the only one when empty."""

        async with self._runtime.transaction() as session:
            projects = (await repo.list_projects(session, user_id, PROJECTS_MAX)).items
        if not wanted:
            return projects[0] if len(projects) == 1 else None
        folded = wanted.casefold()
        found = [p for p in projects if str(p.id) == wanted or p.name.casefold() == folded]
        return found[0] if len(found) == 1 else None

    async def _questions(self, project_id: str, user_id: uuid.UUID) -> tuple[list[GapDetail], int, int]:
        """The caller's open questions, then unassigned ones (at most ``PAGE_SIZE``), each read whole; and how many
        staged answers of people and of AI "Apply now" would also apply."""

        service = await self._runtime.gaps.service()
        ids: list[str] = []
        for assigned in (str(user_id), NO_ASSIGNEE):
            page = await service.list_gaps(
                project_id, status=["open"], route=[ASK], assigned_to=assigned, limit=PAGE_SIZE
            )
            ids.extend(gap.gap_id for gap in page.gaps if gap.gap_id not in ids)
        details: list[GapDetail] = []
        public_url = self._settings.public_url
        for gap_id in ids[:PAGE_SIZE]:
            gap = await service.get_gap(project_id, gap_id)
            holders = await holders_of(self._runtime, uuid.UUID(project_id), user_id, detail_user_ids(gap))
            details.append(gap_detail(gap, project_id, public_url, holders))
        staged = await service.list_gaps(project_id, status=["answered"], route=[ASK], limit=STAGED_MAX)
        ai = await service.list_gaps(project_id, status=["answered"], route=["ai"], limit=STAGED_MAX)
        return details, len(staged.gaps), len(ai.gaps)

    # The submit --------------------------------------------------------------------------------------------------

    async def _submit(self, ack: Any, body: dict[str, Any], view: dict[str, Any], client: Any) -> None:
        submitted = read_answers(view)
        if submitted.errors:
            await ack(response_action="errors", errors=dict(submitted.errors))
            return
        await ack(response_action="update", view=message_view("Answer gaps", ["Saving your answers…"]))
        user = body.get("user", {})
        slack = SlackUser(str(user.get("team_id") or body.get("team", {}).get("id", "")), str(user.get("id", "")))
        lines = await self._record(slack, submitted.project_id, submitted.answers, apply_now=submitted.apply_now)
        await client.views_update(view_id=body["view"]["id"], view=message_view("Answer gaps", lines))

    async def _record(
        self, slack: SlackUser, project_id: str, answers: Sequence[SlackAnswer], *, apply_now: bool
    ) -> list[str]:
        user_id = await self.links.user_of(slack)
        if user_id is None:
            return ["Your Slack account is not linked to SpecFlow any more. Run `/specflow link`."]
        async with self._runtime.transaction() as session:
            member = await repo.get_member_project(session, user_id, uuid.UUID(project_id))
        if member is None:
            return ["You are not a member of this project."]
        lines: list[str] = []
        saved = 0
        for answer in answers:
            outcome = await self._answer(project_id, user_id, answer)
            saved += outcome == "saved"
            lines.append(f"{answer.gap_id}: {outcome}")
        if not answers:
            lines.append("Nothing chosen: no answer saved.")
        if apply_now:
            lines.append(await self._apply(project_id, user_id))
        log_event(
            logger,
            INFO,
            "api.slack.answered",
            user_id=str(user_id),
            project_id=project_id,
            answers=len(answers),
            saved=saved,
            apply_now=apply_now,
        )
        return lines

    async def _answer(self, project_id: str, user_id: uuid.UUID, answer: SlackAnswer) -> str:
        recorded = AnswerInput(
            gap_id=answer.gap_id,
            option_rank=answer.option_rank,
            own_text=answer.own_text,
            answered_by=str(user_id),
            updated_at=answer.updated_at,
            via="slack",
        )
        try:
            await record_answer(self._runtime.gaps, project_id, recorded)
        except StaleGapError:
            return "changed since you opened it; open the form again"
        except GapStateError as error:
            return f"not saved ({error.fields.get('status', 'state')}): answer it on the web"
        except GapNotFoundError:
            return "no longer open"
        except AnswerInvalidError as error:
            log_event(logger, WARNING, "api.slack.answer_refused", project_id=project_id, error_code=error.code)
            return "not saved: answer it on the web"
        await assign_answerer(self._runtime.gaps, project_id, answer.gap_id, user_id)
        return "saved"

    async def _apply(self, project_id: str, user_id: uuid.UUID) -> str:
        service = await self._runtime.gaps.service()
        try:
            result = await service.apply_now(project_id, by=str(user_id))
        except GapStateError as error:
            if error.fields.get("status") == "nothing_pending":
                return "Apply now: nothing waits to be applied (someone may have applied it already)."
            return "Apply now: not started; use “Resolve gaps” on the web."
        return f"Apply now: {len(result.gap_ids)} answers go into one update build."


def _responder(respond: Any) -> Say:
    async def say(text: str, *, public: bool = False) -> None:
        await respond(text=text, response_type="in_channel" if public else "ephemeral")

    return say


async def start_slack(runtime: Any, settings: SlackAppSettings | None, state_dir: str | None) -> None:
    """Connect the Slack app (``SLACK_*_TOKEN``) when the gap runner runs; it closes before the runtime's other parts.
    A failed connection is logged and leaves Slack off, never the API."""

    if settings is None or runtime.gaps is None or state_dir is None:
        return
    slack_app = SlackGaps(settings, runtime, Path(state_dir))
    with Contained() as started:
        await slack_app.start()
    if started.error is not None:
        log_event(logger, WARNING, "api.slack.unavailable", error_type=type(started.error).__name__)
        return
    runtime.slack = slack_app
    runtime.cleanups.append(slack_app.aclose)
    runtime.gaps.add_notice(slack_app.threads)
