"""``SlackThreads``: the channel flow (docs/slack/gap-answering.md "The channel flow").

- **Connect.** A project member runs ``/specflow connect <project id>`` in a channel the app is in; every open
  question of the project (route ``ask``) is posted there, one message each, and so is every new one after each gap
  run. A channel belongs to one project, and the command proves the caller is in it.
- **Answer in the thread.** Anyone in the channel answers by replying in a gap's thread: one option letter, or their
  own words. The reply is recorded as the replier's SpecFlow user (``via`` ``slack``) and applied at once
  (``apply_now``, one update build; a build that runs takes the next answers in one queued request).
- **Who.** A replier links their Slack account once (``/specflow link``, or the link the app shows them privately in
  the thread); a reply sent before that waits in this process and is recorded when they link.

Every reply checks that the replier's SpecFlow user is a member of the project. Logs carry ids, codes and counts,
never gap or Slack text.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from agent_contracts import GapDetail
from api.agent_views import holders_of
from api.contained import Contained
from api.gap.cards import detail_user_ids, gap_detail
from api.gap.replies import assign_answerer as assign_to_answerer
from api.gap.replies import record_and_apply
from api.slack.blocks import CONTRADICTION, escaped, gap_details, gap_headline, reply_answer
from api.slack.channels import Binding, ChannelTakenError, Posted, SlackChannels
from api.slack.links import SlackLinks, SlackUser
from db import repositories as repo
from graph.gap import AnswerInput, AnswerInvalidError, GapNotFoundError, GapStateError
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger(__name__)

ASK: Final = "ask"
OPEN_MAX: Final = 50  # questions posted at once (a bind, a gap run); the rest follow after the next run
NOT_A_CHANNEL: Final = "not_a_channel"
NOT_IN_CHANNEL: Final = "not_in_channel"
SHARED: Final = "shared"
TAKEN: Final = "taken"
NO_CHANNEL: Final = "no_channel"
UNREACHABLE: Final = "unreachable"
LEFT_CHANNEL: Final = frozenset({"not_in_channel", "channel_not_found", "is_archived"})  # Slack's answers to a post
ASSIGN_FAILED: Final = "api.slack.assign_failed"
PENDING_MAX: Final = 20  # replies kept per unlinked Slack user


@dataclass(frozen=True)
class SlackChannel:
    channel_id: str
    name: str
    private: bool


@dataclass(frozen=True)
class _Reply:
    channel_id: str
    thread_ts: str
    text: str


class ChannelRefusedError(Exception):
    """The channel cannot be connected; ``code``: ``NOT_A_CHANNEL`` (a DM), ``NOT_IN_CHANNEL`` (the app is not in
    it), ``SHARED`` (shared outside the workspace) or ``TAKEN`` (connected to another project)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WelcomeFailedError(Exception):
    """The welcome message was not posted; ``code``: ``NO_CHANNEL`` (the project has no channel), ``NOT_IN_CHANNEL``
    (the app left or cannot post in it) or ``UNREACHABLE`` (Slack did not answer)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def assign_answerer(runner: Any, project_id: str, gap_id: str, user_id: uuid.UUID) -> None:
    """After a Slack answer: the shared assignment (``api.gap.replies``), its failure logged as a Slack event."""

    await assign_to_answerer(runner, project_id, gap_id, user_id, event=ASSIGN_FAILED)


class SlackThreads:
    """Posts a bound project's questions and records the answers given in their threads."""

    def __init__(
        self,
        client: Any,
        runtime: Any,
        links: SlackLinks,
        channels: SlackChannels,
        *,
        public_url: str | None,
        link_text: Callable[[SlackUser], str],
    ) -> None:
        self._client = client
        self._runtime = runtime
        self._links = links
        self.channels = channels
        self._public_url = public_url
        self._link_text = link_text
        self._pending: dict[str, list[_Reply]] = {}

    # Binding ------------------------------------------------------------------------------------------------------

    async def connect(self, project_id: str, channel_id: str, *, by: str) -> Binding | None:
        """Connect the project to the channel and return its earlier binding (the same channel: nothing changed).
        ``ChannelRefusedError`` for a DM, a channel the app is not in or one shared outside the workspace, and for
        one connected to another project. The caller posts the open questions (``post_open``)."""

        channel = await self._channel(channel_id)
        try:
            earlier = await self.channels.bind(Binding(project_id, channel_id, channel.name, by, channel.private))
        except ChannelTakenError:
            raise ChannelRefusedError(TAKEN) from None
        log_event(logger, INFO, "api.slack.bound", project_id=project_id, user_id=by)
        return earlier

    async def _channel(self, channel_id: str) -> SlackChannel:
        if not channel_id.startswith(("C", "G")):
            raise ChannelRefusedError(NOT_A_CHANNEL)
        with Contained() as asked:  # Slack answers channel_not_found for a private channel the app is not in
            answer = await self._client.conversations_info(channel=channel_id)
        if asked.error is not None:
            raise ChannelRefusedError(NOT_IN_CHANNEL)
        found = answer.get("channel") or {}
        if found.get("is_im") or found.get("is_mpim"):
            raise ChannelRefusedError(NOT_A_CHANNEL)
        if found.get("is_ext_shared"):
            raise ChannelRefusedError(SHARED)
        if not found.get("is_member"):
            raise ChannelRefusedError(NOT_IN_CHANNEL)
        return SlackChannel(channel_id, str(found.get("name", "")), bool(found.get("is_private")))

    # Posting ------------------------------------------------------------------------------------------------------

    async def post_welcome(self, project_id: str, project_name: str) -> Binding:
        """Post the welcome message in the project's channel so the person sees the connection work; the binding.
        ``WelcomeFailedError`` with a closed code. The text is never logged."""

        binding = await self.channels.binding(project_id)
        if binding is None:
            raise WelcomeFailedError(NO_CHANNEL)
        text = f"Welcome to the workspace of project {escaped(project_name, 150)}."
        with Contained() as posted:
            await self._client.chat_postMessage(channel=binding.channel_id, text=text)
        if posted.error is not None:
            response = getattr(posted.error, "response", None)
            reason = response.get("error") if response is not None else None
            log_event(logger, WARNING, "api.slack.welcome_failed", project_id=project_id, error_type=posted.error_type)
            raise WelcomeFailedError(NOT_IN_CHANNEL if reason in LEFT_CHANNEL else UNREACHABLE)
        log_event(logger, INFO, "api.slack.welcome_posted", project_id=project_id)
        return binding

    async def post_open(self, project_id: str) -> int:
        """Post the project's open questions that its channel has not seen; how many were posted."""

        binding = await self.channels.binding(project_id)
        if binding is None or self._runtime.gaps is None:
            return 0
        service = await self._runtime.gaps.service()
        page = await service.list_gaps(project_id, status=["open"], route=[ASK], limit=OPEN_MAX)
        posted = await self.channels.posted_gaps(project_id)
        new = [gap.gap_id for gap in page.gaps if gap.gap_id not in posted]
        number = max((post.number for post in posted.values()), default=0)
        for gap_id in reversed(new):  # the list is most important first; the channel shows the last message lowest
            detail = await self._detail(project_id, gap_id, uuid.UUID(binding.bound_by))
            number += 1
            text, blocks = gap_headline(number, detail)
            answer = await self._client.chat_postMessage(channel=binding.channel_id, text=text, blocks=blocks)
            ts = str(answer["ts"])
            await self.channels.add_post(Posted(project_id, gap_id, binding.channel_id, ts, number))
            text, blocks = gap_details(detail)
            await self._client.chat_postMessage(channel=binding.channel_id, thread_ts=ts, text=text, blocks=blocks)
        if new:
            log_event(logger, INFO, "api.slack.posted", project_id=project_id, gaps=len(new))
        return len(new)

    async def send(self, project_id: str, result: Any) -> None:
        """The gap runner's notice after a run: post the new questions; never raises."""

        del result
        with Contained() as posted:  # a failed post never fails the gap run
            await self.post_open(project_id)
        if posted.error is not None:
            error_type = type(posted.error).__name__
            log_event(logger, WARNING, "api.slack.post_failed", project_id=project_id, error_type=error_type)

    async def _detail(self, project_id: str, gap_id: str, caller: uuid.UUID) -> GapDetail:
        service = await self._runtime.gaps.service()
        gap = await service.get_gap(project_id, gap_id)
        holders = await holders_of(self._runtime, uuid.UUID(project_id), caller, detail_user_ids(gap))
        return gap_detail(gap, project_id, self._public_url, holders)

    # Replies ------------------------------------------------------------------------------------------------------

    async def on_message(self, event: dict[str, Any], body: dict[str, Any]) -> None:
        """A message in a channel: a human reply in the thread of a gap message answers that gap."""

        thread_ts = event.get("thread_ts")
        if event.get("subtype") or event.get("bot_id") or not thread_ts or thread_ts == event.get("ts"):
            return
        channel_id = str(event.get("channel", ""))
        posted = await self.channels.thread(channel_id, str(thread_ts))
        if posted is None:
            return
        slack = SlackUser(str(event.get("team") or body.get("team_id", "")), str(event.get("user", "")))
        await self._reply(slack, _Reply(channel_id, str(thread_ts), str(event.get("text") or "")), posted)

    async def replay(self, slack: SlackUser) -> None:
        """Record the replies ``slack`` sent before linking (called once the link is made)."""

        for reply in self._pending.pop(slack.key, []):
            posted = await self.channels.thread(reply.channel_id, reply.thread_ts)
            if posted is not None:
                await self._reply(slack, reply, posted)

    async def _reply(self, slack: SlackUser, reply: _Reply, posted: Posted) -> None:
        user_id = await self._links.user_of(slack)
        if user_id is None:
            waiting = self._pending.setdefault(slack.key, [])
            waiting[:] = [*waiting, reply][-PENDING_MAX:]
            text = f"To answer, connect your SpecFlow account once: {self._link_text(slack)} Your reply is kept."
            await self._private(slack, reply, text)
            return
        async with self._runtime.transaction() as session:
            member = await repo.get_member_project(session, user_id, uuid.UUID(posted.project_id))
        if member is None:
            await self._private(slack, reply, "You are not a member of this SpecFlow project, so this is not recorded.")
            return
        await self._answer(slack, reply, posted, user_id)

    async def _answer(self, slack: SlackUser, reply: _Reply, posted: Posted, user_id: uuid.UUID) -> None:
        project_id, gap_id = posted.project_id, posted.gap_id
        detail = await self._detail(project_id, gap_id, user_id)
        codes = {option.code: option.rank for option in detail.options}
        rank, own = reply_answer(reply.text, codes)
        if rank is None and own is None:
            return
        if own is not None and detail.kind == CONTRADICTION:
            await self._private(slack, reply, "A contradiction takes one letter here; an own answer goes on the web.")
            return
        answer = AnswerInput(
            gap_id=gap_id, option_rank=rank, own_text=own, answered_by=str(user_id), updated_at=None, via="slack"
        )
        try:
            applied = await record_and_apply(
                self._runtime.gaps, project_id, answer, user_id, assign_event=ASSIGN_FAILED
            )
        except GapStateError as error:
            status = str(error.fields.get("status", "changed"))
            await self._private(slack, reply, f"Not recorded: this question is {status} now.")
            return
        except GapNotFoundError:
            await self._private(slack, reply, "Not recorded: this question is no longer open.")
            return
        except AnswerInvalidError as error:
            log_event(logger, WARNING, "api.slack.answer_refused", project_id=project_id, error_code=error.code)
            await self._private(slack, reply, "Not recorded: answer this one on the web.")
            return
        chosen = next((o for o in detail.options if o.rank == rank), None)
        said = f"*{escaped(chosen.code, 2)}* ({escaped(chosen.title, 150)})" if chosen else "an own answer"
        state = "applying in one update build" if applied else "saved"
        await self._client.chat_postMessage(
            channel=reply.channel_id,
            thread_ts=reply.thread_ts,
            text=f"Recorded {said} from <@{slack.user_id}>: {state}.",
        )
        text, blocks = gap_headline(posted.number, detail, answered=True)
        await self._client.chat_update(channel=reply.channel_id, ts=reply.thread_ts, text=text, blocks=blocks)
        log_event(
            logger,
            INFO,
            "api.slack.answered",
            user_id=str(user_id),
            project_id=project_id,
            gap_id=gap_id,
            applied=applied,
        )

    async def _private(self, slack: SlackUser, reply: _Reply, text: str) -> None:
        await self._client.chat_postEphemeral(
            channel=reply.channel_id, user=slack.user_id, thread_ts=reply.thread_ts, text=text
        )
