"""The Slack test connection: ``SlackThreads.post_welcome`` posts one welcome message in the project's bound channel
with closed failure codes, and ``POST /projects/{pid}/slack/test`` (any member, D6) answers it over the REST app.

Standings as in test_api_jira_routes: alice owns ``Alpha``, ben is its collaborator, carol is no member.

Sequence::

    web -> project_member -> SlackThreads.post_welcome -> binding read -> chat_postMessage(channel, text) -> 200
    no binding / app not in channel / Slack down -> 409 / 409 / 502; the text is never logged
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from api.slack.channels import Binding, SlackChannels
from api.slack.links import SlackLinks
from api.slack.threads import NO_CHANNEL, NOT_IN_CHANNEL, UNREACHABLE, SlackThreads, WelcomeFailedError
from specflow_api_agent_support import ALPHA, agent_env, serve_agent
from test_api_atlassian_routes import call, error_of

ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"
PROJECT = str(uuid.UUID(int=7))
WELCOME = "Welcome to the workspace of project Alpha."


class SlackError(Exception):
    """What the Slack SDK raises: an exception that carries the response with Slack's error word."""

    def __init__(self, error: str | None) -> None:
        super().__init__("slack said no")
        self.response = None if error is None else {"error": error}


class FakeSlackClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.fail: Exception | None = None

    def as_slack(self) -> SimpleNamespace:
        """The client under the Slack SDK's method name."""

        return SimpleNamespace(chat_postMessage=self.post)

    async def post(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail is not None:
            raise self.fail
        self.posted.append(kwargs)
        return {"ts": "1.1"}


def make_threads(tmp_path: Any, client: FakeSlackClient) -> SlackThreads:
    return SlackThreads(
        client.as_slack(),
        SimpleNamespace(),
        SlackLinks(tmp_path),
        SlackChannels(tmp_path),
        public_url=None,
        link_text=lambda _slack: "",
    )


async def bind(threads: SlackThreads, project: str = PROJECT) -> None:
    await threads.channels.bind(Binding(project, "C1", "shop-gaps", str(uuid.UUID(int=8)), private=True))


async def test_the_welcome_is_one_message_with_the_exact_text_in_the_bound_channel(tmp_path) -> None:
    client = FakeSlackClient()
    threads = make_threads(tmp_path, client)
    await bind(threads)

    bound = await threads.post_welcome(PROJECT, "Alpha")

    assert client.posted == [{"channel": "C1", "text": WELCOME}]
    assert (bound.channel_id, bound.channel_name) == ("C1", "shop-gaps")


async def test_the_project_name_is_escaped_for_slack(tmp_path) -> None:
    client = FakeSlackClient()
    threads = make_threads(tmp_path, client)
    await bind(threads)

    await threads.post_welcome(PROJECT, "R&D <!here>")

    assert client.posted[0]["text"] == "Welcome to the workspace of project R&amp;D &lt;!here&gt;."


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ("not_in_channel", NOT_IN_CHANNEL),
        ("channel_not_found", NOT_IN_CHANNEL),
        ("is_archived", NOT_IN_CHANNEL),
        ("ratelimited", UNREACHABLE),
        (None, UNREACHABLE),
    ],
)
async def test_a_refused_post_is_a_closed_code_and_no_text_is_logged(tmp_path, caplog, error, code) -> None:
    client = FakeSlackClient()
    client.fail = SlackError(error)
    threads = make_threads(tmp_path, client)
    await bind(threads)

    with caplog.at_level(logging.INFO), pytest.raises(WelcomeFailedError) as caught:
        await threads.post_welcome(PROJECT, "Alpha")

    assert caught.value.code == code
    assert "slack said no" not in caplog.text and WELCOME not in caplog.text and "Alpha" not in caplog.text


async def test_a_project_without_a_channel_posts_nothing(tmp_path) -> None:
    client = FakeSlackClient()

    with pytest.raises(WelcomeFailedError) as caught:
        await make_threads(tmp_path, client).post_welcome(PROJECT, "Alpha")

    assert caught.value.code == NO_CHANNEL and client.posted == []


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env, tmp_path):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        client = FakeSlackClient()
        threads = make_threads(tmp_path, client)
        served.runtime.slack = SimpleNamespace(threads=threads)
        repo = served.repo
        ben = await repo.upsert_user(None, SimpleNamespace(subject=f"user:{BEN}", email=BEN, name=None))
        alpha = uuid.UUID(ALPHA)
        repo.members.add((alpha, ben.id))
        repo.roles[(alpha, ben.id)] = "collaborator"
        await repo.upsert_user(None, SimpleNamespace(subject=f"user:{CAROL}", email=CAROL, name=None))
        yield SimpleNamespace(http=served.client, runtime=served.runtime, client=client, threads=threads)


async def test_any_member_posts_the_welcome_and_gets_the_channel(rig) -> None:
    await bind(rig.threads, ALPHA)

    answer = await call(rig, "POST", f"projects/{ALPHA}/slack/test", BEN)  # a collaborator: D6

    assert answer.status_code == 200
    assert answer.json() == {"posted": True, "channel": {"id": "C1", "name": "shop-gaps", "private": True}}
    assert rig.client.posted == [{"channel": "C1", "text": WELCOME}]


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("none", (409, "slack_not_connected")),
        ("left", (409, "slack_not_in_channel")),
        ("down", (502, "slack_unreachable")),
    ],
)
async def test_the_route_answers_each_closed_code(rig, setup, expected) -> None:
    if setup != "none":
        await bind(rig.threads, ALPHA)
        rig.client.fail = SlackError("not_in_channel" if setup == "left" else "fatal_error")

    assert error_of(await call(rig, "POST", f"projects/{ALPHA}/slack/test")) == expected
    assert rig.client.posted == []


async def test_a_non_member_is_404_and_a_server_without_slack_is_503(rig) -> None:
    await bind(rig.threads, ALPHA)

    assert error_of(await call(rig, "POST", f"projects/{ALPHA}/slack/test", CAROL)) == (404, "project_not_found")
    assert rig.client.posted == []
    rig.runtime.slack = None
    assert error_of(await call(rig, "POST", f"projects/{ALPHA}/slack/test")) == (503, "slack_unavailable")
