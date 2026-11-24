"""Tier 1: the Slack channel flow (``api.slack.threads``, docs/slack/gap-answering.md "The channel flow"): only an
internal channel the app is in, and no other project's, can be connected; connecting posts the open questions once
each, a reply in a question's thread is recorded as the replier's SpecFlow user and applied, an unlinked replier gets
the link privately and is recorded once linked, the settings show only the project's own channel, and the store and
the reply parser. A fake Slack client and a fake gap service; no Slack, no database, no graph store."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from api.routers.slack import _settings
from api.slack.blocks import gap_details, gap_headline, reply_answer
from api.slack.channels import Binding, ChannelTakenError, Posted, SlackChannels
from api.slack.links import SlackLinks, SlackUser
from api.slack.threads import NOT_A_CHANNEL, NOT_IN_CHANNEL, SHARED, TAKEN, ChannelRefusedError, SlackThreads
from test_api_slack import detail

PROJECT = str(uuid.UUID(int=7))
OWNER = uuid.UUID(int=8)
ANA = uuid.UUID(int=9)
SLACK = SlackUser("T1", "U1")
STAMP = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


class FakeClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.private: list[dict[str, Any]] = []
        self._ts = 0

    async def conversations_info(self, *, channel: str) -> dict[str, Any]:
        channels = {
            "C1": {"name": "shop-gaps", "is_member": True, "is_private": True},
            "C2": {"name": "general", "is_member": False},
            "C3": {"name": "client-shared", "is_member": True, "is_ext_shared": True},
            "C4": {"name": "billing", "is_member": True},
        }
        if channel not in channels:
            raise RuntimeError("channel_not_found")  # Slack's answer for a private channel the app is not in
        return {"channel": {"id": channel, **channels[channel]}}

    async def _post(self, **kwargs: Any) -> dict[str, Any]:
        self._ts += 1
        self.posted.append(kwargs)
        return {"ts": f"100.{self._ts}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updated.append(kwargs)
        return {}

    async def _ephemeral(self, **kwargs: Any) -> dict[str, Any]:
        self.private.append(kwargs)
        return {}

    def as_slack(self) -> SimpleNamespace:
        """The client as SpecFlow calls it, under the Slack SDK's method names."""

        methods = {
            "conversations_info": self.conversations_info,
            "chat_postMessage": self._post,
            "chat_update": self.chat_update,
            "chat_postEphemeral": self._ephemeral,
        }
        return SimpleNamespace(**methods)


class FakeService:
    def __init__(self, open_ids: list[str]) -> None:
        self.open_ids = open_ids
        self.answers: list[Any] = []
        self.applied: list[str] = []
        self.assignees: dict[str, str] = {}
        self.assigned: list[tuple[str, str, str]] = []

    async def list_gaps(self, _project_id: str, **_: Any) -> Any:
        return SimpleNamespace(gaps=[SimpleNamespace(gap_id=gap_id) for gap_id in self.open_ids])

    async def get_gap(self, _project_id: str, gap_id: str) -> Any:
        return SimpleNamespace(gap_id=gap_id, assigned_to=self.assignees.get(gap_id))

    async def assign_gap(self, _project_id: str, gap_id: str, user_id: str, *, by: str) -> Any:
        self.assigned.append((gap_id, user_id, by))
        self.assignees[gap_id] = user_id
        return await self.get_gap(_project_id, gap_id)

    async def answer_gap(self, _project_id: str, answer: Any) -> Any:
        self.answers.append(answer)
        return SimpleNamespace(gap_id=answer.gap_id)

    async def apply_now(self, _project_id: str, *, by: str) -> Any:
        self.applied.append(by)
        return SimpleNamespace(gap_ids=("G-1",))


class FakeRuntime:
    def __init__(self, service: FakeService, members: set[uuid.UUID]) -> None:
        self.gaps = SimpleNamespace(service=self._service)
        self.svc = service
        self.members = members

    async def _service(self) -> FakeService:
        return self.svc

    def transaction(self) -> Any:
        class Session:
            async def __aenter__(self) -> Any:
                return None

            async def __aexit__(self, *_: Any) -> None:
                return None

        return Session()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    service = FakeService(["G-1", "G-2"])
    runtime = FakeRuntime(service, {OWNER, ANA})
    kinds = {"G-1": "missing", "G-2": "contradiction"}

    async def holders(*_: Any) -> Any:
        return None

    async def member(_session: Any, user_id: uuid.UUID, _project: Any) -> Any:
        return object() if user_id in runtime.members else None

    monkeypatch.setattr("api.slack.threads.holders_of", holders)
    monkeypatch.setattr("api.slack.threads.detail_user_ids", lambda _gap: [])
    monkeypatch.setattr(
        "api.slack.threads.gap_detail", lambda gap, *_: detail(gap.gap_id, kind=kinds.get(gap.gap_id, "missing"))
    )
    monkeypatch.setattr("api.slack.threads.repo.get_member_project", member)
    client = FakeClient()
    links = SlackLinks(tmp_path)
    threads = SlackThreads(
        client.as_slack(),
        runtime,
        links,
        SlackChannels(tmp_path),
        public_url="https://specflow.example.test",
        link_text=lambda slack: f"<link for {slack.user_id}>",
    )
    return SimpleNamespace(threads=threads, client=client, service=service, links=links, runtime=runtime)


def reply(text: str, thread_ts: str, *, user: str = "U1", **over: Any) -> dict[str, Any]:
    return {
        "type": "message",
        "channel": "C1",
        "user": user,
        "team": "T1",
        "text": text,
        "ts": "200.1",
        "thread_ts": thread_ts,
        **over,
    }


async def bound(rig: Any) -> dict[str, str]:
    await rig.threads.connect(PROJECT, "C1", by=str(OWNER))
    await rig.threads.post_open(PROJECT)
    return {post.gap_id: post.ts for post in (await rig.threads.channels.posted_gaps(PROJECT)).values()}


# Binding and posting --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channel", "code"),
    [("D1", NOT_A_CHANNEL), ("C2", NOT_IN_CHANNEL), ("C9", NOT_IN_CHANNEL), ("C3", SHARED)],
)
async def test_only_an_internal_channel_the_app_is_in_can_be_connected(rig, channel, code):
    with pytest.raises(ChannelRefusedError) as refused:
        await rig.threads.connect(PROJECT, channel, by=str(OWNER))

    assert refused.value.code == code
    assert await rig.threads.channels.binding(PROJECT) is None


async def test_a_channel_belongs_to_one_project_and_the_same_one_again_changes_nothing(rig):
    other = str(uuid.UUID(int=99))
    assert await rig.threads.connect(PROJECT, "C1", by=str(OWNER)) is None
    first = await rig.threads.channels.binding(PROJECT)

    with pytest.raises(ChannelRefusedError) as refused:
        await rig.threads.connect(other, "C1", by=str(ANA))
    assert refused.value.code == TAKEN
    assert await rig.threads.connect(PROJECT, "C1", by=str(ANA)) == first  # nothing changed, not even who
    assert await rig.threads.channels.binding(PROJECT) == first
    assert await rig.threads.connect(other, "C4", by=str(ANA)) is None


async def test_the_settings_show_only_the_projects_own_channel(rig):
    empty = await _settings(rig.threads, PROJECT, editable=False)
    assert (empty.available, empty.channel, empty.editable) == (True, None, False)
    await rig.threads.connect(PROJECT, "C1", by=str(OWNER))
    await rig.threads.connect(str(uuid.UUID(int=99)), "C4", by=str(ANA))

    shown = await _settings(rig.threads, PROJECT, editable=True)

    assert shown.model_dump() == {
        "available": True,
        "channel": {"id": "C1", "name": "shop-gaps", "private": True},
        "editable": True,
    }
    assert (await _settings(None, PROJECT, editable=True)).available is False


def lines(client: FakeClient) -> list[str]:
    """The channel's own lines (not the thread replies), as posted."""

    return [m["blocks"][0]["text"]["text"] for m in client.posted if "thread_ts" not in m]


async def test_binding_posts_each_open_question_once_as_a_numbered_line_with_its_details_in_the_thread(rig):
    posts = await bound(rig)

    assert set(posts) == {"G-1", "G-2"}
    assert [m["channel"] for m in rig.client.posted] == ["C1"] * 4
    headline, details = rig.client.posted[:2]
    assert "thread_ts" not in headline and details["thread_ts"] == posts["G-2"]  # the last of the list is posted first
    assert lines(rig.client) == ["🔴 *#1 · BLOCKER · Order limit*", "🔴 *#2 · BLOCKER · Order limit*"]
    await rig.threads.send(PROJECT, result=None)
    assert len(rig.client.posted) == 4
    rig.service.open_ids.append("G-3")
    await rig.threads.send(PROJECT, result=None)
    assert lines(rig.client)[-1] == "🔴 *#3 · BLOCKER · Order limit*"


async def test_an_unbound_project_posts_nothing(rig):
    assert await rig.threads.post_open(PROJECT) == 0
    assert rig.client.posted == []


# Replies -------------------------------------------------------------------------------------------------------------


async def test_a_letter_in_the_thread_is_that_option_recorded_as_the_replier_and_applied(rig):
    posts = await bound(rig)
    await rig.links.link(rig.links.nonce_for(SLACK), ANA)

    await rig.threads.on_message(reply("b.", posts["G-1"]), {})

    [answer] = rig.service.answers
    assert (answer.gap_id, answer.option_rank, answer.own_text) == ("G-1", 2, None)
    assert (answer.answered_by, answer.via, answer.updated_at) == (str(ANA), "slack", None)
    assert rig.service.applied == [str(ANA)]
    assert rig.service.assigned == [("G-1", str(ANA), str(ANA))]  # nobody had it: now the answerer's
    said = rig.client.posted[-1]
    assert said["thread_ts"] == posts["G-1"] and "<@U1>" in said["text"]
    [update] = rig.client.updated
    assert update["ts"] == posts["G-1"]
    struck = update["blocks"][0]["text"]["text"]
    assert struck == "✅ ~#2 · BLOCKER · Order limit~"


async def test_other_words_are_an_own_answer_but_not_for_a_contradiction(rig):
    posts = await bound(rig)
    await rig.links.link(rig.links.nonce_for(SLACK), ANA)

    await rig.threads.on_message(reply("Ask the client first", posts["G-1"]), {})
    await rig.threads.on_message(reply("Neither, use 750", posts["G-2"]), {})

    [answer] = rig.service.answers
    assert (answer.gap_id, answer.own_text) == ("G-1", "Ask the client first")
    assert "contradiction" in rig.client.private[-1]["text"]


async def test_an_unlinked_replier_gets_the_link_privately_and_is_recorded_once_linked(rig):
    posts = await bound(rig)

    await rig.threads.on_message(reply("A", posts["G-1"]), {})
    assert rig.service.answers == []
    [private] = rig.client.private
    assert (private["user"], private["thread_ts"]) == ("U1", posts["G-1"]) and "<link for U1>" in private["text"]

    await rig.links.link(rig.links.nonce_for(SLACK), ANA)
    await rig.threads.replay(SLACK)
    [answer] = rig.service.answers
    assert (answer.gap_id, answer.option_rank, answer.answered_by) == ("G-1", 1, str(ANA))


async def test_a_non_member_bot_messages_and_other_threads_record_nothing(rig):
    posts = await bound(rig)
    rig.runtime.members.discard(ANA)
    await rig.links.link(rig.links.nonce_for(SLACK), ANA)

    await rig.threads.on_message(reply("A", posts["G-1"]), {})
    await rig.threads.on_message(reply("A", posts["G-1"], bot_id="B1"), {})
    await rig.threads.on_message(reply("A", "999.9"), {})
    await rig.threads.on_message(reply("A", posts["G-1"], ts=posts["G-1"]), {})

    assert rig.service.answers == []
    assert "not a member" in rig.client.private[-1]["text"]


# The store and the parts ---------------------------------------------------------------------------------------------


async def test_a_new_binding_forgets_the_old_channel_posts(tmp_path):
    store = SlackChannels(tmp_path)
    await store.bind(Binding(PROJECT, "C1", "a", str(OWNER)))
    await store.add_post(Posted(PROJECT, "G-1", "C1", "1.1"))
    assert (await store.thread("C1", "1.1")) == Posted(PROJECT, "G-1", "C1", "1.1")

    await store.bind(Binding(PROJECT, "C2", "b", str(OWNER)))

    assert await store.thread("C1", "1.1") is None
    assert (await SlackChannels(tmp_path).binding(PROJECT)) == Binding(PROJECT, "C2", "b", str(OWNER))
    with pytest.raises(ChannelTakenError):
        await store.bind(Binding(str(uuid.UUID(int=99)), "C2", "b", str(ANA)))
    await store.unbind(PROJECT)
    assert await store.binding(PROJECT) is None
    await store.bind(Binding(str(uuid.UUID(int=99)), "C2", "b", str(ANA), private=False))  # free again
    assert (await store.binding(str(uuid.UUID(int=99)))).private is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("B", (2, None)),
        (" a ) ", (1, None)),
        ("E", (None, "E")),
        ("B, but 600", (None, "B, but 600")),
        ("  ", (None, None)),
    ],
)
def test_a_reply_is_one_offered_letter_or_own_words(text, expected):
    assert reply_answer(text, {"A": 1, "B": 2}) == expected


def test_the_thread_details_escape_the_gap_text_and_say_how_to_answer():
    text, blocks = gap_details(detail())
    dumped = json.dumps(blocks)

    assert "<!here>" not in dumped and "&lt;!here&gt;" in dumped
    assert "Reply here with one letter (A, B), or write your own answer." in dumped
    assert "open in SpecFlow" not in dumped and "<http" not in dumped
    assert "<!" not in text


def test_a_headline_is_one_line_and_a_tilde_cannot_end_its_strike_through():
    _, blocks = gap_headline(7, detail(title="Rate ~5% <b>"))
    _, struck = gap_headline(7, detail(title="Rate ~5% <b>"), answered=True)

    assert blocks[0]["text"]["text"] == "🔴 *#7 · BLOCKER · Rate -5% &lt;b&gt;*"
    assert struck[0]["text"]["text"] == "✅ ~#7 · BLOCKER · Rate -5% &lt;b&gt;~"


async def test_an_assigned_gap_keeps_its_assignee_when_someone_else_answers_in_slack(rig):
    posts = await bound(rig)
    rig.service.assignees["G-1"] = str(OWNER)
    await rig.links.link(rig.links.nonce_for(SLACK), ANA)

    await rig.threads.on_message(reply("A", posts["G-1"]), {})

    assert [a.gap_id for a in rig.service.answers] == ["G-1"]
    assert rig.service.assigned == []
