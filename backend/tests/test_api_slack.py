"""Tier 1: the Slack app's pure parts (``api.slack``): the modal it renders from a ``GapDetail``, the answers it reads
back from a submit, the account links and their nonces, the answer path as the linked user, and its settings. No
Slack, no database, no graph store."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from agent_contracts import GapDetail
from api.plan.state import NoGraphBuildError
from api.slack.app import CONNECT_REFUSED, CONNECT_USAGE, NOT_OWNER, SlackGaps
from api.slack.blocks import APPLY_BLOCK, APPLY_VALUE, CHOICE, OWN, SlackAnswer, answer_view, read_answers
from api.slack.channels import Binding
from api.slack.links import NONCE_TTL, LinkRefusedError, SlackLinks, SlackUser
from api.slack.threads import NOT_IN_CHANNEL
from graph.gap import StaleGapError
from settings import ConfigurationError, SlackAppSettings, load_settings

PROJECT = str(uuid.UUID(int=7))
USER = uuid.UUID(int=8)
STAMP = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
SLACK = SlackUser("T1", "U1")
SLACK_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "PUBLIC_URL": "https://specflow.example.test",
}


def detail(gap_id: str = "G-1", *, kind: str = "missing", title: str = "Order limit") -> GapDetail:
    option = {
        "code": "A",
        "rank": 1,
        "title": "500 EUR everywhere",
        "label": "500 EUR",
        "detail": "One limit",
        "recommended": True,
        "placement": "amend",
        "edits": [{"placement": "amend", "where": "orders.md / Review", "operation": "replace", "text": "Above 500"}],
    }
    return GapDetail.model_validate(
        {
            "project_id": PROJECT,
            "gap_id": gap_id,
            "title": title,
            "severity": "blocker",
            "kind": kind,
            "origin": "detected",
            "status": "open",
            "blocking": True,
            "question": "Which limit <!here> & more?",
            "why": "Two values",
            "about": [],
            "impacts": [],
            "quotes": [{"source": "orders.md", "text": "Orders above 1,000 EUR"}],
            "options": [option, {**option, "code": "B", "rank": 2, "title": "1,000 EUR", "recommended": False}],
            "answer": None,
            "history": [],
            "follow_ups": [],
            "reach_stories": [],
            "reach_tasks": [],
            "assignee": None,
            "answerable": True,
            "updated_at": STAMP,
            "web_url": "https://specflow.example.test/projects/p/gaps?gap=G-1",
            "card": "",
        }
    )


def submitted(view: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    return {**view, "state": {"values": values}}


# The modal ------------------------------------------------------------------------------------------------------------


def test_the_modal_escapes_gap_text_and_offers_the_options_with_their_edits():
    view = answer_view(PROJECT, "Shop", [detail()], staged_by_others=2, ai_staged=4)
    text = json.dumps(view)

    assert "&lt;!here&gt; &amp; more" in text and "<!here>" not in text
    [radio] = [b for b in view["blocks"] if b.get("block_id") == "choice:G-1"]
    options = radio["element"]["options"]
    assert [o["value"] for o in options] == ["rank:1", "rank:2", OWN]
    assert options[0]["description"]["text"] == "Replaces in orders.md / Review: Above 500"
    assert all(len(o["text"]["text"]) <= 75 for o in options)
    assert "2 answers staged by others and 4 AI answers" in text
    assert json.loads(view["private_metadata"]) == {"p": PROJECT, "g": {"G-1": STAMP.isoformat()}}
    assert all(block.get("text", {}).get("verbatim", True) for block in view["blocks"] if block["type"] == "section")


def test_a_contradiction_offers_no_own_answer_and_links_the_web_card():
    view = answer_view(PROJECT, "Shop", [detail(kind="contradiction")], staged_by_others=0, ai_staged=0)

    [radio] = [b for b in view["blocks"] if b.get("block_id") == "choice:G-1"]
    assert OWN not in [o["value"] for o in radio["element"]["options"]]
    assert not [b for b in view["blocks"] if b.get("block_id") == "own:G-1"]
    assert "gaps?gap=G-1|on the web card" in json.dumps(view)


def test_the_modal_shows_at_most_five_gaps_within_slack_block_limits():
    view = answer_view(PROJECT, "Shop", [detail(f"G-{n}") for n in range(8)], staged_by_others=0, ai_staged=0)

    assert len(json.loads(view["private_metadata"])["g"]) == 5
    assert len(view["blocks"]) <= 100
    assert len(view["private_metadata"]) <= 3000
    inputs = [b["element"] for b in view["blocks"] if b.get("element", {}).get("type") == "plain_text_input"]
    assert inputs and all(element["max_length"] < 3000 for element in inputs)


# Reading a submit -----------------------------------------------------------------------------------------------------


def test_a_submit_gives_an_option_an_own_answer_a_skip_and_the_apply_box():
    gaps = [detail("G-1"), detail("G-2"), detail("G-3")]
    view = answer_view(PROJECT, "Shop", gaps, staged_by_others=0, ai_staged=0)
    values = {
        "choice:G-1": {CHOICE: {"selected_option": {"value": "rank:2"}}},
        "own:G-2": {OWN: {"value": "  Ask   the client  "}},
        APPLY_BLOCK: {APPLY_VALUE: {"selected_options": [{"value": APPLY_VALUE}]}},
    }

    read = read_answers(submitted(view, values))

    assert read.project_id == PROJECT
    assert read.answers == (
        SlackAnswer("G-1", 2, None, STAMP),
        SlackAnswer("G-2", None, "Ask the client", STAMP),
    )
    assert read.apply_now and read.errors == {}


def test_other_without_text_and_an_option_with_text_are_input_errors():
    view = answer_view(PROJECT, "Shop", [detail("G-1"), detail("G-2")], staged_by_others=0, ai_staged=0)
    values = {
        "choice:G-1": {CHOICE: {"selected_option": {"value": OWN}}},
        "choice:G-2": {CHOICE: {"selected_option": {"value": "rank:1"}}},
        "own:G-2": {OWN: {"value": "also this"}},
        APPLY_BLOCK: {APPLY_VALUE: {"selected_options": []}},
    }

    read = read_answers(submitted(view, values))

    assert set(read.errors) == {"own:G-1", "own:G-2"}
    assert read.answers == () and not read.apply_now


# Links ----------------------------------------------------------------------------------------------------------------


async def test_a_nonce_links_its_slack_user_once_and_the_link_survives_a_new_store(tmp_path):
    links = SlackLinks(tmp_path)
    nonce = links.nonce_for(SLACK)

    assert await links.link(nonce, USER) == SLACK
    with pytest.raises(LinkRefusedError):
        await links.link(nonce, USER)
    assert await SlackLinks(tmp_path).user_of(SLACK) == USER
    assert await links.user_of(SlackUser("T1", "U2")) is None


async def test_an_expired_or_unknown_nonce_is_refused(tmp_path):
    now = [STAMP]
    links = SlackLinks(tmp_path, clock=lambda: now[0])
    nonce = links.nonce_for(SLACK)
    now[0] = STAMP + NONCE_TTL + timedelta(seconds=1)

    for value in (nonce, "unknown"):
        with pytest.raises(LinkRefusedError):
            await links.link(value, USER)
    assert await links.user_of(SLACK) is None


# Recording as the linked user -----------------------------------------------------------------------------------------


class FakeService:
    def __init__(self, *, stale: set[str] | None = None, open_gaps: int = 0) -> None:
        self.answers: list[Any] = []
        self.applied: list[str] = []
        self._stale = stale or set()
        self._open = open_gaps
        self.assigned: list[tuple[str, str]] = []

    async def get_gap(self, _project_id: str, gap_id: str) -> Any:
        return SimpleNamespace(gap_id=gap_id, assigned_to=None)

    async def assign_gap(self, _project_id: str, gap_id: str, user_id: str, *, by: str) -> Any:
        self.assigned.append((gap_id, user_id))
        return SimpleNamespace(gap_id=gap_id, assigned_to=user_id, by=by)

    async def list_gaps(self, _project_id: str, **_: Any) -> Any:
        return SimpleNamespace(gaps=[object()] * self._open)

    async def answer_gap(self, project_id: str, answer: Any) -> Any:
        if answer.gap_id in self._stale:
            raise StaleGapError(gap_id=answer.gap_id)
        self.answers.append((project_id, answer))
        return SimpleNamespace(gap_id=answer.gap_id)

    async def apply_now(self, _project_id: str, *, by: str) -> Any:
        self.applied.append(by)
        return SimpleNamespace(gap_ids=("G-1", "G-9"))


class FakeRuntime:
    def __init__(self, service: FakeService, *, member: bool = True) -> None:
        self.gaps = SimpleNamespace(service=self._service)
        self._svc = service
        self._member = member

    async def _service(self) -> FakeService:
        return self._svc

    def transaction(self) -> Any:
        runtime = self

        class Session:
            async def __aenter__(self) -> Any:
                return runtime

            async def __aexit__(self, *_: Any) -> None:
                return None

        return Session()


@pytest.fixture
async def slack_app(monkeypatch):
    """Builds Slack apps on fake runtimes (membership as the runtime says) and closes them after the test."""

    made: list[SlackGaps] = []

    async def member(_session: Any, _user: Any, _project: Any) -> Any:
        return object() if made_for[0]._member else None

    made_for: list[FakeRuntime] = []

    def make(folder: Any, runtime: FakeRuntime) -> SlackGaps:
        made_for.insert(0, runtime)
        made.append(SlackGaps(SlackAppSettings(**SLACK_ENV), runtime, folder))
        return made[-1]

    monkeypatch.setattr("api.slack.app.repo.get_member_project", member)
    yield make
    for app in made:
        await app.aclose()


async def link(app: SlackGaps) -> None:
    await app.links.link(app.links.nonce_for(SLACK), USER)


async def test_answers_are_recorded_as_the_linked_user_via_slack_and_applied(tmp_path, slack_app):
    service = FakeService(stale={"G-2"})
    app = slack_app(tmp_path, FakeRuntime(service))
    await link(app)
    answers = [SlackAnswer("G-1", 1, None, STAMP), SlackAnswer("G-2", None, "text", STAMP)]

    lines = await app._record(SLACK, PROJECT, answers, apply_now=True)

    [(project_id, recorded)] = service.answers
    assert (project_id, recorded.answered_by, recorded.via, recorded.option_rank) == (PROJECT, str(USER), "slack", 1)
    assert recorded.updated_at == STAMP
    assert lines == [
        "G-1: saved",
        "G-2: changed since you opened it; open the form again",
        "Apply now: 2 answers go into one update build.",
    ]
    assert service.applied == [str(USER)]
    assert service.assigned == [("G-1", str(USER))]  # only the saved answer's gap


async def test_an_unlinked_slack_user_or_a_non_member_records_nothing(tmp_path, slack_app):
    service = FakeService()
    unlinked = slack_app(tmp_path / "a", FakeRuntime(service))
    assert (
        "not linked"
        in (await unlinked._record(SLACK, PROJECT, [SlackAnswer("G-1", 1, None, STAMP)], apply_now=True))[0]
    )
    outsider = slack_app(tmp_path / "b", FakeRuntime(service, member=False))
    await link(outsider)
    answers = [SlackAnswer("G-1", 1, None, STAMP)]

    assert (await outsider._record(SLACK, PROJECT, answers, apply_now=True)) == [
        "You are not a member of this project."
    ]
    assert service.answers == [] and service.applied == []


# Settings -------------------------------------------------------------------------------------------------------------

GRAPH_BUILD = {"GRAPH_LLM_MODEL": "a", "GRAPH_SMALL_MODEL": "b", "GRAPH_EMBEDDING_MODEL": "c"}


def test_the_api_reads_both_tokens_and_refuses_one_alone_or_a_wrong_kind(valid_env):
    env = {**valid_env, **GRAPH_BUILD}

    assert load_settings("api", env).slack_app is None
    slack = load_settings("api", {**env, "SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-2"}).slack_app
    assert slack is not None and slack.bot_token.get_secret_value() == "xoxb-1"
    with pytest.raises(ConfigurationError) as one:
        load_settings("api", {**env, "SLACK_BOT_TOKEN": "xoxb-1"})
    assert "SLACK_APP_TOKEN" in str(one.value)
    with pytest.raises(ConfigurationError) as swapped:
        load_settings("api", {**env, "SLACK_BOT_TOKEN": "xapp-2", "SLACK_APP_TOKEN": "xoxb-1"})
    assert "SLACK_BOT_TOKEN" in str(swapped.value) and "SLACK_APP_TOKEN" in str(swapped.value)


# /specflow plan -------------------------------------------------------------------------------------------------------


class FakePlans:
    def __init__(self, *, refuse: Exception | None = None) -> None:
        self.started: list[tuple[str, bool]] = []
        self._refuse = refuse

    def running(self, _project_id: str) -> bool:
        return False

    async def start(self, project_id: str, *, replan: bool) -> Any:
        if self._refuse is not None:
            raise self._refuse
        self.started.append((project_id, replan))
        return SimpleNamespace(status="running")


class Responses:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


async def acked() -> None:
    return None


@pytest.fixture
async def planning(tmp_path, slack_app, monkeypatch):
    async def projects(_session: Any, _user: Any, _limit: int) -> Any:
        return SimpleNamespace(
            items=[SimpleNamespace(id=uuid.UUID(PROJECT), name="Shop"), SimpleNamespace(id=uuid.uuid4(), name="Other")]
        )

    monkeypatch.setattr("api.slack.app.repo.list_projects", projects)
    runtime = FakeRuntime(FakeService(open_gaps=2))
    runtime.plans = FakePlans()
    runtime.graph_builds = None
    return SimpleNamespace(app=slack_app(tmp_path, runtime), runtime=runtime)


def command(text: str, channel: str = "C1") -> dict[str, Any]:
    return {"team_id": "T1", "user_id": "U1", "text": text, "channel_id": channel, "trigger_id": "t"}


async def test_plan_in_a_bound_channel_starts_the_plan_as_the_button_does_and_says_so_in_the_channel(planning):
    await link(planning.app)
    await planning.app.threads.channels.bind(Binding(PROJECT, "C1", "shop-gaps", str(USER)))
    respond = Responses()

    await planning.app._command(acked, command("plan"), respond, None)

    assert planning.runtime.plans.started == [(PROJECT, False)]
    [said] = respond.sent
    assert said["response_type"] == "in_channel"
    assert said["text"].startswith(
        "📋 <@U1> started creating the implementation plan of *Shop*. 2 questions are still open."
    )
    assert "<http" not in said["text"]  # no web link: Slack is the whole surface


async def test_replan_names_the_project_and_recreates_the_plan(planning):
    await link(planning.app)
    respond = Responses()

    await planning.app._command(acked, command("replan shop", channel="C9"), respond, None)

    assert planning.runtime.plans.started == [(PROJECT, True)]
    assert "started recreating" in respond.sent[0]["text"]


async def test_plan_is_refused_without_a_link_a_project_or_a_finished_build(planning):
    respond = Responses()
    await planning.app._command(acked, command("plan"), respond, None)
    assert "First connect your account" in respond.sent[-1]["text"]

    await link(planning.app)
    await planning.app._command(acked, command("plan", channel="C9"), respond, None)
    assert respond.sent[-1] == {"text": "Name the project: `/specflow plan <project>`.", "response_type": "ephemeral"}

    planning.runtime.plans = FakePlans(refuse=NoGraphBuildError(PROJECT))
    await planning.app._command(acked, command("plan shop"), respond, None)
    assert respond.sent[-1]["text"] == "The project has no built knowledge yet; build it in SpecFlow first."
    assert all(sent["response_type"] == "ephemeral" for sent in respond.sent)


# /specflow connect and disconnect -----------------------------------------------------------------------------------


class ChannelClient:
    """The Slack calls of a connect: C1 is a channel the app is in; any other is not found."""

    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.private: list[dict[str, Any]] = []

    async def conversations_info(self, *, channel: str) -> dict[str, Any]:
        if channel != "C1":
            raise RuntimeError("channel_not_found")
        return {"channel": {"id": "C1", "name": "shop-gaps", "is_member": True, "is_private": True}}

    async def _post(self, **kwargs: Any) -> dict[str, Any]:
        self.posted.append(kwargs)
        return {"ts": "1.1"}

    async def _ephemeral(self, **kwargs: Any) -> dict[str, Any]:
        self.private.append(kwargs)
        return {}

    def as_slack(self) -> SimpleNamespace:
        """The client under the Slack SDK's method names."""

        methods = {
            "conversations_info": self.conversations_info,
            "chat_postMessage": self._post,
            "chat_postEphemeral": self._ephemeral,
        }
        return SimpleNamespace(**methods)


@pytest.fixture
def connecting(planning, monkeypatch):
    """The planning app with a fake Slack client, a role for the caller (``rig.role``; ``None``: no member) and the
    posting of open questions recorded instead of done."""

    rig = SimpleNamespace(app=planning.app, client=ChannelClient(), role="owner", posted_for=[])

    async def membership(_session: Any, _user: Any, project_id: uuid.UUID) -> Any:
        return None if rig.role is None else (SimpleNamespace(id=project_id, name="Shop"), rig.role)

    async def post_open(project_id: str) -> int:
        rig.posted_for.append(project_id)
        return 0

    monkeypatch.setattr("api.slack.app.repo.get_membership", membership)
    slack = rig.client.as_slack()
    monkeypatch.setattr(planning.app.threads, "_client", slack)
    monkeypatch.setattr(planning.app.threads, "post_open", post_open)
    monkeypatch.setattr(type(planning.app._app), "client", property(lambda _app: slack))
    return rig


async def test_connect_by_the_owner_in_a_channel_binds_it_and_posts_the_questions(connecting):
    await link(connecting.app)
    respond = Responses()

    await connecting.app._command(acked, command(f"connect {PROJECT}"), respond, None)

    [said] = respond.sent
    assert said["response_type"] == "in_channel"
    assert said["text"].startswith("🔗 <@U1> connected this channel to the SpecFlow project *Shop*.")
    assert (await connecting.app.threads.channels.binding(PROJECT)) == Binding(PROJECT, "C1", "shop-gaps", str(USER))
    assert connecting.posted_for == [PROJECT]

    await connecting.app._command(acked, command(f"connect {PROJECT}"), respond, None)
    assert respond.sent[-1] == {"text": "This channel is already connected to *Shop*.", "response_type": "ephemeral"}


async def test_connect_needs_the_project_id_its_owner_and_a_channel_the_app_is_in(connecting):
    await link(connecting.app)
    respond = Responses()

    await connecting.app._command(acked, command("connect"), respond, None)
    assert respond.sent[-1]["text"] == CONNECT_USAGE
    for role, wanted in (("collaborator", PROJECT), (None, PROJECT), ("owner", "Shop")):
        connecting.role = role
        await connecting.app._command(acked, command(f"connect {wanted}"), respond, None)
        assert respond.sent[-1]["text"] == NOT_OWNER  # the same answer: an outsider learns nothing
    connecting.role = "owner"
    await connecting.app._command(acked, command(f"connect {PROJECT}", channel="C9"), respond, None)
    assert respond.sent[-1]["text"] == CONNECT_REFUSED[NOT_IN_CHANNEL]

    assert all(sent["response_type"] == "ephemeral" for sent in respond.sent)
    assert await connecting.app.threads.channels.binding(PROJECT) is None
    assert connecting.posted_for == []


async def test_an_unlinked_caller_signs_in_first_and_the_channel_connects_then(connecting):
    respond = Responses()

    await connecting.app._command(acked, command(f"connect {PROJECT}"), respond, None)

    [said] = respond.sent
    assert said["response_type"] == "ephemeral" and said["text"].startswith("Sign in to SpecFlow to connect")
    assert await connecting.app.threads.channels.binding(PROJECT) is None

    await link(connecting.app)
    await connecting.app.linked(SLACK)

    assert (await connecting.app.threads.channels.binding(PROJECT)).channel_id == "C1"
    assert "connected this channel to the SpecFlow project *Shop*" in connecting.client.posted[-1]["text"]
    assert connecting.client.posted[-1]["channel"] == "C1"
    assert connecting.posted_for == [PROJECT]

    await connecting.app.linked(SLACK)  # a later link connects nothing again
    assert connecting.posted_for == [PROJECT]


async def test_disconnect_in_the_channel_is_for_the_owner_of_its_project(connecting):
    await link(connecting.app)
    respond = Responses()
    await connecting.app._command(acked, command("disconnect"), respond, None)
    assert respond.sent[-1]["text"] == "This channel is not connected to a SpecFlow project."
    await connecting.app.threads.channels.bind(Binding(PROJECT, "C1", "shop-gaps", str(USER)))

    connecting.role = "collaborator"
    await connecting.app._command(acked, command("disconnect"), respond, None)
    assert respond.sent[-1]["text"] == "Only the owner of the connected SpecFlow project can disconnect it."
    assert await connecting.app.threads.channels.binding(PROJECT) is not None

    connecting.role = "owner"
    await connecting.app._command(acked, command("disconnect"), respond, None)
    assert respond.sent[-1]["response_type"] == "in_channel"
    assert "disconnected this channel from SpecFlow" in respond.sent[-1]["text"]
    assert await connecting.app.threads.channels.binding(PROJECT) is None
