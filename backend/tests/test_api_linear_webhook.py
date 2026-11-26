"""Tier 1: the Linear webhook route (``POST /api/v1/integrations/linear/webhook``, docs/linear/gaps.md "Webhook"): the
signature of the RAW body (good, wrong, missing, over another body), the timestamp (stale and future), the secret
not set (503), a comment handed to the channel once per delivery, an issue nobody posted (a quiet 200), and the
OAuth app revoked. The route runs on a small app with the error envelope and the fakes of ``linear_gaps_support``;
no network."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr

from api.errors import install_error_handlers
from api.linear_gaps.channel import LinearGaps
from api.linear_gaps.webhook import SIGNATURE_HEADER
from api.routers import linear_webhook
from jira_gaps_support import gap
from linear.models import NewIssue
from linear_gaps_support import (
    ANA,
    ANA_LINEAR,
    ORG,
    PROJECT,
    SIGNING_KEY,
    Rig,
    build_rig,
    comment_event,
    delivery,
    sign,
)

PATH = "/api/v1/integrations/linear/webhook"
NOT_A_QUESTION = "0b0c1d2e-3f40-4a51-8b62-73c84d95e6f7"


@dataclass
class Served:
    http: httpx.AsyncClient
    rig: Rig
    channel: LinearGaps

    async def post(self, payload: dict[str, Any], **over: Any) -> httpx.Response:
        raw, headers = delivery(payload, **over)
        return await self.http.post(PATH, content=raw, headers=headers)

    async def settle(self) -> None:
        tasks = list(self.channel._tasks)
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


def app_of(runtime: Any) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(linear_webhook.router, prefix="/api/v1")
    app.state.runtime = runtime
    return app


@pytest_asyncio.fixture
async def served(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Served]:
    rig = build_rig(monkeypatch)
    runtime = rig.runtime
    runtime.settings = SimpleNamespace(linear=SimpleNamespace(webhook_secret=SecretStr(SIGNING_KEY)))
    channel = rig.channel()
    runtime.linear_gaps = channel
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_of(runtime)), base_url="http://t") as http:
        yield Served(http, rig, channel)
    await channel.aclose()


async def question(served: Served) -> Any:
    """One posted question; the sub-issue it is asked in."""

    rig = served.rig
    await rig.enable("one")
    rig.service.add(gap("G-1"))
    await rig.poster().post_open(str(PROJECT))
    return rig.children()[0]


def code_of(answer: httpx.Response) -> tuple[int, str]:
    return answer.status_code, answer.json()["error"]["code"]


# The signature --------------------------------------------------------------------------------------------------


async def test_a_signed_comment_is_answered_from_the_background_and_acknowledged_at_once(served: Served) -> None:
    child = await question(served)
    event = comment_event(served.rig.say(child.id, ANA_LINEAR, "A"))

    answer = await served.post(event)
    await served.settle()

    assert (answer.status_code, answer.json()) == (200, {"ok": True})
    [recorded] = served.rig.service.answers
    assert (recorded.gap_id, recorded.via, recorded.answered_by) == ("G-1", "linear", str(ANA))
    assert (await served.rig.linear.issue(child.id)).state.type == "completed"


async def test_the_body_is_read_raw_so_any_json_spacing_verifies(served: Served) -> None:
    child = await question(served)
    raw = json.dumps(comment_event(served.rig.say(child.id, ANA_LINEAR, "A")), indent=4, sort_keys=True).encode()

    answer = await served.http.post(PATH, content=raw, headers={SIGNATURE_HEADER: sign(raw)})

    assert answer.status_code == 200


async def test_a_wrong_signature_is_refused_and_nothing_is_read(served: Served) -> None:
    child = await question(served)
    event = comment_event(served.rig.say(child.id, ANA_LINEAR, "A"))
    raw, _ = delivery(event)

    for signature in (sign(raw, "another-secret"), sign(raw + b" "), "", "zz"):
        answer = await served.http.post(PATH, content=raw, headers={SIGNATURE_HEADER: signature})
        assert code_of(answer) == (401, "linear_webhook_signature_invalid")
    await served.settle()

    assert served.rig.service.answers == []


async def test_a_body_changed_after_signing_is_refused(served: Served) -> None:
    child = await question(served)
    raw, headers = delivery(comment_event(served.rig.say(child.id, ANA_LINEAR, "A")))

    answer = await served.http.post(PATH, content=raw.replace(b'"A"', b'"B"'), headers=headers)

    assert code_of(answer) == (401, "linear_webhook_signature_invalid")


async def test_a_missing_signature_is_refused(served: Served) -> None:
    raw, _ = delivery({"type": "Comment"})

    answer = await served.http.post(PATH, content=raw)

    assert code_of(answer) == (401, "linear_webhook_signature_invalid")


async def test_a_signed_body_that_is_not_json_is_refused(served: Served) -> None:
    raw = b"not json"

    answer = await served.http.post(PATH, content=raw, headers={SIGNATURE_HEADER: sign(raw)})

    assert code_of(answer) == (401, "linear_webhook_signature_invalid")


@pytest.mark.parametrize("offset_ms", [-61_000, 61_000, -3_600_000])
async def test_a_delivery_more_than_a_minute_off_is_refused(served: Served, offset_ms: int) -> None:
    child = await question(served)
    comment = served.rig.say(child.id, ANA_LINEAR, "A")
    stamp = int(time.time() * 1000) + offset_ms

    answer = await served.post(comment_event(comment, webhookTimestamp=stamp))
    await served.settle()

    assert code_of(answer) == (401, "linear_webhook_signature_invalid")
    assert served.rig.service.answers == []


@pytest.mark.parametrize("stamp", [None, "yesterday", True])
async def test_a_delivery_without_a_usable_timestamp_is_refused(served: Served, stamp: Any) -> None:
    child = await question(served)
    event = comment_event(served.rig.say(child.id, ANA_LINEAR, "A"), webhookTimestamp=stamp)

    assert code_of(await served.post(event)) == (401, "linear_webhook_signature_invalid")


async def test_a_delivery_a_few_seconds_old_is_accepted(served: Served) -> None:
    child = await question(served)
    event = comment_event(served.rig.say(child.id, ANA_LINEAR, "A"), webhookTimestamp=int(time.time() * 1000) - 30_000)

    assert (await served.post(event)).status_code == 200


async def test_without_a_signing_secret_the_route_answers_503(served: Served) -> None:
    served.rig.runtime.settings = SimpleNamespace(linear=SimpleNamespace(webhook_secret=None))

    assert code_of(await served.post({"type": "Comment"})) == (503, "linear_webhook_unavailable")

    served.rig.runtime.settings = SimpleNamespace(linear=None)  # Linear is off on this server

    assert code_of(await served.post({"type": "Comment"})) == (503, "linear_webhook_unavailable")


# What is done with a good delivery -----------------------------------------------------------------------------------


async def test_a_delivery_twice_is_answered_once(served: Served) -> None:
    child = await question(served)
    event = comment_event(served.rig.say(child.id, ANA_LINEAR, "A"))

    first = await served.post(event, delivery_id="d-7")
    second = await served.post(event, delivery_id="d-7")  # Linear delivered it again
    await served.settle()

    assert (first.status_code, second.status_code) == (200, 200)
    assert len(served.rig.service.answers) == 1 and len(await served.rig.comments_of(child.id)) == 1


async def test_a_second_delivery_of_an_answered_question_changes_nothing_either(served: Served) -> None:
    # Another delivery id (a retry after a restart forgot the first): the row says it was answered.
    child = await question(served)
    event = comment_event(served.rig.say(child.id, ANA_LINEAR, "A"))

    await served.post(event, delivery_id="d-1")
    await served.settle()
    await served.post(event, delivery_id="d-2")
    await served.settle()

    assert len(served.rig.service.answers) == 1 and len(await served.rig.comments_of(child.id)) == 1


async def test_a_comment_in_an_issue_nobody_posted_is_a_quiet_200(served: Served) -> None:
    await question(served)
    stranger_issue = await served.rig.linear.create_issue(
        NewIssue(NOT_A_QUESTION, served.rig.team.id, "Not a question")
    )
    event = comment_event(served.rig.say(stranger_issue.id, ANA_LINEAR, "A"))
    writes = len(served.rig.linear.writes)

    answer = await served.post(event)
    await served.settle()

    assert answer.status_code == 200 and served.rig.service.answers == []
    assert len(served.rig.linear.writes) == writes


@pytest.mark.parametrize(
    "over",
    [
        {"action": "update"},
        {"action": "remove"},
        {"type": "Issue"},
        {"type": "Reaction"},
    ],
)
async def test_other_events_are_acknowledged_and_ignored(served: Served, over: dict[str, str]) -> None:
    child = await question(served)
    event = comment_event(served.rig.say(child.id, ANA_LINEAR, "A"), **over)

    answer = await served.post(event)
    await served.settle()

    assert answer.status_code == 200 and served.rig.service.answers == []


async def test_a_comment_without_an_issue_is_ignored(served: Served) -> None:
    child = await question(served)
    event = comment_event(served.rig.say(child.id, ANA_LINEAR, "A"))
    event["data"].pop("issueId")  # a comment on a project update has none

    assert (await served.post(event)).status_code == 200


async def test_without_gap_runs_a_comment_is_acknowledged_and_nothing_is_read(served: Served) -> None:
    child = await question(served)
    event = comment_event(served.rig.say(child.id, ANA_LINEAR, "A"))
    served.rig.runtime.linear_gaps = None

    assert (await served.post(event)).status_code == 200
    assert served.rig.service.answers == []


# The app is revoked -----------------------------------------------------------------------------------------------


async def test_a_revoked_app_marks_the_connections_of_the_workspace_revoked(served: Served) -> None:
    payload = {
        "type": "OAuthApp",
        "action": "revoked",
        "organizationId": ORG,
        "webhookTimestamp": int(time.time() * 1000),
        "data": {},
    }

    answer = await served.post(payload)

    assert answer.status_code == 200
    assert served.rig.db.revoked == [(ORG, None)]


async def test_a_revoked_app_works_without_gap_runs_and_an_unsigned_one_does_nothing(served: Served) -> None:
    served.rig.runtime.linear_gaps = None
    payload = {
        "type": "OAuthApp",
        "action": "revoked",
        "organizationId": ORG,
        "webhookTimestamp": int(time.time() * 1000),
    }
    raw, _ = delivery(payload)

    refused = await served.http.post(PATH, content=raw, headers={SIGNATURE_HEADER: "00"})
    allowed = await served.post(payload)

    assert (refused.status_code, allowed.status_code) == (401, 200)
    assert served.rig.db.revoked == [(ORG, None)]
