"""The agent write tools (``add_finding``, ``report_task``, ``complete_story``) through the MCP router over the REAL API
app, in process (the pattern of ``test_mcp_people_tools_over_api.py``): the story plan and findings of
``people_support`` behind the API, a recording transport between the two services, the MCP SDK client in front.

Covers the ``POST`` with the project in the query and the other arguments as the JSON body, the JSON text equal to the
structured content, the typed error details end to end, a story told from claim to completion, the audit (ids and
codes only: never finding text, a note, a label, a claim token, a commit or a URL) and the input schemas the SDK
enforces before any call.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from agent_contracts import AGENT_ROUTES, FindingResult, StoryCompletion, TaskReport
from mcp_server.errors import MESSAGES
from mcp_support import ALICE, call, mcp_env, sdk_session, structured, text
from people_support import API, serve_people
from specflow_api_agent_support import ALPHA, agent_env
from test_mcp_people_tools_over_api import Stack
from test_mcp_spec_tools_over_api import Recorder

CONTRACT = "TASK::A::API_CONTRACT"
COMMIT = "d" * 40
LABEL = "SecretLabel"
SECRETS = (
    "SecretText",
    "SecretNote",
    "secret/ref.py",
    "https://secret.example.test/pr/1",
    COMMIT,
    LABEL,
    "secret/claim-branch",
    "SecretFailedNote",
    "secret/failed-branch",
    "SecretFailedReport",
    "alice@example.com",
    "ben@example.com",
)
NOT_HELD = "nope"  # a claim token nobody holds


@asynccontextmanager
async def over_people(monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str]) -> AsyncIterator[Stack]:
    async with serve_people(monkeypatch, agent_env(valid_env)) as people:
        recorder = Recorder(httpx.ASGITransport(app=people.rig.app, raise_app_exceptions=False))
        yield Stack(people, recorder, mcp_env(valid_env))


async def test_a_story_from_claim_to_completion_posts_each_write_and_answers_what_the_api_answers(
    monkeypatch, valid_env
):
    async with over_people(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        token = structured(await call(session, "claim_story", project_id=ALPHA, story_id=API))["claim_token"]
        finding = await call(
            session,
            "add_finding",
            project_id=ALPHA,
            kind="gap",
            text="How long is a cart kept?",
            about=["FEAT::API"],
            refers_to=[{"type": "commit", "value": "e44f0c1"}],
            story_id=API,
        )
        reported = await call(
            session, "report_task", project_id=ALPHA, task_id=CONTRACT, claim_token=token, progress="done", note="ok"
        )
        completed = await call(
            session, "complete_story", project_id=ALPHA, story_id=API, claim_token=token, merge_commit=COMMIT
        )
        _, (add, add_answer), (report, report_answer), (complete, complete_answer) = stack.api.agent_calls()
        tasks = stack.people.memory.tasks

    for sent, route in ((add, "add_finding"), (report, "report_task"), (complete, "complete_story")):
        assert (sent.method, sent.url.path, dict(sent.url.params)) == (
            "POST",
            AGENT_ROUTES[route],
            {"project_id": ALPHA},
        )
    assert json.loads(add.content) == {
        "kind": "gap",
        "text": "How long is a cart kept?",
        "about": ["FEAT::API"],
        "refers_to": [{"type": "commit", "value": "e44f0c1"}],
        "story_id": API,
        "blocking": False,
    }  # project_id is not in the body, None is left out
    assert json.loads(report.content) == {"task_id": CONTRACT, "claim_token": token, "progress": "done", "note": "ok"}
    assert json.loads(complete.content) == {"story_id": API, "claim_token": token, "merge_commit": COMMIT}
    for result, answer, model in (
        (finding, add_answer, FindingResult),
        (reported, report_answer, TaskReport),
        (completed, complete_answer, StoryCompletion),
    ):
        assert not result.is_error and answer.status_code == 200
        assert structured(result) == answer.json() and model.model_validate(structured(result))
        assert json.loads(text(result)) == structured(result) and len(result.content) == 1  # a write keeps the JSON
    assert structured(finding)["created"] is True and structured(reported)["task"]["progress"] == "done"
    assert structured(completed)["completed"] == [CONTRACT, "TASK::A::API_LOGIC"]
    assert tasks[CONTRACT]["status"] == "completed"


async def test_errors_carry_their_typed_details_and_the_fixed_messages(monkeypatch, valid_env):
    async with over_people(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        token = structured(await call(session, "claim_story", project_id=ALPHA, story_id=API))["claim_token"]
        node = await call(
            session, "add_finding", project_id=ALPHA, kind="note", text="x", about=["FEAT::API", "REQ::NOPE"]
        )
        commit = await call(
            session, "complete_story", project_id=ALPHA, story_id=API, claim_token=token, merge_commit="abc"
        )
        skipped = await call(
            session, "report_task", project_id=ALPHA, task_id=CONTRACT, claim_token=token, progress="skipped"
        )
        mismatch = await call(
            session, "report_task", project_id=ALPHA, task_id=CONTRACT, claim_token=NOT_HELD, progress="done"
        )
        missing = await call(
            session, "report_task", project_id=ALPHA, task_id="TASK::NOPE", claim_token=token, progress="done"
        )

    assert structured(node)["error"] == {
        "code": "node_not_found",
        "message": MESSAGES["node_not_found"],
        "details": {"spec_ids": ["REQ::NOPE"]},
    }
    assert structured(commit)["error"] == {
        "code": "invalid_commit",
        "message": MESSAGES["invalid_commit"],
        "details": {"argument": "merge_commit"},
    }
    assert structured(skipped)["error"]["details"] == {"argument": "note"}
    assert structured(mismatch)["error"] == {"code": "claim_mismatch", "message": MESSAGES["claim_mismatch"]}
    assert structured(missing)["error"] == {"code": "task_not_found", "message": MESSAGES["task_not_found"]}
    for result in (node, commit, skipped, mismatch, missing):
        assert result.is_error and json.loads(text(result)) == structured(result)


async def test_the_audit_holds_ids_and_codes_only_never_text_note_ref_token_commit_or_url(
    monkeypatch, valid_env, central_log
):
    central_log.configure()
    async with over_people(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        claimed = await call(
            session, "claim_story", project_id=ALPHA, story_id=API, agent_label=LABEL, branch="secret/claim-branch"
        )
        token = structured(claimed)["claim_token"]
        await call(session, "list_areas", project_id=ALPHA)
        await call(session, "list_stories", project_id=ALPHA)
        await call(session, "preview_story", project_id=ALPHA, story_id=API)
        await call(
            session,
            "release_story",
            project_id=ALPHA,
            story_id=API,
            claim_token=NOT_HELD,
            note="SecretFailedNote",
            branch="secret/failed-branch",
        )
        await call(
            session,
            "report_task",
            project_id=ALPHA,
            task_id=CONTRACT,
            claim_token=NOT_HELD,
            progress="skipped",
            note="SecretFailedReport",
        )
        await call(
            session,
            "add_finding",
            project_id=ALPHA,
            kind="gap",
            text="SecretText",
            about=["FEAT::API"],
            refers_to=[{"type": "file", "value": "secret/ref.py"}],
        )
        await call(
            session,
            "report_task",
            project_id=ALPHA,
            task_id=CONTRACT,
            claim_token=token,
            progress="skipped",
            note="SecretNote",
        )
        await call(
            session,
            "complete_story",
            project_id=ALPHA,
            story_id=API,
            claim_token=token,
            merge_commit=COMMIT,
            pr_url="https://secret.example.test/pr/1",
        )
        await call(session, "complete_story", project_id=ALPHA, story_id=API, claim_token=NOT_HELD, merge_commit=COMMIT)
    tools = [(e["tool"], e["outcome"]) for e in central_log.events("mcp.tool.completed")]
    assert tools == [
        ("claim_story", "ok"),
        ("list_areas", "ok"),
        ("list_stories", "ok"),
        ("preview_story", "ok"),
        ("release_story", "claim_mismatch"),
        ("report_task", "claim_mismatch"),
        ("add_finding", "ok"),
        ("report_task", "ok"),
        ("complete_story", "ok"),
        ("complete_story", "claim_mismatch"),
    ]
    written = central_log.events("api.agent.written")
    assert [(e["route"], e["outcome"], e["repeat"]) for e in written] == [
        ("story-claim", "ok", False),
        ("story-release", "claim_mismatch", False),
        ("task-progress", "claim_mismatch", False),
        ("findings", "ok", False),
        ("task-progress", "ok", False),
        ("story-completion", "ok", False),
        ("story-completion", "claim_mismatch", False),
    ]
    assert written[3]["finding_id"].startswith("FND::") and written[4]["task_id"] == CONTRACT
    everything = json.dumps(central_log.records(), default=str)
    for private in (token, NOT_HELD, *SECRETS):
        assert private not in everything, private


async def test_a_member_presenting_the_holders_valid_token_gets_claim_mismatch_on_every_claim_write(
    monkeypatch, valid_env
):
    # Ben holds API; Alice, through the MCP role, presents Ben's current token: the library refuses all three writes.
    async with over_people(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        people = stack.people
        bens = await people.hold(API, people.ben)
        before = (
            {k: dict(v) for k, v in people.memory.stories.items()},
            {k: dict(v) for k, v in people.memory.tasks.items()},
        )
        results = [
            await call(session, "release_story", project_id=ALPHA, story_id=API, claim_token=bens),
            await call(session, "report_task", project_id=ALPHA, task_id=CONTRACT, claim_token=bens, progress="done"),
            await call(
                session, "complete_story", project_id=ALPHA, story_id=API, claim_token=bens, merge_commit=COMMIT
            ),
        ]
        after = (
            {k: dict(v) for k, v in people.memory.stories.items()},
            {k: dict(v) for k, v in people.memory.tasks.items()},
        )
    assert after == before
    for result in results:
        assert result.is_error
        assert structured(result)["error"] == {"code": "claim_mismatch", "message": MESSAGES["claim_mismatch"]}


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("add_finding", {"project_id": ALPHA, "kind": "gap", "text": "x", "about": ["F" * 201]}),
        ("add_finding", {"project_id": ALPHA, "kind": "rumour", "text": "x", "about": ["F"]}),
        ("add_finding", {"project_id": ALPHA, "kind": "gap", "text": "", "about": ["F"]}),
        ("add_finding", {"project_id": ALPHA, "kind": "gap", "text": "x" * 2001, "about": ["F"]}),
        ("add_finding", {"project_id": ALPHA, "kind": "gap", "text": "x", "about": []}),
        ("add_finding", {"project_id": ALPHA, "kind": "gap", "text": "x", "about": ["F"] * 21}),
        (
            "add_finding",
            {
                "project_id": ALPHA,
                "kind": "gap",
                "text": "x",
                "about": ["F"],
                "refers_to": [{"type": "mail", "value": "a"}],
            },
        ),
        (
            "add_finding",
            {
                "project_id": ALPHA,
                "kind": "gap",
                "text": "x",
                "about": ["F"],
                "refers_to": [{"type": "pr", "value": "1"}] * 11,
            },
        ),
        ("report_task", {"project_id": ALPHA, "task_id": "T", "claim_token": NOT_HELD}),
        ("report_task", {"project_id": ALPHA, "task_id": "T", "claim_token": NOT_HELD, "progress": "finished"}),
        (
            "report_task",
            {"project_id": ALPHA, "task_id": "T", "claim_token": NOT_HELD, "progress": "done", "note": "n" * 501},
        ),
        ("complete_story", {"project_id": ALPHA, "story_id": API, "claim_token": NOT_HELD}),
        ("complete_story", {"project_id": ALPHA, "story_id": API, "claim_token": "", "merge_commit": COMMIT}),
        (
            "complete_story",
            {
                "project_id": ALPHA,
                "story_id": API,
                "claim_token": NOT_HELD,
                "merge_commit": COMMIT,
                "done": ["T"] * 201,
            },
        ),
        (
            "complete_story",
            {
                "project_id": ALPHA,
                "story_id": API,
                "claim_token": NOT_HELD,
                "merge_commit": COMMIT,
                "done": ["T" * 201],
            },
        ),
    ],
)
async def test_arguments_outside_the_input_schema_are_refused_before_any_api_call(
    monkeypatch, valid_env, tool, arguments
):
    async with over_people(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        result = await call(session, tool, **arguments)
    assert result.is_error
    assert stack.api.agent_calls() == []
    assert result.structured_content is None  # the SDK's own refusal, not a tool error with a code
