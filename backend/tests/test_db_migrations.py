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
COLLABORATORS = "0002_project_collaborators"
GAP_MODULE = "0003_gap_module"
HEAD_0004 = "0004_gap_assignment"
HEAD_0005 = "0005_jira_sync"
HEAD_0006 = "0006_atlassian_connections"
HEAD_0007 = "0007_github"
HEAD_0008 = "0008_project_integrations"
HEAD_0009 = "0009_jira_gaps"
HEAD_0010 = "0010_github_projects"
HEAD = "0011_notion"
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
# The relational rows of the gap module (docs/graph/gap/data-model.md "Relational store"), added by 0003.
GAP_TABLES = (
    "gap_option",
    "corpus_amendment",
    "gap_run",
    "gap_seed",
    "gap_site",
    "gap_verdict",
    "gap_section_relevance",
    "gap_noise_rule",
    "gap_suppression",
    "gap_note",
    "gap_event",
)
# The member who resolves a gap, added by 0004.
ASSIGNMENT = "gap_assignment"
# The Jira target and the node-to-issue map, added by 0005.
JIRA_TABLES = ("jira_targets", "jira_issues")
# The users' Atlassian connections, added by 0006 (row-level security with no policy: not project-scoped).
CONNECTIONS = "atlassian_connections"
# The users' GitHub connections (no policy) and the project repositories (project-scoped), added by 0007.
GITHUB_CONNECTIONS = "github_connections"
REPOSITORIES = "project_repositories"
# The member and workspace of each project's apps (project-scoped), added by 0008.
INTEGRATIONS = "project_integrations"
# The Jira gap channel's target and gap-to-sub-task map (project-scoped), added by 0009.
JIRA_GAP_TABLES = ("jira_gap_targets", "jira_gap_issues")
# The GitHub Projects destination's target and plan-to-issue map (project-scoped), added by 0010.
GITHUB_PROJECT_TABLES = ("github_project_targets", "github_project_items")
# The users' Notion connections (no policy) and the Notion plan destination's target and map (project-scoped), added by
# 0011.
NOTION_CONNECTIONS = "notion_connections"
NOTION_PLAN_TABLES = ("notion_plan_targets", "notion_plan_items")
APP_TABLES = (
    *INITIAL_TABLES,
    "project_invites",
    *GAP_TABLES,
    ASSIGNMENT,
    *JIRA_TABLES,
    CONNECTIONS,
    GITHUB_CONNECTIONS,
    REPOSITORIES,
    INTEGRATIONS,
    *JIRA_GAP_TABLES,
    *GITHUB_PROJECT_TABLES,
    NOTION_CONNECTIONS,
    *NOTION_PLAN_TABLES,
)
SCOPED_TABLES = (
    ("projects", "id"),
    ("project_members", "project_id"),
    ("documents", "project_id"),
    ("connections", "project_id"),
    ("connection_items", "project_id"),
    *(
        (table, "project_id")
        for table in (
            *GAP_TABLES,
            ASSIGNMENT,
            *JIRA_TABLES,
            REPOSITORIES,
            INTEGRATIONS,
            *JIRA_GAP_TABLES,
            *GITHUB_PROJECT_TABLES,
            *NOTION_PLAN_TABLES,
        )
    ),
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


def test_the_migration_history_is_the_initial_the_collaborators_the_gap_and_the_assignment_revision() -> None:
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))

    assert script.get_heads() == [HEAD]
    assert script.get_bases() == [FIRST]
    assert [revision.revision for revision in script.walk_revisions()] == [
        HEAD,
        HEAD_0010,
        HEAD_0009,
        HEAD_0008,
        HEAD_0007,
        HEAD_0006,
        HEAD_0005,
        HEAD_0004,
        GAP_MODULE,
        COLLABORATORS,
        FIRST,
    ]
    assert script.get_revision(FIRST).down_revision is None
    assert script.get_revision(COLLABORATORS).down_revision == FIRST
    assert script.get_revision(GAP_MODULE).down_revision == COLLABORATORS
    assert script.get_revision(HEAD_0004).down_revision == GAP_MODULE
    assert script.get_revision(HEAD_0005).down_revision == HEAD_0004
    assert script.get_revision(HEAD_0006).down_revision == HEAD_0005
    assert script.get_revision(HEAD_0007).down_revision == HEAD_0006
    assert script.get_revision(HEAD_0008).down_revision == HEAD_0007
    assert script.get_revision(HEAD_0009).down_revision == HEAD_0008
    assert script.get_revision(HEAD_0010).down_revision == HEAD_0009
    assert script.get_revision(HEAD).down_revision == HEAD_0010


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
    assert "UPDATE alembic_version SET version_num='0003_gap_module'" in sql
    assert "UPDATE alembic_version SET version_num='0004_gap_assignment'" in sql
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
    assert "UPDATE alembic_version SET version_num='0003_gap_module'" in sql
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
    assert sql.count("ENABLE ROW LEVEL SECURITY") == len(SCOPED_TABLES) + 4  # + invites, 3 connection tables
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
    command.upgrade(cfg, f"{FIRST}:{COLLABORATORS}", sql=True)
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

    command.downgrade(cfg, f"{COLLABORATORS}:{FIRST}", sql=True)

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


# The gap module's tables, added by revision 0003 on top of 0002 ---------------------------------


def gap_sql() -> str:
    """The SQL of revision 0003 alone."""

    cfg, buffer = offline_config()
    command.upgrade(cfg, f"{COLLABORATORS}:{GAP_MODULE}", sql=True)
    return buffer.getvalue()


def test_0003_creates_only_the_gap_tables_and_stamps_its_revision() -> None:
    sql = gap_sql()

    assert re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+) \(", sql) == list(GAP_TABLES)
    for table in GAP_TABLES:
        assert f"CONSTRAINT fk_{table}_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id)" in sql
    index = "CREATE UNIQUE INDEX ix_gap_option_one_recommended ON gap_option (project_id, gap_id) WHERE recommended"
    assert index in sql
    assert "UPDATE alembic_version SET version_num='0003_gap_module'" in sql
    # no existing table changes: the only ALTER TABLE statements enable row-level security on a gap table
    assert sorted(re.findall(r"ALTER TABLE (\w+) ENABLE ROW LEVEL SECURITY;", sql)) == sorted(GAP_TABLES)
    assert sql.count("ALTER TABLE") == len(GAP_TABLES)
    assert "DROP" not in sql.upper()
    assert "DELETE" not in sql.upper()


def test_0003_checks_the_role_and_skips_the_lock_timeout_offline_before_any_ddl() -> None:
    sql = gap_sql()

    first_table = sql.index("CREATE TABLE gap_option")
    assert sql.index("-- lock_timeout skipped (offline)") < first_table
    assert sql.index(SKIP_COMMENT) < first_table
    assert "lock_timeout = '10s'" not in sql  # the session setting is online only
    text = (ALEMBIC_INI.parent / "migrations" / "sql" / "0003_lock_timeout.sql").read_text("utf-8")
    assert text.strip() == "SET LOCAL lock_timeout = '10s'"


def test_0003_scopes_every_gap_table_to_the_agent_role_without_a_grant() -> None:
    sql = gap_sql()

    scope = "nullif(current_setting('app.project_id', true), '')::uuid"
    for table in GAP_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in sql
        assert (
            f'CREATE POLICY agent_project_scope ON {table} TO "specflow_agent" '
            f"USING (project_id = {scope}) WITH CHECK (project_id = {scope});"
        ) in sql
        assert sql.index(f"CREATE TABLE {table} (") < sql.index(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    assert sql.count("CREATE POLICY agent_project_scope") == len(GAP_TABLES)
    assert "GRANT" not in sql.upper()


def test_the_downgrade_of_0003_drops_only_the_gap_tables_and_says_what_is_lost() -> None:
    cfg, buffer = offline_config()

    command.downgrade(cfg, f"{GAP_MODULE}:{COLLABORATORS}", sql=True)

    sql = buffer.getvalue()
    assert re.findall(r"DROP TABLE (?:IF EXISTS )?(\w+)", sql) == list(reversed(GAP_TABLES))
    assert "UPDATE alembic_version SET version_num='0002_project_collaborators'" in sql
    assert sql.index("-- lock_timeout skipped (offline)") < sql.index("DROP TABLE")
    assert SKIP_COMMENT not in sql  # the downgrade needs no agent role
    doc = (ALEMBIC_INI.parent / "migrations" / "versions" / "0003_gap_module.py").read_text("utf-8")
    assert "Every gap row is lost" in doc


# The gap assignment table, added by revision 0004 on top of 0003 -------------------------------


def assignment_sql() -> str:
    """The SQL of revision 0004 alone."""

    cfg, buffer = offline_config()
    command.upgrade(cfg, f"{GAP_MODULE}:{HEAD_0004}", sql=True)
    return buffer.getvalue()


def test_0004_creates_only_the_assignment_table_keyed_to_the_member_row() -> None:
    sql = assignment_sql()

    assert re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+) \(", sql) == [ASSIGNMENT]
    assert "CONSTRAINT pk_gap_assignment PRIMARY KEY (project_id, gap_id)" in sql
    assert "CONSTRAINT fk_gap_assignment_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id)" in sql
    assert (
        "CONSTRAINT fk_gap_assignment_project_id_project_members FOREIGN KEY(project_id, user_id) "
        "REFERENCES project_members (project_id, user_id) ON DELETE CASCADE"
    ) in sql
    assert "CREATE INDEX ix_gap_assignment_user_id ON gap_assignment (project_id, user_id);" in sql
    assert "UPDATE alembic_version SET version_num='0004_gap_assignment'" in sql
    assert sql.count("ALTER TABLE") == 1  # row-level security on the new table; no existing table changes
    assert "DROP" not in sql.upper()
    assert "DELETE FROM" not in sql.upper()


def test_0004_checks_the_role_and_scopes_the_table_to_the_agent_role_without_a_grant() -> None:
    sql = assignment_sql()

    scope = "nullif(current_setting('app.project_id', true), '')::uuid"
    created = sql.index("CREATE TABLE gap_assignment")
    assert sql.index("-- lock_timeout skipped (offline)") < created
    assert sql.index(SKIP_COMMENT) < created
    assert created < sql.index("ALTER TABLE gap_assignment ENABLE ROW LEVEL SECURITY;")
    assert (
        'CREATE POLICY agent_project_scope ON gap_assignment TO "specflow_agent" '
        f"USING (project_id = {scope}) WITH CHECK (project_id = {scope});"
    ) in sql
    assert "GRANT" not in sql.upper()


def test_the_downgrade_of_0004_drops_only_the_assignment_table_and_says_what_is_lost() -> None:
    cfg, buffer = offline_config()

    command.downgrade(cfg, f"{HEAD_0004}:{GAP_MODULE}", sql=True)

    sql = buffer.getvalue()
    assert re.findall(r"DROP TABLE (?:IF EXISTS )?(\w+)", sql) == [ASSIGNMENT]
    assert "UPDATE alembic_version SET version_num='0003_gap_module'" in sql
    assert sql.index("-- lock_timeout skipped (offline)") < sql.index("DROP TABLE")
    assert SKIP_COMMENT not in sql  # the downgrade needs no agent role
    doc = (ALEMBIC_INI.parent / "migrations" / "versions" / "0004_gap_assignment.py").read_text("utf-8")
    assert "Every assignment is lost" in doc


# The Jira sync tables, added by revision 0005 on top of 0004 -------------------------------


def jira_sql() -> str:
    """The SQL of revision 0005 alone."""

    cfg, buffer = offline_config()
    command.upgrade(cfg, f"{HEAD_0004}:{HEAD_0005}", sql=True)
    return buffer.getvalue()


def test_0005_creates_only_the_two_jira_tables_with_named_constraints_and_the_partial_key_index() -> None:
    sql = jira_sql()

    assert re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+) \(", sql) == list(JIRA_TABLES)
    assert "CONSTRAINT pk_jira_targets PRIMARY KEY (project_id)" in sql
    assert "CONSTRAINT pk_jira_issues PRIMARY KEY (project_id, node_id)" in sql
    assert "CONSTRAINT fk_jira_targets_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id)" in sql
    assert "CONSTRAINT fk_jira_issues_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id)" in sql
    assert "CONSTRAINT ck_jira_targets_last_status CHECK (last_status IN ('running', 'succeeded'" in sql
    assert "CONSTRAINT ck_jira_targets_last_trigger CHECK (last_trigger IN ('plan_run', 'mcp'" in sql
    assert "CONSTRAINT ck_jira_issues_kind CHECK (kind IN ('epic', 'story', 'subtask'))" in sql
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_jira_issues_jira_key ON jira_issues (project_id, jira_key) "
        "WHERE jira_key IS NOT NULL;"
    ) in sql
    assert "CASCADE" not in sql.upper()
    assert "UPDATE alembic_version SET version_num='0005_jira_sync'" in sql
    assert sql.count("ALTER TABLE") == 2  # row-level security on the new tables; no existing table changes
    assert "DROP" not in sql.upper()


def test_0005_checks_the_role_and_scopes_both_tables_to_the_agent_role_without_a_grant() -> None:
    sql = jira_sql()

    scope = "nullif(current_setting('app.project_id', true), '')::uuid"
    assert sql.index("-- lock_timeout skipped (offline)") < sql.index("CREATE TABLE jira_targets")
    assert sql.index(SKIP_COMMENT) < sql.index("CREATE TABLE jira_targets")
    for table in JIRA_TABLES:
        assert sql.index(f"CREATE TABLE {table}") < sql.index(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        assert (
            f'CREATE POLICY agent_project_scope ON {table} TO "specflow_agent" '
            f"USING (project_id = {scope}) WITH CHECK (project_id = {scope});"
        ) in sql
    assert "GRANT" not in sql.upper()


def test_the_downgrade_of_0005_drops_only_the_jira_tables_and_says_what_is_lost() -> None:
    cfg, buffer = offline_config()

    command.downgrade(cfg, f"{HEAD_0005}:{HEAD_0004}", sql=True)

    sql = buffer.getvalue()
    assert re.findall(r"DROP TABLE (?:IF EXISTS )?(\w+)", sql) == list(reversed(JIRA_TABLES))
    assert "UPDATE alembic_version SET version_num='0004_gap_assignment'" in sql
    assert sql.index("-- lock_timeout skipped (offline)") < sql.index("DROP TABLE")
    assert SKIP_COMMENT not in sql  # the downgrade needs no agent role
    doc = (ALEMBIC_INI.parent / "migrations" / "versions" / "0005_jira_sync.py").read_text("utf-8")
    assert "whole node-to-issue map is lost" in doc


# The Atlassian connections, added by revision 0006 on top of 0005 -------------------------------


def connections_sql() -> str:
    """The SQL of revision 0006 alone."""

    cfg, buffer = offline_config()
    command.upgrade(cfg, f"{HEAD_0005}:{HEAD_0006}", sql=True)
    return buffer.getvalue()


def test_0006_creates_the_connections_table_and_the_acting_account_column_with_their_keys() -> None:
    sql = connections_sql()

    assert re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+) \(", sql) == [CONNECTIONS]
    assert "CONSTRAINT pk_atlassian_connections PRIMARY KEY (user_id)" in sql
    assert "CONSTRAINT ck_atlassian_connections_status CHECK (status IN ('active', 'expired'))" in sql
    assert (
        "CONSTRAINT fk_atlassian_connections_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE"
    ) in sql
    assert "refresh_token BYTEA NOT NULL" in sql
    assert "ALTER TABLE jira_targets ADD COLUMN connected_user_id UUID;" in sql
    assert (
        "ALTER TABLE jira_targets ADD CONSTRAINT fk_jira_targets_connected_user_id_users "
        "FOREIGN KEY(connected_user_id) REFERENCES users (id) ON DELETE SET NULL;"
    ) in sql
    assert "UPDATE alembic_version SET version_num='0006_atlassian_connections'" in sql
    assert "DROP" not in sql.upper()


def test_0006_enables_row_level_security_on_the_connections_with_no_policy_and_no_grant() -> None:
    sql = connections_sql()

    assert sql.index("-- lock_timeout skipped (offline)") < sql.index(f"CREATE TABLE {CONNECTIONS}")
    assert sql.index(f"CREATE TABLE {CONNECTIONS}") < sql.index(f"ALTER TABLE {CONNECTIONS} ENABLE ROW LEVEL SECURITY;")
    assert "POLICY" not in sql.upper()
    assert "GRANT" not in sql.upper()
    assert SKIP_COMMENT not in sql  # no policy names the agent role


def test_the_downgrade_of_0006_drops_the_column_and_the_table_and_says_what_is_lost() -> None:
    cfg, buffer = offline_config()

    command.downgrade(cfg, f"{HEAD_0006}:{HEAD_0005}", sql=True)

    sql = buffer.getvalue()
    assert re.findall(r"DROP TABLE (?:IF EXISTS )?(\w+)", sql) == [CONNECTIONS]
    assert "ALTER TABLE jira_targets DROP COLUMN connected_user_id;" in sql
    assert sql.index("DROP CONSTRAINT fk_jira_targets_connected_user_id_users") < sql.index("DROP COLUMN")
    assert "UPDATE alembic_version SET version_num='0005_jira_sync'" in sql
    assert sql.index("-- lock_timeout skipped (offline)") < sql.index("ALTER TABLE")
    doc = (ALEMBIC_INI.parent / "migrations" / "versions" / "0006_atlassian_connections.py").read_text("utf-8")
    assert "Every Atlassian connection is lost" in doc


# The GitHub tables, added by revision 0007 on top of 0006 -------------------------------


def github_sql() -> str:
    """The SQL of revision 0007 alone."""

    cfg, buffer = offline_config()
    command.upgrade(cfg, f"{HEAD_0006}:{HEAD_0007}", sql=True)
    return buffer.getvalue()


def test_0007_creates_the_github_tables_with_their_keys_and_checks() -> None:
    sql = github_sql()

    assert re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+) \(", sql) == [GITHUB_CONNECTIONS, REPOSITORIES]
    assert "CONSTRAINT pk_github_connections PRIMARY KEY (user_id)" in sql
    assert (
        "CONSTRAINT fk_github_connections_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE"
    ) in sql
    assert "refresh_token BYTEA NOT NULL" in sql and "github_user_id BIGINT NOT NULL" in sql
    assert "CONSTRAINT pk_project_repositories PRIMARY KEY (id)" in sql
    assert "CONSTRAINT uq_project_repositories_project_repo UNIQUE (project_id, github_repo_id)" in sql
    assert (
        "CONSTRAINT fk_project_repositories_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id)"
    ) in sql
    assert (
        "CONSTRAINT fk_project_repositories_added_by_users FOREIGN KEY(added_by) REFERENCES users (id) "
        "ON DELETE SET NULL"
    ) in sql
    assert "CONSTRAINT ck_project_repositories_clone_state CHECK (clone_state IN ('pending', 'cloning'" in sql
    assert "CONSTRAINT ck_project_repositories_failure_code CHECK (failure_code IN ('github_not_connected'" in sql
    assert (
        "CONSTRAINT ck_project_repositories_failure_code_matches_state "
        "CHECK ((clone_state = 'failed') = (failure_code IS NOT NULL))"
    ) in sql
    assert "UPDATE alembic_version SET version_num='0007_github'" in sql
    assert "DROP" not in sql.upper()


def test_0007_scopes_the_repositories_to_the_agent_role_and_hides_the_connections_without_a_grant() -> None:
    sql = github_sql()

    scope = "nullif(current_setting('app.project_id', true), '')::uuid"
    assert sql.index("-- lock_timeout skipped (offline)") < sql.index(f"CREATE TABLE {GITHUB_CONNECTIONS}")
    for table in (GITHUB_CONNECTIONS, REPOSITORIES):
        assert sql.index(f"CREATE TABLE {table}") < sql.index(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
    assert sql.count("CREATE POLICY") == 1
    assert (
        f'CREATE POLICY agent_project_scope ON {REPOSITORIES} TO "specflow_agent" '
        f"USING (project_id = {scope}) WITH CHECK (project_id = {scope});"
    ) in sql
    assert "GRANT" not in sql.upper()


def test_the_downgrade_of_0007_drops_only_the_github_tables_and_says_what_is_lost() -> None:
    cfg, buffer = offline_config()

    command.downgrade(cfg, f"{HEAD_0007}:{HEAD_0006}", sql=True)

    sql = buffer.getvalue()
    assert re.findall(r"DROP TABLE (?:IF EXISTS )?(\w+)", sql) == [REPOSITORIES, GITHUB_CONNECTIONS]
    assert "UPDATE alembic_version SET version_num='0006_atlassian_connections'" in sql
    assert sql.index("-- lock_timeout skipped (offline)") < sql.index("DROP TABLE")
    doc = (ALEMBIC_INI.parent / "migrations" / "versions" / "0007_github.py").read_text("utf-8")
    assert "Every GitHub connection is lost" in doc


# The project integrations, added by revision 0008 on top of 0007 -------------------------


def integrations_sql() -> str:
    """The SQL of revision 0008 alone."""

    cfg, buffer = offline_config()
    command.upgrade(cfg, f"{HEAD_0007}:{HEAD_0008}", sql=True)
    return buffer.getvalue()


def test_0008_creates_the_integrations_table_with_its_keys_and_scopes_it_to_the_agent_role() -> None:
    sql = integrations_sql()

    assert re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+) \(", sql) == [INTEGRATIONS]
    assert "CONSTRAINT pk_project_integrations PRIMARY KEY (project_id, app)" in sql
    assert "CONSTRAINT ck_project_integrations_app CHECK (app IN ('atlassian', 'github', 'slack'))" in sql
    assert (
        "CONSTRAINT fk_project_integrations_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id)"
    ) in sql
    assert (
        "CONSTRAINT fk_project_integrations_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL"
    ) in sql
    assert "user_id UUID," in sql and "workspace_id TEXT NOT NULL" in sql  # the member may be gone
    scope = "nullif(current_setting('app.project_id', true), '')::uuid"
    assert sql.index("-- lock_timeout skipped (offline)") < sql.index(f"CREATE TABLE {INTEGRATIONS}")
    assert sql.index(f"CREATE TABLE {INTEGRATIONS}") < sql.index(
        f"ALTER TABLE {INTEGRATIONS} ENABLE ROW LEVEL SECURITY;"
    )
    assert (
        f'CREATE POLICY agent_project_scope ON {INTEGRATIONS} TO "specflow_agent" '
        f"USING (project_id = {scope}) WITH CHECK (project_id = {scope});"
    ) in sql
    assert "GRANT" not in sql.upper()
    assert "UPDATE alembic_version SET version_num='0008_project_integrations'" in sql


def test_0008_widens_the_source_kinds_and_backfills_the_atlassian_and_github_bindings() -> None:
    sql = integrations_sql()

    assert "DROP CONSTRAINT ck_connections_kind" in sql
    assert ("ADD CONSTRAINT ck_connections_kind CHECK (kind IN ('gdrive', 'upload', 'confluence', 'jira_epic'))") in sql
    inserts = [part for part in sql.split(";") if "INSERT INTO project_integrations" in part]
    assert len(inserts) == 2
    assert "'atlassian'" in inserts[0] and "FROM jira_targets JOIN atlassian_connections" in inserts[0]
    assert "'github'" in inserts[1] and "github_connections" in inserts[1] and "first_adders.rank = 1" in inserts[1]


def test_the_downgrade_of_0008_deletes_the_new_sources_drops_the_table_and_says_what_is_lost() -> None:
    cfg, buffer = offline_config()

    command.downgrade(cfg, f"{HEAD_0008}:{HEAD_0007}", sql=True)

    sql = buffer.getvalue()
    assert re.findall(r"DROP TABLE (?:IF EXISTS )?(\w+)", sql) == [INTEGRATIONS]
    assert re.findall(r"DELETE FROM (\w+)", sql) == ["connection_items", "documents", "connections"]
    assert "ADD CONSTRAINT ck_connections_kind CHECK (kind IN ('gdrive', 'upload'))" in sql
    assert "UPDATE alembic_version SET version_num='0007_github'" in sql
    assert sql.index("-- lock_timeout skipped (offline)") < sql.index("DELETE FROM")
    doc = (ALEMBIC_INI.parent / "migrations" / "versions" / "0008_project_integrations.py").read_text("utf-8")
    assert "every project integration" in doc and "DELETES every ``confluence`` and ``jira_epic`` row" in doc


# The Jira gap channel, added by revision 0009 on top of 0008 -------------------------------


def jira_gaps_sql() -> str:
    """The SQL of revision 0009 alone."""

    cfg, buffer = offline_config()
    command.upgrade(cfg, f"{HEAD_0008}:{HEAD_0009}", sql=True)
    return buffer.getvalue()


def test_0009_creates_the_two_gap_tables_with_their_keys_and_scopes_them_to_the_agent_role() -> None:
    sql = jira_gaps_sql()

    assert re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+) \(", sql) == list(JIRA_GAP_TABLES)
    assert "CONSTRAINT pk_jira_gap_targets PRIMARY KEY (project_id)" in sql
    assert "CONSTRAINT pk_jira_gap_issues PRIMARY KEY (project_id, gap_id)" in sql
    assert "CONSTRAINT ck_jira_gap_targets_grouping CHECK (grouping IN ('one', 'area'))" in sql
    scope = "nullif(current_setting('app.project_id', true), '')::uuid"
    for table in JIRA_GAP_TABLES:
        assert f"CONSTRAINT fk_{table}_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id)" in sql
        assert (
            f'CREATE POLICY agent_project_scope ON {table} TO "specflow_agent" '
            f"USING (project_id = {scope}) WITH CHECK (project_id = {scope});"
        ) in sql
    assert sql.index("-- lock_timeout skipped (offline)") < sql.index("CREATE TABLE jira_gap_targets")
    assert "GRANT" not in sql.upper()
    assert "UPDATE alembic_version SET version_num='0009_jira_gaps'" in sql


def test_the_downgrade_of_0009_drops_both_tables_and_says_what_is_lost() -> None:
    cfg, buffer = offline_config()

    command.downgrade(cfg, f"{HEAD_0009}:{HEAD_0008}", sql=True)

    sql = buffer.getvalue()
    assert re.findall(r"DROP TABLE (?:IF EXISTS )?(\w+)", sql) == ["jira_gap_issues", "jira_gap_targets"]
    assert "UPDATE alembic_version SET version_num='0008_project_integrations'" in sql
    doc = (ALEMBIC_INI.parent / "migrations" / "versions" / "0009_jira_gaps.py").read_text("utf-8")
    assert "Every Jira gap target and the whole gap-to-sub-task map is lost" in doc


# The GitHub Projects destination, added by revision 0010 on top of 0009 ----------------------


def github_projects_sql() -> str:
    """The SQL of revision 0010 alone."""

    cfg, buffer = offline_config()
    command.upgrade(cfg, f"{HEAD_0009}:{HEAD_0010}", sql=True)
    return buffer.getvalue()


def test_0010_creates_the_two_projects_tables_with_their_keys_and_scopes_them_to_the_agent_role() -> None:
    sql = github_projects_sql()

    assert re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+) \(", sql) == list(GITHUB_PROJECT_TABLES)
    assert "CONSTRAINT pk_github_project_targets PRIMARY KEY (project_id)" in sql
    assert "CONSTRAINT pk_github_project_items PRIMARY KEY (project_id, node_id)" in sql
    assert "CONSTRAINT uq_github_project_items_project_id UNIQUE (project_id, issue_number)" in sql
    assert "CONSTRAINT ck_github_project_items_kind CHECK (kind IN ('story', 'subtask'))" in sql
    assert "CONSTRAINT ck_github_project_targets_last_trigger CHECK (last_trigger IN ('plan_run', 'settings'" in sql
    assert "REFERENCES project_repositories (id) ON DELETE CASCADE" in sql
    scope = "nullif(current_setting('app.project_id', true), '')::uuid"
    for table in GITHUB_PROJECT_TABLES:
        assert f"CONSTRAINT fk_{table}_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id)" in sql
        assert (
            f'CREATE POLICY agent_project_scope ON {table} TO "specflow_agent" '
            f"USING (project_id = {scope}) WITH CHECK (project_id = {scope});"
        ) in sql
    assert sql.index("-- lock_timeout skipped (offline)") < sql.index("CREATE TABLE github_project_targets")
    assert "GRANT" not in sql.upper()
    assert "UPDATE alembic_version SET version_num='0010_github_projects'" in sql


def test_the_downgrade_of_0010_drops_both_tables_and_says_what_is_lost() -> None:
    cfg, buffer = offline_config()

    command.downgrade(cfg, f"{HEAD_0010}:{HEAD_0009}", sql=True)

    sql = buffer.getvalue()
    assert re.findall(r"DROP TABLE (?:IF EXISTS )?(\w+)", sql) == ["github_project_items", "github_project_targets"]
    assert "UPDATE alembic_version SET version_num='0009_jira_gaps'" in sql
    doc = (ALEMBIC_INI.parent / "migrations" / "versions" / "0010_github_projects.py").read_text("utf-8")
    assert "Every GitHub Projects target and the whole plan-to-issue map is lost" in doc


# Notion, added by revision 0011 on top of 0010 --------------------------------------------------


def notion_sql() -> str:
    """The SQL of revision 0011 alone."""

    cfg, buffer = offline_config()
    command.upgrade(cfg, f"{HEAD_0010}:{HEAD}", sql=True)
    return buffer.getvalue()


def test_0011_creates_the_notion_tables_with_their_keys_and_scopes_the_plan_tables_to_the_agent_role() -> None:
    sql = notion_sql()

    created = re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+) \(", sql)
    assert created == [NOTION_CONNECTIONS, *NOTION_PLAN_TABLES]
    assert "CONSTRAINT pk_notion_connections PRIMARY KEY (user_id)" in sql
    assert "CONSTRAINT pk_notion_plan_targets PRIMARY KEY (project_id)" in sql
    assert "CONSTRAINT pk_notion_plan_items PRIMARY KEY (project_id, node_id)" in sql
    assert "CONSTRAINT uq_notion_plan_items_project_id UNIQUE (project_id, page_id)" in sql
    assert "CONSTRAINT ck_notion_plan_items_kind CHECK (kind IN ('story', 'task'))" in sql
    assert "CONSTRAINT ck_notion_connections_status CHECK (status IN ('active', 'expired'))" in sql
    assert "CONSTRAINT ck_notion_plan_targets_last_trigger CHECK (last_trigger IN ('plan_run', 'settings'" in sql
    assert "REFERENCES users (id) ON DELETE CASCADE" in sql
    scope = "nullif(current_setting('app.project_id', true), '')::uuid"
    for table in NOTION_PLAN_TABLES:
        assert f"CONSTRAINT fk_{table}_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id)" in sql
        assert (
            f'CREATE POLICY agent_project_scope ON {table} TO "specflow_agent" '
            f"USING (project_id = {scope}) WITH CHECK (project_id = {scope});"
        ) in sql
    assert f"ALTER TABLE {NOTION_CONNECTIONS} ENABLE ROW LEVEL SECURITY" in sql
    assert f"CREATE POLICY agent_project_scope ON {NOTION_CONNECTIONS}" not in sql
    assert sql.index("-- lock_timeout skipped (offline)") < sql.index("CREATE TABLE notion_connections")
    assert "GRANT" not in sql.upper()
    assert "UPDATE alembic_version SET version_num='0011_notion'" in sql


def test_0011_widens_the_app_and_source_kind_checks_with_notion() -> None:
    sql = notion_sql()

    assert "ADD CONSTRAINT ck_project_integrations_app CHECK (app IN ('atlassian', 'github', 'slack', 'notion'))" in sql
    assert (
        "ADD CONSTRAINT ck_connections_kind CHECK (kind IN ('gdrive', 'upload', 'confluence', 'jira_epic', 'notion'))"
    ) in sql


def test_the_downgrade_of_0011_drops_the_tables_removes_notion_rows_and_says_what_is_lost() -> None:
    cfg, buffer = offline_config()

    command.downgrade(cfg, f"{HEAD}:{HEAD_0010}", sql=True)

    sql = buffer.getvalue()
    dropped = re.findall(r"DROP TABLE (?:IF EXISTS )?(\w+)", sql)
    assert dropped == ["notion_plan_items", "notion_plan_targets", NOTION_CONNECTIONS]
    assert "DELETE FROM project_integrations WHERE project_integrations.app = 'notion'" in sql
    assert "DELETE FROM connections WHERE connections.kind = 'notion'" in sql
    assert "UPDATE alembic_version SET version_num='0010_github_projects'" in sql
    doc = (ALEMBIC_INI.parent / "migrations" / "versions" / "0011_notion.py").read_text("utf-8")
    assert "every Notion connection" in doc
