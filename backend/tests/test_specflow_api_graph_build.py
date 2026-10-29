"""Tier 1: ``POST``/``GET /api/v1/projects/{pid}/graph_build`` and ``GraphBuildSettings``.

The runner is a fake on ``Runtime.graph_builds``; handlers never touch Neo4j, so
the harness's ``FakeDriver`` must see no query.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from api.graph_build.runner import BuildState
from db import sources as sources_repo
from runtime_defaults_support import RUNTIME_DEFAULTS
from settings import DEFAULT_GRAPH_STATE_DIR, ConfigurationError, load_settings
from specflow_api_support import bearer

STARTED = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
GRAPH_ENV = {
    "GRAPH_LLM_MODEL": "main/model",
    "GRAPH_SMALL_MODEL": "small/model",
    "GRAPH_EMBEDDING_MODEL": "embed/model",
    "GRAPH_EMBEDDING_DIM": "1536",
}


class FakeRunner:
    """Records starts; ``state`` is what ``status`` answers when nothing runs."""

    def __init__(self) -> None:
        self.starts: list[str] = []
        self.active: set[str] = set()
        self.state = BuildState("idle")

    def running(self, project_id: str) -> bool:
        return project_id in self.active

    async def start(self, project_id: str) -> BuildState:
        if project_id not in self.active:
            self.starts.append(project_id)
            self.active.add(project_id)
        return await self.status(project_id)

    async def status(self, project_id: str) -> BuildState:
        if project_id in self.active:
            return BuildState("running", started_at=STARTED)
        return self.state


@pytest.fixture
def processed(monkeypatch):
    counts: dict[uuid.UUID, int] = {}

    async def count(session, project_id):
        return counts.get(project_id, 0)

    monkeypatch.setattr(sources_repo, "count_processed_documents", count)
    return counts


async def project(harness, email: str = "alice@example.com") -> uuid.UUID:
    response = await harness.client.post("/api/v1/projects", json={"name": "Shop"}, headers=bearer(email))
    assert response.status_code == 201
    return uuid.UUID(response.json()["id"])


def url(pid: uuid.UUID) -> str:
    return f"/api/v1/projects/{pid}/graph_build"


async def test_post_starts_one_build_and_answers_202_at_once(harness, processed):
    runner = harness.runtime.graph_builds = FakeRunner()
    pid = await project(harness)
    processed[pid] = 2

    response = await harness.client.post(url(pid), headers=bearer())

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "running"
    assert body["started_at"] == "2026-10-26T09:00:00Z"
    assert runner.starts == [str(pid)]
    assert harness.driver.queries == 0


async def test_a_second_post_while_running_returns_the_same_build(harness, processed):
    runner = harness.runtime.graph_builds = FakeRunner()
    pid = await project(harness)
    processed[pid] = 1

    first = await harness.client.post(url(pid), headers=bearer())
    second = await harness.client.post(url(pid), headers=bearer())

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert runner.starts == [str(pid)]


async def test_a_non_member_gets_404(harness, processed):
    runner = harness.runtime.graph_builds = FakeRunner()
    pid = await project(harness)
    processed[pid] = 1
    stranger = bearer("bob@example.com")

    for method in ("POST", "GET"):
        response = await harness.client.request(method, url(pid), headers=stranger)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "project_not_found"
    missing = await harness.client.post(url(uuid.uuid4()), headers=bearer())
    assert missing.status_code == 404
    assert runner.starts == []


async def test_without_graph_settings_the_feature_answers_503(harness, processed):
    assert harness.runtime.graph_builds is None  # valid_env has no GRAPH_* model
    pid = await project(harness)
    processed[pid] = 1

    for method in ("POST", "GET"):
        response = await harness.client.request(method, url(pid), headers=bearer())
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "graph_build_unavailable"


async def test_a_project_without_processed_documents_gets_409(harness, processed):
    runner = harness.runtime.graph_builds = FakeRunner()
    pid = await project(harness)

    response = await harness.client.post(url(pid), headers=bearer())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_processed_documents"
    assert runner.starts == []


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (BuildState("idle"), {"status": "idle", "build_id": None, "error_code": None}),
        (
            BuildState(
                "succeeded",
                build_id="b1",
                mode="first",
                started_at=STARTED,
                finished_at=STARTED,
                settled=True,
                spec_changes={"added": 12},
                verification_ok=True,
            ),
            {
                "status": "succeeded",
                "build_id": "b1",
                "mode": "first",
                "settled": True,
                "spec_changes": {"added": 12},
                "verification_ok": True,
                "finished_at": "2026-10-26T09:00:00Z",
                "error_code": None,
            },
        ),
        (
            BuildState("failed", build_id="b2", mode="update", error_code="llm_fatal"),
            {"status": "failed", "build_id": "b2", "error_code": "llm_fatal", "settled": None},
        ),
    ],
)
async def test_get_maps_the_stored_states(harness, state, expected):
    runner = harness.runtime.graph_builds = FakeRunner()
    runner.state = state
    pid = await project(harness)

    response = await harness.client.get(url(pid), headers=bearer())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "status",
        "build_id",
        "mode",
        "started_at",
        "finished_at",
        "settled",
        "spec_changes",
        "verification_ok",
        "error_code",
    }
    assert {key: body[key] for key in expected} == expected
    assert harness.driver.queries == 0


async def test_get_reports_a_running_build(harness, processed):
    harness.runtime.graph_builds = FakeRunner()
    pid = await project(harness)
    processed[pid] = 1
    await harness.client.post(url(pid), headers=bearer())

    response = await harness.client.get(url(pid), headers=bearer())

    assert response.json()["status"] == "running"


# Settings ----------------------------------------------------------------------


def test_the_graph_build_is_off_when_every_model_is_blank(valid_env):
    blank = dict.fromkeys(GRAPH_ENV, "") | {"GRAPH_STATE_DIR": ""}

    assert load_settings("api", valid_env).graph_build is None
    assert load_settings("api", {**valid_env, **blank}).graph_build is None
    # The template's dimension alone does not turn it on.
    assert load_settings("api", {**valid_env, "GRAPH_EMBEDDING_DIM": "1536"}).graph_build is None


def test_the_graph_build_settings_map_to_the_library_config(valid_env):
    settings = load_settings("api", {**valid_env, **GRAPH_ENV}).graph_build

    assert settings is not None
    assert settings.state_dir == DEFAULT_GRAPH_STATE_DIR
    llm = settings.to_llm_config()
    assert (llm.main_model, llm.small_model, llm.embedding_model, llm.embedding_dim) == (
        "main/model",
        "small/model",
        "embed/model",
        1536,
    )
    assert llm.endpoint == valid_env["LLM_GATEWAY_URL"]
    assert isinstance(llm.api_key, SecretStr)
    assert "gateway-key" not in repr(settings) + repr(llm)


def test_graph_build_settings_are_api_only(valid_env):
    env = {**valid_env, **GRAPH_ENV}

    assert load_settings("worker", env).graph_build is None
    assert load_settings("migrate", env).graph_build is None


@pytest.mark.parametrize("dim", ["0", "abc", "5000"])
def test_a_bad_embedding_dim_is_reported(valid_env, dim):
    with pytest.raises(ConfigurationError) as error:
        load_settings("api", {**valid_env, **GRAPH_ENV, "GRAPH_EMBEDDING_DIM": dim})

    assert error.value.variables == ("GRAPH_EMBEDDING_DIM",)


def test_a_partial_graph_configuration_names_the_missing_variables(valid_env):
    env = {**valid_env, "GRAPH_LLM_MODEL": "main/model"}
    env.pop("LLM_GATEWAY_URL")

    with pytest.raises(ConfigurationError) as error:
        load_settings("api", env)

    assert set(error.value.variables) == {
        "LLM_GATEWAY_URL",
        "GRAPH_SMALL_MODEL",
        "GRAPH_EMBEDDING_MODEL",
    }  # GRAPH_EMBEDDING_DIM has a default


def test_a_blank_llm_gateway_key_is_refused_outside_local(valid_env):
    local = {**valid_env, **GRAPH_ENV, "LLM_GATEWAY_API_KEY": ""}
    assert load_settings("api", local).graph_build is not None

    deployed = {**local, "APP_ENV": "dev"}
    with pytest.raises(ConfigurationError) as error:
        load_settings("api", deployed)
    assert "LLM_GATEWAY_API_KEY" in error.value.variables


def test_the_embedding_dim_defaults_to_1536(valid_env):
    env = {**valid_env, **GRAPH_ENV}
    env.pop("GRAPH_EMBEDDING_DIM", None)

    assert load_settings("api", env).graph_build.embedding_dim == RUNTIME_DEFAULTS["GRAPH_EMBEDDING_DIM"]
