"""Tier 1: repositories logic, agent_scope, migrate and the role script, without a database."""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import ProgrammingError

from db import migrate, repositories
from db.agent_scope import agent_scope, build_agent_session_factory
from db.models import Document
from db.session import build_engine
from settings import AgentDatabaseSettings, DatabaseSettings, load_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
ROLE_SCRIPT = REPO_ROOT / "backend" / "postgres-init" / "10-specflow-roles.sh"
BASH = shutil.which("bash") or "bash"
PLACEHOLDER_CREDENTIAL = "not-a-real-credential"
PID = uuid.UUID("11111111-1111-4111-8111-111111111111")
DID = uuid.UUID("22222222-2222-4222-8222-222222222222")


class RecordingSession:
    """Records executed statements; ``rowcount`` is how many rows an UPDATE returns."""

    def __init__(self, rowcount: int = 1) -> None:
        self.statements: list[tuple[Any, Any]] = []
        self.rowcount = rowcount

    async def execute(self, statement: Any, params: Any = None) -> Any:
        self.statements.append((statement, params))
        return SimpleNamespace(rowcount=self.rowcount)

    async def scalars(self, statement: Any) -> Any:
        self.statements.append((statement, None))
        row = object() if self.rowcount else None
        return SimpleNamespace(one_or_none=lambda: row)

    def sql(self, index: int = 0) -> str:
        statement = self.statements[index][0]
        return str(statement.compile(dialect=postgresql.dialect()))


# Repositories -----------------------------------------------------------------


def test_cursor_round_trips_and_is_url_safe() -> None:
    stamp = datetime(2026, 10, 8, 9, 30, 1, 123456, tzinfo=UTC)

    cursor = repositories.encode_cursor(stamp, DID)

    assert re.fullmatch(r"[A-Za-z0-9_-]+", cursor)
    assert repositories.decode_cursor(cursor) == (stamp, DID)


@pytest.mark.parametrize("cursor", ["", "!!!", "bm90LWEtY3Vyc29y", "MjAyNi0xMC0wOHx4"])
def test_a_bad_cursor_is_rejected(cursor: str) -> None:
    with pytest.raises(repositories.InvalidCursorError):
        repositories.decode_cursor(cursor)


@pytest.mark.parametrize(
    ("email", "actor"),
    [
        ("J.Doe@example.com", "human:j.doe"),
        ("a+b_c-d@x.org", "human:ab_c-d"),
        ("Zoë@x.org", "human:zo"),
    ],
)
def test_actor_id_is_the_lowercased_filtered_local_part(email: str, actor: str) -> None:
    assert repositories.actor_id_for(email) == actor


async def test_mark_processing_is_conditional_on_uploaded_or_failed() -> None:
    session = RecordingSession(rowcount=0)

    changed = await repositories.mark_processing(session, PID, DID)

    assert changed is False
    sql = session.sql()
    assert sql.startswith("UPDATE documents SET status=")
    assert "documents.status IN" in sql
    assert "documents.project_id =" in sql and "documents.id =" in sql
    assert "RETURNING documents.id" in sql


async def test_mark_summarizing_requires_parsed_and_not_running() -> None:
    session = RecordingSession(rowcount=1)

    assert await repositories.mark_summarizing(session, PID, DID) is True

    sql = session.sql()
    assert "summarization_status=" in sql
    assert "documents.status =" in sql
    assert "documents.summarization_status !=" in sql


@pytest.mark.parametrize(
    ("function", "code"),
    [
        (repositories.mark_failed, "Traceback: connection refused"),
        (repositories.mark_failed, "agent_failed"),
        (repositories.mark_summarization_failed, "pdf_parse_failed"),
        (repositories.mark_summarization_failed, "boom"),
    ],
)
async def test_failures_accept_only_the_fixed_codes(function: Any, code: str) -> None:
    session = RecordingSession()

    with pytest.raises(ValueError, match="unknown error code"):
        await function(session, PID, DID, code)

    assert session.statements == []


async def test_failure_codes_are_stored_verbatim() -> None:
    session = RecordingSession()

    assert await repositories.mark_failed(session, PID, DID, "pdf_parse_failed")

    params = session.statements[0][0].compile(dialect=postgresql.dialect()).params
    assert params["error"] == "pdf_parse_failed"
    assert params["status"] == "failed"


# agent_scope ------------------------------------------------------------------


class FakeSession(RecordingSession):
    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self.log = log

    async def __aenter__(self) -> FakeSession:
        self.log.append("open")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.log.append("close")

    def begin(self) -> FakeSession.Transaction:
        return FakeSession.Transaction(self.log)

    class Transaction:
        def __init__(self, log: list[str]) -> None:
            self.log = log

        async def __aenter__(self) -> None:
            self.log.append("begin")

        async def __aexit__(self, exc_type: Any, *exc: Any) -> None:
            self.log.append("rollback" if exc_type else "commit")


async def test_agent_scope_sets_the_project_first_in_one_transaction() -> None:
    log: list[str] = []
    session = FakeSession(log)

    async with agent_scope(lambda: session, PID) as scoped:
        assert scoped is session
        await scoped.execute("SELECT 1")

    first, params = session.statements[0]
    assert str(first) == "SELECT set_config('app.project_id', :pid, true)"
    assert params == {"pid": str(PID)}
    assert len(session.statements) == 2
    assert log == ["open", "begin", "commit", "close"]


async def test_agent_scope_rolls_back_on_error() -> None:
    log: list[str] = []

    with pytest.raises(RuntimeError):
        async with agent_scope(lambda: FakeSession(log), PID):
            raise RuntimeError("tool failed")

    assert log == ["open", "begin", "rollback", "close"]


@pytest.mark.parametrize("bad", [str(PID), None, 1])
async def test_agent_scope_requires_a_uuid(bad: Any) -> None:
    with pytest.raises(TypeError):
        async with agent_scope(lambda: FakeSession([]), bad):
            pass


# migrate ----------------------------------------------------------------------


def test_the_source_layout_alembic_ini_is_found(tmp_path: Path) -> None:
    assert migrate.find_alembic_ini({}) == REPO_ROOT / "backend" / "alembic.ini"


def test_alembic_config_overrides_the_location(tmp_path: Path) -> None:
    ini = tmp_path / "alembic.ini"
    ini.write_text("[alembic]\n")

    assert migrate.find_alembic_ini({"ALEMBIC_CONFIG": str(ini)}) == ini
    with pytest.raises(FileNotFoundError, match="ALEMBIC_CONFIG"):
        migrate.find_alembic_ini({"ALEMBIC_CONFIG": str(tmp_path / "missing.ini")})


def test_run_upgrades_to_head_with_the_role_settings(
    valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_settings("migrate", valid_env)
    calls: list[tuple[Any, str]] = []
    revisions = iter([None, "0001_initial"])

    async def fake_current(_db: Any) -> str | None:
        return next(revisions)  # an empty database, then the stamped head

    monkeypatch.setattr(migrate.command, "upgrade", lambda cfg, rev: calls.append((cfg, rev)))
    monkeypatch.setattr(migrate, "current_revision", fake_current)

    assert migrate.run(settings) == 0

    ((cfg, revision),) = calls
    assert revision == "head"
    assert cfg.attributes["agent_user"] == "agent_user"
    assert cfg.attributes["database_url"].database == "specgraph"
    assert cfg.config_file_name == str(REPO_ROOT / "backend" / "alembic.ini")


# The role script --------------------------------------------------------------


def test_the_role_script_is_valid_bash_and_executable() -> None:
    assert ROLE_SCRIPT.stat().st_mode & 0o111
    subprocess.run([BASH, "-n", str(ROLE_SCRIPT)], check=True)


def test_the_role_script_keeps_the_passwords_out_of_argv_and_output() -> None:
    script = ROLE_SCRIPT.read_text()

    assert "\\getenv app_password DATABASE_PASSWORD" in script
    assert "\\getenv agent_password DATABASE_AGENT_PASSWORD" in script
    assert "%L" in script
    assert "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS" in script
    assert "LOGIN NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE" in script
    # The shell never expands a password; only the presence loop reads it indirectly.
    assert re.findall(r"\$\{?(DATABASE_PASSWORD|DATABASE_AGENT_PASSWORD)\b", script) == []
    assert "set -x" not in script


ALL_ROLE_VARS = {
    "POSTGRES_USER": "postgres",
    "DATABASE_NAME": "specflow",
    "DATABASE_USER": "specflow",
    "DATABASE_PASSWORD": "pw-canary-app",
    "DATABASE_AGENT_USER": "specflow_agent",
    "DATABASE_AGENT_PASSWORD": "pw-canary-8c1",
}


def run_role_script(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(ROLE_SCRIPT)],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("env", "names"),
    [
        ({}, list(ALL_ROLE_VARS)),
        (
            {k: v for k, v in ALL_ROLE_VARS.items() if k != "DATABASE_AGENT_PASSWORD"},
            ["DATABASE_AGENT_PASSWORD"],
        ),
    ],
)
def test_the_role_script_names_missing_variables(env: dict[str, str], names: list[str]) -> None:
    result = run_role_script(env)

    assert result.returncode != 0
    for name in names:
        assert name in result.stderr


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"DATABASE_USER": "Postgres"}, "DATABASE_USER must differ"),
        ({"DATABASE_AGENT_USER": "SpecFlow"}, "DATABASE_AGENT_USER must differ"),
        ({"DATABASE_AGENT_USER": "POSTGRES"}, "DATABASE_AGENT_USER must differ"),
    ],
)
def test_the_role_script_refuses_the_admin_and_owner_roles(override: dict[str, str], message: str) -> None:
    result = run_role_script({**ALL_ROLE_VARS, **override})

    assert result.returncode != 0
    assert message in result.stderr
    assert "pw-canary" not in result.stdout + result.stderr


# Validation follow-ups (S2-validation LOW 1, 5, 6) -----------------------------


@pytest.mark.parametrize("limit", [0, -1])
async def test_a_page_limit_below_one_is_a_value_error(limit: int) -> None:
    session = RecordingSession()

    with pytest.raises(ValueError, match="limit must be at least 1"):
        await repositories.list_projects(session, PID, limit)
    with pytest.raises(ValueError, match="limit must be at least 1"):
        await repositories.list_documents(session, PID, limit)
    assert session.statements == []


@pytest.mark.parametrize("email", ["+++@x.org", "@x.org", "Ωμέγα@x.org"])
def test_actor_id_falls_back_to_the_subject_without_valid_characters(email: str) -> None:
    assert repositories.actor_id_for(email, "sub-7") == "human:sub-7"
    assert repositories.actor_id_for("ok@x.org", "sub-7") == "human:ok"


def test_the_duplicate_error_carries_an_immutable_snapshot() -> None:
    row = Document(id=DID, project_id=PID, status="parsed", original_filename="a.pdf")

    error = repositories.DuplicateDocumentError(row)

    assert not isinstance(error.existing, Document)
    assert (error.existing.id, error.existing.project_id) == (DID, PID)
    assert (error.existing.status, error.existing.original_filename) == ("parsed", "a.pdf")
    with pytest.raises(FrozenInstanceError):
        error.existing.status = "failed"


# R-code fixes: large columns, hidden parameters -------------------------------


class CapturingSession:
    """Records statements; ``scalars`` answers an empty result, ``identity_map`` holds nothing."""

    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.identity_map = SimpleNamespace(get=lambda _key: None)

    async def scalars(self, statement: Any) -> Any:
        self.statements.append(statement)
        row = object()
        return SimpleNamespace(all=list, one_or_none=lambda: row)

    async def scalar(self, statement: Any) -> Any:
        self.statements.append(statement)
        return None

    def sql(self, index: int = 0) -> str:
        return str(self.statements[index].compile(dialect=postgresql.dialect()))


def selected_columns(sql: str) -> set[str]:
    """The ``documents`` column names a SELECT list or RETURNING clause names."""

    head = sql.split("RETURNING", 1)[1] if "RETURNING" in sql else sql.split(" FROM ", 1)[0]
    return set(re.findall(r"documents\.(\w+)", head))


LARGE = {"markdown", "summarization"}


async def test_the_document_list_query_selects_neither_markdown_nor_summarization() -> None:
    session = CapturingSession()

    await repositories.list_documents(session, PID, 50)

    columns = selected_columns(session.sql())
    assert {"id", "status", "summarization_status", "markdown_key", "created_at"} <= columns
    assert not LARGE & columns


async def test_the_status_read_skips_the_large_columns_and_the_detail_read_keeps_them() -> None:
    session = CapturingSession()

    await repositories.get_document_state(session, PID, DID)
    await repositories.get_document(session, PID, DID)

    state, detail = (selected_columns(session.sql(i)) for i in (0, 1))
    assert not LARGE & state
    assert detail >= LARGE  # the detail view still returns the markdown


@pytest.mark.parametrize(
    "call",
    [
        lambda s: repositories.mark_processing(s, PID, DID),
        lambda s: repositories.mark_parsed(s, PID, DID, "# md", "pymupdf"),
        lambda s: repositories.mark_failed(s, PID, DID, "pdf_parse_failed"),
        lambda s: repositories.mark_summarizing(s, PID, DID),
        lambda s: repositories.save_summarization(s, PID, DID, "text", "m"),
        lambda s: repositories.mark_summarization_failed(s, PID, DID, "tool_failed"),
    ],
)
async def test_updates_return_the_row_without_the_large_columns(call: Any) -> None:
    session = CapturingSession()

    assert await call(session) is True

    returning = selected_columns(session.sql())
    assert {"status", "updated_at"} <= returning
    assert not LARGE & returning


def test_every_engine_hides_bound_parameters_from_error_messages() -> None:
    """R-code LOW 6: ``api.request.failed`` logs ``str(exc)``; it must not carry values."""

    database = DatabaseSettings(host="invalid.invalid", port=5432, name="d", user="u", password=PLACEHOLDER_CREDENTIAL)
    app_engine = build_engine(database)
    assert app_engine.sync_engine.hide_parameters is True

    leaked_value = "alice@example.org"
    error = ProgrammingError(
        "INSERT INTO users (email) VALUES ($1)",
        {"email": leaked_value},
        Exception("syntax"),
        hide_parameters=app_engine.sync_engine.hide_parameters,
    )
    assert leaked_value not in str(error)
    assert "parameters hidden" in str(error)

    factory = build_agent_session_factory(
        database, AgentDatabaseSettings(DATABASE_AGENT_USER="agent", DATABASE_AGENT_PASSWORD=PLACEHOLDER_CREDENTIAL)
    )
    assert factory.kw["bind"].sync_engine.hide_parameters is True
