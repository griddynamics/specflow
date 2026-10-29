"""Tier 1 (offline) tests of the Temporal core: ids, queues, registry, models,
provider, plugin, client wiring order, worker runner and the AC-16.0 ban.

No Temporal server is contacted and nothing is downloaded.
"""

from __future__ import annotations

import subprocess
import sys
import types
from importlib.util import find_spec
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from agent_runtime import plugin as plugin_module
from settings import load_settings
from temporal import ids, queues, registry
from temporal.agents import models as agent_models
from temporal.documents import models as doc_models
from temporal.registry import Registration, RegistrationUnavailableError

BACKEND = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- ids, queues
def test_workflow_ids_follow_the_spec_and_take_an_optional_prefix():
    did = uuid4()

    assert ids.parse_workflow_id(did) == f"doc-parse-{did}"
    assert ids.summary_workflow_id(did) == f"doc-summary-{did}"
    assert ids.parse_workflow_id(did, prefix="pytest-1-") == f"pytest-1-doc-parse-{did}"
    assert ids.summary_workflow_id(str(did), prefix="p-") == f"p-doc-summary-{did}"


def test_queue_names_come_from_settings_and_take_a_prefix(valid_env):
    temporal = load_settings("worker", valid_env).temporal

    assert queues.queue_name(temporal, "parse") == "specflow-parse"
    assert queues.queue_name(temporal, "agents") == "specflow-agents"
    assert queues.queue_name(temporal, "parse", prefix="pytest-1-") == "pytest-1-specflow-parse"
    with pytest.raises(ValueError, match="Unknown queue"):
        queues.queue_name(temporal, "diagnostics")


def test_queue_names_honour_the_configured_overrides(valid_env):
    env = {**valid_env, "TEMPORAL_QUEUE_PARSE": "q-parse", "TEMPORAL_QUEUE_AGENTS": "q-agents"}
    temporal = load_settings("worker", env).temporal

    assert queues.polled_queues(temporal) == (("parse", "q-parse"), ("agents", "q-agents"))


@pytest.mark.parametrize(
    ("worker_queues", "expected"),
    [
        ("parse,agents", ("parse", "agents")),
        ("agents", ("agents",)),
        ("parse", ("parse",)),
    ],
)
def test_polled_queues_follow_worker_queues(valid_env, worker_queues, expected):
    temporal = load_settings("worker", {**valid_env, "WORKER_QUEUES": worker_queues}).temporal

    assert tuple(name for name, _ in queues.polled_queues(temporal)) == expected


def test_the_queue_option_replaces_worker_queues(valid_env):
    temporal = load_settings("worker", valid_env, queues=["agents"]).temporal

    assert queues.polled_queues(temporal) == (("agents", "specflow-agents"),)


def test_sandbox_modules_do_not_pull_sqlalchemy_google_or_neo4j():
    code = (
        "import sys\n"
        "import temporal.ids, temporal.queues\n"
        "import temporal.documents.models, temporal.agents.models\n"
        "bad = [m for m in ('sqlalchemy', 'google', 'neo4j', 'settings') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=BACKEND, timeout=60, check=False
    )

    assert result.returncode == 0, result.stderr[-500:]


# ---------------------------------------------------------------------- models
def _instances() -> list:
    pid, did, uid = uuid4(), uuid4(), uuid4()
    return [
        doc_models.ParseDocumentInput(project_id=pid, document_id=did),
        doc_models.ParseDocumentResult(document_id=did, status="parsed", markdown_chars=12),
        doc_models.MarkFailedInput(project_id=pid, document_id=did, error_code="pdf_parse_failed"),
        agent_models.SummarizeDocumentInput(
            project_id=pid, document_id=did, requested_by=uid, model="m/x", trace_sensitive_data=True
        ),
        agent_models.SummarizeDocumentResult(
            document_id=did,
            summary_chars=5,
            title="T",
            model="m/x",
            usage=agent_models.UsageSummary(requests=2, input_tokens=3, output_tokens=4),
        ),
        agent_models.DocumentRunContext(project_id=pid, run_id=f"doc-summary-{did}", document_id=did),
        agent_models.ReadChunkInput(project_id=pid, document_id=did, offset=0),
        agent_models.DocumentChunk(text="abc", offset=0, next_offset=None, total_chars=3),
        agent_models.SaveSummarizationInput(
            project_id=pid,
            document_id=did,
            summary="s",
            title="T",
            model="m/x",
            workflow_id="w",
            requested_by=uid,
        ),
        agent_models.SummaryOutput(title="T", summary="S"),
    ]


@pytest.mark.parametrize("instance", _instances(), ids=lambda m: type(m).__name__)
def test_models_round_trip_through_json(instance):
    again = type(instance).model_validate_json(instance.model_dump_json())

    assert again == instance


def test_read_chunk_input_defaults_and_bounds():
    base = {"project_id": uuid4(), "document_id": uuid4()}

    assert agent_models.ReadChunkInput(**base, offset=0).limit == 16000
    assert agent_models.CHUNK_LIMIT == 16000
    with pytest.raises(ValidationError):
        agent_models.ReadChunkInput(**base, offset=-1)
    with pytest.raises(ValidationError):
        agent_models.ReadChunkInput(**base, offset=0, limit=16001)


def test_summarize_input_requires_a_non_empty_model():
    base = {
        "project_id": uuid4(),
        "document_id": uuid4(),
        "requested_by": uuid4(),
        "trace_sensitive_data": False,
    }

    with pytest.raises(ValidationError):
        agent_models.SummarizeDocumentInput(**base, model="")


def test_summary_output_bounds_match_the_spec():
    agent_models.SummaryOutput(title="t" * 200, summary="s" * 8000)
    for title, summary in [("", "s"), ("t", ""), ("t" * 201, "s"), ("t", "s" * 8001)]:
        with pytest.raises(ValidationError):
            agent_models.SummaryOutput(title=title, summary=summary)


def test_result_models_carry_exactly_the_spec_fields():
    assert set(doc_models.ParseDocumentResult.model_fields) == {
        "document_id",
        "status",
        "markdown_chars",
    }
    assert set(agent_models.SummarizeDocumentResult.model_fields) == {
        "document_id",
        "summary_chars",
        "title",
        "model",
        "usage",
    }
    assert set(agent_models.SummarizeDocumentInput.model_fields) == {
        "project_id",
        "document_id",
        "requested_by",
        "model",
        "trace_sensitive_data",
    }
    assert agent_models.MarkFailedInput is doc_models.MarkFailedInput


# -------------------------------------------------------------------- registry
def _registration() -> Registration:
    return Registration(workflows=[], activities=[])


def test_registry_maps_queues_to_the_registration_modules():
    assert registry.REGISTRATION_MODULES == {
        "parse": "temporal.documents.registration",
        "agents": "temporal.agents.registration",
    }


def test_registry_imports_the_queue_module_lazily_and_calls_register(valid_env):
    settings = load_settings("worker", valid_env)
    imported: list[str] = []
    expected = Registration(workflows=[object], activities=[len])

    def fake_import(name: str):
        imported.append(name)
        return types.SimpleNamespace(register=lambda s: expected if s is settings else None)

    assert registry.load_registration("agents", settings, import_module=fake_import) is expected
    assert imported == ["temporal.agents.registration"]


def test_registry_reports_a_missing_registration_module_clearly(valid_env):
    settings = load_settings("worker", valid_env)

    def missing(name: str):
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    with pytest.raises(RegistrationUnavailableError) as error:
        registry.load_registration("parse", settings, import_module=missing)

    assert "temporal.documents.registration" in str(error.value)
    assert "parse" in str(error.value)


def test_registry_does_not_hide_a_broken_dependency_of_a_registration(valid_env):
    settings = load_settings("worker", valid_env)

    def broken(name: str):
        raise ModuleNotFoundError("No module named 'sqlalchemy'", name="sqlalchemy")

    with pytest.raises(ModuleNotFoundError):
        registry.load_registration("parse", settings, import_module=broken)


@pytest.mark.parametrize(
    "module",
    [types.SimpleNamespace(), types.SimpleNamespace(register=lambda _s: "not a registration")],
)
def test_registry_rejects_a_registration_module_of_the_wrong_shape(valid_env, module):
    settings = load_settings("worker", valid_env)

    with pytest.raises(RegistrationUnavailableError):
        registry.load_registration("parse", settings, import_module=lambda _name: module)


def test_registry_rejects_an_unknown_queue(valid_env):
    with pytest.raises(RegistrationUnavailableError):
        registry.load_registration("diagnostics", load_settings("worker", valid_env))


def test_the_real_registry_gives_a_clear_error_until_the_modules_exist(valid_env):
    """Skips once the parse/agent steps deliver their registration modules."""

    settings = load_settings("worker", valid_env)
    for queue, module in registry.REGISTRATION_MODULES.items():
        try:
            delivered = find_spec(module) is not None
        except ModuleNotFoundError:  # a parent package of the module is missing
            delivered = False
        if not delivered:
            with pytest.raises(RegistrationUnavailableError, match=module):
                registry.load_registration(queue, settings)


def test_the_plugin_adds_the_model_activity_to_every_worker(valid_env):
    """AC-8.5 (L1): each queue runs its own workflows and activities, and the
    plugin registers ``invoke_model_activity`` on every worker built from it."""

    settings = load_settings("worker", valid_env)
    plugin = plugin_module.build_plugin(settings)
    parse = Registration(workflows=[], activities=[len])
    agents = Registration(workflows=[], activities=[abs])

    for registration in (parse, agents):
        config = plugin.configure_worker(
            {
                "activities": list(registration.activities),
                "workflow_runner": SandboxedWorkflowRunner(),
                "client": types.SimpleNamespace(config=lambda **_kwargs: {}),
            }
        )
        names = {getattr(a, "__name__", "") for a in config["activities"]}
        assert {"invoke_model_activity", "invoke_model_activity_streaming"} <= names
    parse_only = {getattr(a, "__name__", "") for a in parse.activities}
    assert "abs" not in parse_only
