from __future__ import annotations

import ast
import configparser
import re
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL

from settings import DatabaseConfigurationError, DatabaseSettings

# These tests never open a connection: Alembic runs in offline (`--sql`) mode
# and writes the SQL into an in-memory buffer. The URL comes from
# `cfg.attributes["database_url"]`, the hook migrations/env.py offers to
# programmatic callers. The tests stay synchronous because env.py calls
# `asyncio.run` for online runs.

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
ENV_PY = BACKEND_DIR / "migrations" / "env.py"

# Written out literally, not read from settings, so the tests clean up
# (and expect) the same keys whatever the code under test says.
DATABASE_KEYS = (
    "DATABASE_HOST",
    "DATABASE_PORT",
    "DATABASE_NAME",
    "DATABASE_USER",
    "DATABASE_PASSWORD",
)
DB_CRED = "dummy-pw-9f3"
DUMMY_URL = DatabaseSettings(host="offline.invalid", port=5432, name="dummy", user="dummy", password=DB_CRED).url()
DUMMY_URL_STRING = DUMMY_URL.render_as_string(hide_password=False)


AGENT_KEYS = ("DATABASE_AGENT_USER", "DATABASE_AGENT_PASSWORD")
AGENT_CRED = "agent-canary-pw-51d"
SKIP_COMMENT = "-- agent role existence check skipped (offline)"
AGENT_GRANT = "GRANT SELECT (id, project_id, original_filename, version, status, markdown) ON documents TO {role}"
HEAD = "0001_initial"
APP_TABLES = ("users", "projects", "project_members", "connections", "documents", "connection_items")
SCOPED_TABLES = (
    ("projects", "id"),
    ("project_members", "project_id"),
    ("documents", "project_id"),
    ("connections", "project_id"),
    ("connection_items", "project_id"),
)


@pytest.fixture(autouse=True)
def _clean_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DATABASE_KEYS + AGENT_KEYS:
        monkeypatch.delenv(name, raising=False)


def offline_config(url: str | URL | None = DUMMY_URL) -> tuple[Config, StringIO]:
    buffer = StringIO()
    cfg = Config(str(ALEMBIC_INI), output_buffer=buffer)
    if url is not None:
        cfg.attributes["database_url"] = url
    return cfg, buffer


# T8 ------------------------------------------------------------------------


def test_the_migration_history_is_the_single_initial_revision() -> None:
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))

    assert script.get_heads() == [HEAD]
    assert script.get_bases() == [HEAD]
    assert [revision.revision for revision in script.walk_revisions()] == [HEAD]
    assert script.get_revision(HEAD).down_revision is None


# T9 ------------------------------------------------------------------------


@pytest.mark.parametrize("url", [DUMMY_URL, DUMMY_URL_STRING], ids=["URL", "str"])
def test_the_upgrade_creates_the_schema_and_stamps_the_initial_revision(
    url: str | URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_AGENT_PASSWORD", AGENT_CRED)
    cfg, buffer = offline_config(url)

    command.upgrade(cfg, "head", sql=True)

    sql = buffer.getvalue()
    assert "CREATE TABLE alembic_version" in sql
    assert "INSERT INTO alembic_version (version_num) VALUES ('0001_initial')" in sql
    assert "UPDATE alembic_version" not in sql  # one revision, no chain
    assert re.findall(r"CREATE TABLE (\w+) \(", sql) == ["alembic_version", *APP_TABLES]
    assert "CONSTRAINT uq_documents_project_id_content_hash UNIQUE (project_id, content_hash)" in sql
    assert "EXTENSION" not in sql
    assert DB_CRED not in sql
    assert AGENT_CRED not in sql
    assert "PASSWORD" not in sql.upper().replace("DATABASE_AGENT_PASSWORD", "")


def test_the_downgrade_clears_the_version() -> None:
    cfg, buffer = offline_config()

    command.downgrade(cfg, f"{HEAD}:base", sql=True)

    sql = buffer.getvalue()
    assert "DELETE FROM alembic_version" in sql
    assert "version_num = '0001_initial'" in sql
    assert "EXTENSION" not in sql
    assert DB_CRED not in sql


# T10 -----------------------------------------------------------------------


def test_alembic_ini_holds_no_url_and_no_logging_configuration() -> None:
    # `%(here)s` breaks the default interpolation, so switch it off.
    parser = configparser.ConfigParser(interpolation=None)
    assert parser.read(ALEMBIC_INI) == [str(ALEMBIC_INI)]

    assert not parser.has_option("alembic", "sqlalchemy.url")
    assert Config(str(ALEMBIC_INI)).get_main_option("sqlalchemy.url") is None
    for section in parser.sections():
        assert section not in {"loggers", "handlers", "formatters"}
        assert not section.startswith(("logger_", "handler_", "formatter_"))


def test_alembic_ini_points_at_the_migrations_next_to_it() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ALEMBIC_INI)

    assert parser.get("alembic", "script_location") == "%(here)s/migrations"
    assert parser.get("alembic", "prepend_sys_path") == "."
    assert parser.get("alembic", "path_separator") == "os"


def test_env_py_never_configures_stdlib_logging() -> None:
    # An AST walk, not a text search: env.py's comments mention logging.
    tree = ast.parse(ENV_PY.read_text(encoding="utf-8"), filename=str(ENV_PY))

    imported_modules: list[str] = []
    imported_names: list[str] = []
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported_modules.append(node.module or "")
            imported_names += [alias.name for alias in node.names]
        elif isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)

    assert imported_modules  # the walk saw env.py's imports at all
    assert not [m for m in imported_modules if m.split(".")[0] == "logging"]
    assert "fileConfig" not in imported_names
    assert "fileConfig" not in referenced
    assert "logging" not in referenced


def test_env_py_without_a_url_and_without_database_settings_fails_clearly() -> None:
    cfg, buffer = offline_config(url=None)

    with pytest.raises(DatabaseConfigurationError) as caught:
        command.upgrade(cfg, "head", sql=True)

    message = str(caught.value)
    for name in DATABASE_KEYS:
        if name != "DATABASE_PORT":  # the port has a default
            assert name in message
    assert buffer.getvalue() == ""


def test_env_py_without_a_url_reads_the_database_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_HOST", "offline.invalid")
    monkeypatch.setenv("DATABASE_PORT", "5432")
    monkeypatch.setenv("DATABASE_NAME", "dummy")
    monkeypatch.setenv("DATABASE_USER", "dummy")
    monkeypatch.setenv("DATABASE_PASSWORD", DB_CRED)
    cfg, buffer = offline_config(url=None)

    command.upgrade(cfg, "head", sql=True)

    sql = buffer.getvalue()
    assert "INSERT INTO alembic_version (version_num) VALUES ('0001_initial')" in sql
    assert DB_CRED not in sql


# Agent access offline (AC-3.2, H1) -------------------------------------------


def upgrade_sql(cfg: Config, buffer: StringIO) -> str:
    command.upgrade(cfg, HEAD, sql=True)
    return buffer.getvalue()


def test_offline_upgrade_skips_the_role_check_with_a_comment() -> None:
    sql = upgrade_sql(*offline_config())

    assert SKIP_COMMENT in sql
    assert sql.index(SKIP_COMMENT) < sql.index("CREATE TABLE users")  # checked before any DDL
    assert "pg_roles" not in sql


def test_offline_upgrade_enables_rls_and_creates_the_policy_on_each_scoped_table() -> None:
    sql = upgrade_sql(*offline_config())

    scope = "NULLIF(current_setting('app.project_id', true), '')::uuid"
    for table, column in SCOPED_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert (
            f'CREATE POLICY agent_project_scope ON {table} TO "specflow_agent" '
            f"USING ({column} = {scope}) WITH CHECK ({column} = {scope})"
        ) in sql
        # RLS and its policy come after the table exists
        assert sql.index(f"CREATE TABLE {table} (") < sql.index(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    assert sql.count("CREATE POLICY agent_project_scope") == len(SCOPED_TABLES)
    assert sql.count("ENABLE ROW LEVEL SECURITY") == len(SCOPED_TABLES)
    assert "users ENABLE ROW LEVEL SECURITY" not in sql  # users is not project-scoped


def test_offline_upgrade_grants_only_the_document_columns() -> None:
    sql = upgrade_sql(*offline_config())

    grants = [line for line in sql.splitlines() if "GRANT" in line.upper()]
    assert grants == [AGENT_GRANT.format(role='"specflow_agent"') + ";"]
    assert "summarization" not in grants[0]


def test_offline_upgrade_takes_the_role_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_AGENT_USER", "env_agent")
    monkeypatch.setenv("DATABASE_AGENT_PASSWORD", AGENT_CRED)

    sql = upgrade_sql(*offline_config())

    assert AGENT_GRANT.format(role='"env_agent"') in sql
    assert AGENT_CRED not in sql


def test_offline_upgrade_prefers_the_config_attribute_and_quotes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_AGENT_USER", "env_agent")
    cfg, buffer = offline_config()
    cfg.attributes["agent_user"] = 'Odd"Agent'

    sql = upgrade_sql(cfg, buffer)

    assert AGENT_GRANT.format(role='"Odd""Agent"') in sql
    assert "env_agent" not in sql


def test_offline_downgrade_to_base_drops_every_table_newest_first() -> None:
    """Dropping a table drops its indexes, constraints, policy and grant with it."""

    cfg, buffer = offline_config()

    command.downgrade(cfg, f"{HEAD}:base", sql=True)

    sql = buffer.getvalue()
    assert re.findall(r"DROP TABLE (\w+)", sql) == list(reversed(APP_TABLES))
    assert "DELETE FROM alembic_version" in sql
    assert SKIP_COMMENT not in sql  # the downgrade needs no agent role
