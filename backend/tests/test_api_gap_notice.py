"""Tier 1: the gap notice to a Slack workflow (``api.gap.notice``, docs/slack/gap-answering.md "Before the app is
approved"): its payload, its POST on a mock transport, the runner's call after a run and its settings. No network."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from api.gap.notice import GapNotice, notice_payload, notice_text
from gap_host_support import PROJECT, StubGapRun, drained, make_gap_runner, store_build
from graph.gap import GapRunResult
from logger_harness import events, fields
from settings import ConfigurationError, SlackNoticeSettings, load_settings

HOOK = "https://hooks.slack.com/triggers/T000/111/secret-part"
OTHER_PROJECT = "00000000-0000-4000-8000-00000000beef"
COUNTS = {"routes": {"ask": 3, "ai": 4}, "gaps_new": {"total": 7, "by_severity": {"blocker": 1, "major": 2}}}


def result(counts: Any = COUNTS, *, status: str = "completed") -> GapRunResult:
    return GapRunResult(run_id="run-1", mode="update", build_id="build-1", status=status, counts=counts)


def settings(*, public_url: str | None = "https://specflow.example.test", hook: str = HOOK) -> SlackNoticeSettings:
    env = {"SLACK_WORKFLOW_WEBHOOK_URL": hook, "SLACK_WORKFLOW_PROJECTS": PROJECT}
    if public_url is not None:
        env["PUBLIC_URL"] = public_url
    return SlackNoticeSettings(**env)


def notice(
    handler: Any,
    *,
    name: str | None = "Shop",
    public_url: str | None = "https://specflow.example.test",
    hook: str = HOOK,
) -> GapNotice:
    async def project_name(_project_id: str) -> str | None:
        return name

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GapNotice(settings(public_url=public_url, hook=hook), project_name, client=client)


class Hook:
    """Records each request and answers ``status``."""

    def __init__(self, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status)


# The payload ---------------------------------------------------------------------------------------------------------


def test_the_payload_holds_the_project_counts_and_link_as_text():
    payload = notice_payload(result(), project="Shop", url="https://x.test/projects/p")

    assert payload == {
        "project": "Shop",
        "new_questions": "3",
        "blockers": "1",
        "majors": "2",
        "url": "https://x.test/projects/p",
    }


@pytest.mark.parametrize(
    "counts",
    [{"routes": {"ask": 0, "ai": 5}}, {}, {"routes": "broken"}, {"routes": {"ask": True}}],
    ids=["ai-only", "no-counts", "not-a-map", "bool"],
)
def test_a_run_that_asks_nobody_anything_has_no_payload(counts):
    assert notice_payload(result(counts), project="Shop", url="") is None


# The POST ------------------------------------------------------------------------------------------------------------


async def test_a_listed_project_posts_its_notice_to_the_hook(caplog):
    hook = Hook()

    with caplog.at_level("INFO"):
        await notice(hook).send(PROJECT, result())

    [request] = hook.requests
    assert (request.method, str(request.url)) == ("POST", HOOK)
    assert json.loads(request.content) == {
        "project": "Shop",
        "new_questions": "3",
        "blockers": "1",
        "majors": "2",
        "url": f"https://specflow.example.test/projects/{PROJECT}",
    }
    [sent] = events(caplog, "api.gap.notice_sent")
    assert (fields(sent)["project_id"], fields(sent)["new_questions"]) == (PROJECT, 3)


def test_the_incoming_webhook_text_escapes_the_project_and_links_the_canvas():
    payload = notice_payload(result(), project="R&D <shop>", url="https://x.test/projects/p")

    assert payload is not None
    assert notice_text(payload) == (
        "R&amp;D &lt;shop&gt;: 3 new questions (1 blockers, 2 major). <https://x.test/projects/p|Answer in SpecFlow>"
    )
    assert notice_text({**payload, "url": ""}).endswith("(1 blockers, 2 major).")


async def test_an_incoming_webhook_gets_the_text_instead_of_the_variables():
    hook = Hook()
    incoming = "https://hooks.slack.com/services/T000/B000/secret-part"

    await notice(hook, hook=incoming).send(PROJECT, result())

    [request] = hook.requests
    assert str(request.url) == incoming
    assert json.loads(request.content) == {
        "text": f"Shop: 3 new questions (1 blockers, 2 major). "
        f"<https://specflow.example.test/projects/{PROJECT}|Answer in SpecFlow>"
    }


async def test_without_a_name_or_public_url_the_notice_has_the_project_id_and_no_link():
    hook = Hook()

    await notice(hook, name=None, public_url=None).send(PROJECT, result())

    body = json.loads(hook.requests[0].content)
    assert (body["project"], body["url"]) == (PROJECT, "")


@pytest.mark.parametrize(
    ("project_id", "run"),
    [(OTHER_PROJECT, result()), (PROJECT, result(status="failed")), (PROJECT, result({"routes": {"ask": 0}}))],
    ids=["unlisted-project", "failed-run", "no-new-questions"],
)
async def test_nothing_is_posted_for_an_unlisted_project_a_failed_run_or_no_question(project_id, run):
    hook = Hook()

    await notice(hook).send(project_id, run)

    assert hook.requests == []


async def test_a_refused_post_is_logged_without_the_hook_and_never_raised(caplog):
    with caplog.at_level("INFO"):
        await notice(Hook(status=500)).send(PROJECT, result())

    [failed] = events(caplog, "api.gap.notice_failed")
    assert (fields(failed)["status_code"], fields(failed)["error_type"]) == (500, None)
    assert "secret-part" not in caplog.text


async def test_an_unreachable_hook_is_logged_and_never_raised(caplog):
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with caplog.at_level("INFO"):
        await notice(unreachable).send(PROJECT, result())

    [failed] = events(caplog, "api.gap.notice_failed")
    assert (fields(failed)["error_type"], fields(failed)["status_code"]) == ("ConnectError", None)
    assert "secret-part" not in caplog.text


# The runner ----------------------------------------------------------------------------------------------------------


class RecordingNotice:
    def __init__(self) -> None:
        self.sent: list[tuple[str, GapRunResult]] = []
        self.closed = False

    async def send(self, project_id: str, run: GapRunResult) -> None:
        self.sent.append((project_id, run))

    async def aclose(self) -> None:
        self.closed = True


async def test_the_runner_sends_the_notice_after_each_finished_run_and_closes_it(tmp_path):
    await store_build(tmp_path, "build-1", minutes=0)
    run = StubGapRun()
    run.counts = COUNTS
    recording = RecordingNotice()
    runner = make_gap_runner(tmp_path, run, notice=recording)

    await runner.after_build(PROJECT, "build-1")
    await drained(runner)
    await runner.aclose()

    [(project_id, sent)] = recording.sent
    assert (project_id, sent.build_id, sent.counts) == (PROJECT, "build-1", COUNTS)
    assert recording.closed


# Settings ------------------------------------------------------------------------------------------------------------

GRAPH_BUILD = {"GRAPH_LLM_MODEL": "a", "GRAPH_SMALL_MODEL": "b", "GRAPH_EMBEDDING_MODEL": "c"}


def test_the_api_has_no_notice_while_the_hook_is_unset(valid_env):
    assert load_settings("api", {**valid_env, **GRAPH_BUILD}).slack_notice is None


def test_the_api_reads_the_hook_the_projects_and_the_public_url(valid_env):
    env = {
        **valid_env,
        **GRAPH_BUILD,
        "SLACK_WORKFLOW_WEBHOOK_URL": f" {HOOK} ",
        "SLACK_WORKFLOW_PROJECTS": f"{PROJECT.upper()}, {PROJECT}",
        "PUBLIC_URL": "https://specflow.example.test/",
    }

    slack = load_settings("api", env).slack_notice

    assert slack is not None
    assert (slack.webhook_url.get_secret_value(), slack.projects) == (HOOK, (PROJECT,))
    assert slack.public_url == "https://specflow.example.test"
    assert HOOK not in repr(slack)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("SLACK_WORKFLOW_WEBHOOK_URL", "https://example.test/hook"),
        ("SLACK_WORKFLOW_WEBHOOK_URL", "http://hooks.slack.com/triggers/x"),
        ("SLACK_WORKFLOW_PROJECTS", ""),
        ("SLACK_WORKFLOW_PROJECTS", "shop"),
    ],
    ids=["other-host", "plain-http", "no-project", "not-an-id"],
)
def test_a_hook_elsewhere_or_a_bad_project_list_is_refused(valid_env, variable, value):
    env = {**valid_env, **GRAPH_BUILD, "SLACK_WORKFLOW_WEBHOOK_URL": HOOK, "SLACK_WORKFLOW_PROJECTS": PROJECT}
    env[variable] = value

    with pytest.raises(ConfigurationError) as refused:
        load_settings("api", env)

    assert variable in str(refused.value)
