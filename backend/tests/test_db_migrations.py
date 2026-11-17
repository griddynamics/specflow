from __future__ import annotations

import ast
import configparser
import hashlib
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
DUMMY_URL = DatabaseSettings(
    host="offline.invalid",
    port=5432,
    name="dummy",
    user="dummy",
    password=DB_CRED,
    PGSSLMODE="",
    PGSSLROOTCERT="",
).url()
DUMMY_URL_STRING = DUMMY_URL.render_as_string(hide_password=False)


AGENT_KEYS = ("DATABASE_AGENT_USER", "DATABASE_AGENT_PASSWORD")
AGENT_CRED = "agent-canary-pw-51d"
SKIP_COMMENT = "-- agent role existence check skipped (offline)"
AGENT_GRANT = "GRANT SELECT (id, project_id, original_filename, version, status, markdown) ON documents TO {role}"
FIRST = "0001_initial"  # frozen: the 2.0.0 schema
HEAD = "0002_project_collaborators"
# SHA-256 of versions/0001_initial.py as 2.0.0 shipped it; a mismatch means a frozen revision was edited.
FROZEN_0001_SHA256 = "ebc6d2e0f6ca8eef9bcbc5acde990ed151fe17fbc1945c75e5d6289a455948dc"
INITIAL_TABLES = (
    "users",
    "projects",
    "project_members",
    "connections",
    "documents",
    "connection_items",
)
APP_TABLES = (*INITIAL_TABLES, "project_invites")
SCOPED_TABLES = (
    ("projects", "id"),
    ("project_members", "project_id"),
    ("documents", "project_id"),
    ("connections", "project_id"),
    ("connection_items", "project_id"),
)
INVITES = ("project_invites", "project_id")


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


def test_the_migration_history_is_the_initial_revision_and_the_collaborators_revision() -> None:
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))

    assert script.get_heads() == [HEAD]
    assert script.get_bases() == [FIRST]
    assert [revision.revision for revision in script.walk_revisions()] == [HEAD, FIRST]
    assert script.get_revision(FIRST).down_revision is None
    assert script.get_revision(HEAD).down_revision == FIRST


# T9 ------------------------------------------------------------------------


@pytest.mark.parametrize("url", [DUMMY_URL, DUMMY_URL_STRING], ids=["URL", "str"])
def test_the_upgrade_creates_the_schema_and_stamps_the_head_revision(
    url: str | URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_AGENT_PASSWORD", AGENT_CRED)
    cfg, buffer = offline_config(url)

    command.upgrade(cfg, "head", sql=True)

    sql = buffer.getvalue()
    assert "CREATE TABLE alembic_version" in sql
    assert "INSERT INTO alembic_version (version_num) VALUES ('0001_initial')" in sql
    assert "UPDATE alembic_version SET version_num='0002_project_collaborators'" in sql
    assert re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+) \(", sql) == [
        "alembic_version",
        *APP_TABLES,
    ]
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
    assert "UPDATE alembic_version SET version_num='0002_project_collaborators'" in sql
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
    for table, column in (*SCOPED_TABLES, INVITES):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert (
            f'CREATE POLICY agent_project_scope ON {table} TO "specflow_agent" '
            f"USING ({column} = {scope}) WITH CHECK ({column} = {scope})"
        ).lower() in sql.lower()  # revision 0002 spells the functions in its own letter case
        # RLS and its policy come after the table exists
        assert re.search(rf"CREATE TABLE (IF NOT EXISTS )?{table} \(", sql)
        assert re.search(rf"CREATE TABLE (IF NOT EXISTS )?{table} \(", sql).start() < sql.index(
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"
        )
    assert sql.count("CREATE POLICY agent_project_scope") == len(SCOPED_TABLES) + 1
    assert sql.count("ENABLE ROW LEVEL SECURITY") == len(SCOPED_TABLES) + 1
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
    assert re.findall(r"DROP TABLE (?:IF EXISTS )?(\w+)", sql) == list(reversed(APP_TABLES))
    assert "DELETE FROM alembic_version" in sql
    assert SKIP_COMMENT not in sql  # the downgrade needs no agent role


# The collaborator schema, added by revision 0002 on top of the frozen 0001_initial ---------------


def collaborators_sql() -> str:
    """The SQL of revision 0002 alone."""

    cfg, buffer = offline_config()
    command.upgrade(cfg, f"{FIRST}:{HEAD}", sql=True)
    return buffer.getvalue()


def test_revision_0001_keeps_the_2_0_0_schema() -> None:
    cfg, buffer = offline_config()

    command.upgrade(cfg, FIRST, sql=True)

    sql = buffer.getvalue()
    assert re.findall(r"CREATE TABLE (\w+) \(", sql) == [
        "alembic_version",
        *INITIAL_TABLES,
    ]
    assert "CONSTRAINT ck_project_members_role CHECK (role IN ('owner'))" in sql
    assert "role TEXT DEFAULT 'owner' NOT NULL" in sql
    assert "project_invites" not in sql
    assert "ux_project_members_one_owner" not in sql
    assert "ix_users_email_lower" not in sql


def test_the_members_role_check_default_and_one_owner_index_are_changed_by_0002() -> None:
    sql = collaborators_sql()

    assert "ALTER TABLE project_members DROP CONSTRAINT IF EXISTS ck_project_members_role;" in sql
    assert (
        "ALTER TABLE project_members ADD CONSTRAINT ck_project_members_role CHECK (role IN ('owner', 'collaborator'));"
    ) in sql
    assert sql.index("DROP CONSTRAINT IF EXISTS ck_project_members_role") < sql.index(
        "ADD CONSTRAINT ck_project_members_role"
    )
    assert "ALTER TABLE project_members ALTER COLUMN role DROP DEFAULT;" in sql
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_project_members_one_owner ON project_members (project_id) "
        "WHERE role = 'owner';"
    ) in sql
    assert "admin" not in sql.lower()


def test_the_invites_table_and_the_email_index_are_created_only_when_missing() -> None:
    sql = collaborators_sql()

    assert "CREATE TABLE IF NOT EXISTS project_invites (" in sql
    assert "CONSTRAINT uq_project_invites_project_id_email_norm UNIQUE (project_id, email_norm)" in sql
    assert "CONSTRAINT ck_project_invites_email_norm CHECK (email_norm = LOWER(BTRIM(email_norm)))" in sql
    assert "CONSTRAINT fk_project_invites_project_id_projects FOREIGN KEY (project_id) REFERENCES projects (id)" in sql
    assert "CONSTRAINT fk_project_invites_invited_by_users FOREIGN KEY (invited_by) REFERENCES users (id)" in sql
    assert "CREATE INDEX IF NOT EXISTS ix_project_invites_email_norm ON project_invites (email_norm);" in sql
    assert "CREATE INDEX IF NOT EXISTS ix_users_email_lower ON users (lower(email));" in sql
    assert "TRIGGER" not in sql.upper()  # no legacy trigger, no data rewrite
    assert "DELETE" not in sql.upper()  # the upgrade removes nothing


def test_the_upgrade_demotes_extra_owners_after_the_check_and_before_the_one_owner_index() -> None:
    sql = collaborators_sql()

    assert sql.count("UPDATE project_members SET role='collaborator'") == 1  # the only data step
    update = sql.index("UPDATE project_members SET role='collaborator'")
    assert sql.index("ADD CONSTRAINT ck_project_members_role") < update < sql.index("CREATE UNIQUE INDEX")
    statement = sql[update : sql.index(";", update)]
    # creator first, then the earliest joined_at, then the smallest user_id; the first one stays owner
    assert "ORDER BY project_members.user_id = projects.created_by DESC, project_members.joined_at, " in statement
    assert "project_members.user_id) AS rank" in statement
    assert "WHERE owners.rank > 1" in statement
    assert "WHERE project_members.role = 'owner' AND" in statement


def test_the_first_online_statement_sets_a_lock_timeout_and_offline_output_skips_it() -> None:
    sql = collaborators_sql()

    assert "-- lock_timeout skipped (offline)" in sql
    assert "lock_timeout = '10s'" not in sql  # the session setting is online only
    assert sql.index("-- lock_timeout skipped (offline)") < sql.index("ALTER TABLE project_members DROP CONSTRAINT")
    text = (ALEMBIC_INI.parent / "migrations" / "sql" / "0002_lock_timeout.sql").read_text("utf-8")
    assert text.strip() == "SET LOCAL lock_timeout = '10s'"
    doc = (ALEMBIC_INI.parent / "migrations" / "versions" / "0002_project_collaborators.py").read_text("utf-8")
    assert "WHOLE transaction" in doc


def test_0002_enables_rls_on_the_invites_and_re_creates_the_policy_without_a_grant() -> None:
    sql = collaborators_sql()

    assert "ALTER TABLE project_invites ENABLE ROW LEVEL SECURITY;" in sql
    assert sql.index("DROP POLICY IF EXISTS agent_project_scope ON project_invites;") < sql.index(
        'CREATE POLICY agent_project_scope ON project_invites TO "specflow_agent"'
    )
    assert "GRANT" not in sql.upper()
    assert SKIP_COMMENT in sql


def test_the_downgrade_of_0002_returns_to_the_2_0_0_schema_and_says_what_is_lost() -> None:
    cfg, buffer = offline_config()

    command.downgrade(cfg, f"{HEAD}:{FIRST}", sql=True)

    sql = buffer.getvalue()
    assert "DELETE FROM project_members WHERE project_members.role = 'collaborator';" in sql
    assert "DROP TABLE IF EXISTS project_invites;" in sql
    assert "DROP INDEX IF EXISTS ux_project_members_one_owner;" in sql
    assert "DROP INDEX IF EXISTS ix_users_email_lower;" in sql
    assert "ADD CONSTRAINT ck_project_members_role CHECK (role IN ('owner'));" in sql
    assert "ALTER TABLE project_members ALTER COLUMN role SET DEFAULT 'owner';" in sql
    assert "UPDATE alembic_version SET version_num='0001_initial'" in sql
    assert sql.index("DELETE FROM project_members") < sql.index("ADD CONSTRAINT ck_project_members_role")
    doc = (ALEMBIC_INI.parent / "migrations" / "versions" / "0002_project_collaborators.py").read_text("utf-8")
    assert "DELETES every ``collaborator`` row" in doc


def test_revision_0001_is_frozen() -> None:
    """0001_initial is the 2.0.0 schema: a schema change is a new revision, never an edit of it."""

    path = ALEMBIC_INI.parent / "migrations" / "versions" / "0001_initial.py"
    unix_text = path.read_bytes().replace(b"\r\n", b"\n")  # a CRLF checkout must not fail the guard
    assert hashlib.sha256(unix_text).hexdigest() == FROZEN_0001_SHA256
