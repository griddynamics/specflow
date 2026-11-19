"""Gap module: the relational rows of graph.gap, with row-level security for the agent role.

Adds, on top of revision 0002_project_collaborators, the eleven tables of the gap module's
relational store (docs/graph/gap/data-model.md "Relational store"): gap_option,
corpus_amendment, gap_run, gap_seed, gap_site, gap_verdict, gap_section_relevance,
gap_noise_rule, gap_suppression, gap_note and gap_event. Every table has ``project_id`` (a
foreign key to ``projects``), row-level security and the policy ``agent_project_scope`` of the
agent role, and no grant to that role: only the application role reads and writes them. No
existing table changes.

Locks: the first online statement of upgrade() and downgrade() is ``SET LOCAL lock_timeout =
'10s'``. The foreign keys to ``projects`` take a lock on it; if a lock is not granted in time the
statement fails and the WHOLE transaction (one per run) is rolled back, so nothing is half
applied; the next start of the api retries the revision.

Not re-runnable: no database was built from a schema that already held these tables.

downgrade() returns to the 0002_project_collaborators schema exactly: it DROPS the eleven gap
tables, which also drops their indexes, constraints and policies. Every gap row is lost (options,
amendments, runs, seeds, sites, verdicts, relevance cache, noise rules, suppressions, notes and the
gap event log); the Gap and Clarification nodes in the graph store stay, without their options and
amendments. Every other table is untouched.

Revision ID: 0003_gap_module
Revises: 0002_project_collaborators
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from db.migration_helpers import agent_role, set_lock_timeout

# revision identifiers, used by Alembic.
revision: str = "0003_gap_module"
down_revision: str | Sequence[str] | None = "0002_project_collaborators"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
TIMESTAMPTZ = sa.DateTime(timezone=True)

LOCK_TIMEOUT = "0003_lock_timeout.sql"
AGENT_ROLE = "0003_agent_role.sql"
ENABLE_RLS = "0003_enable_rls.sql"
CREATE_POLICY = "0003_create_policy.sql"
# The table and the role the two statements above name; replaced per table and with the agent role.
TABLE_PLACEHOLDER = "gap_option"
POLICY_ROLE_PLACEHOLDER = "agent_role"

# The gap module's relational rows, in creation order (none references another).
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

GAP_RUN_MODES = ("update", "full", "site")
GAP_RUN_STATUSES = ("running", "completed", "degraded", "failed")
GAP_SEED_STRENGTHS = ("strong", "weak")
GAP_RELEVANCES = ("spec", "tracking", "mixed")
GAP_SITE_OUTCOMES = ("gap", "not_a_gap", "not_spec", "duplicate", "skipped", "deferred")
GAP_DECIDED_BY = ("rules", "model", "person")
GAP_NOISE_SCOPES = ("document", "section", "pattern")
AMENDMENT_OPERATIONS = ("insert_after", "replace", "append_section")
AMENDMENT_STATUSES = ("pending", "active", "retired", "suspended", "orphaned", "reverted", "undecided")


def read_migration_sql(name: str) -> str:
    """One statement of ``migrations/sql/<name>``, on one line."""

    text = (Path(__file__).resolve().parent.parent / "sql" / name).read_text(encoding="utf-8")
    return " ".join(text.split()).replace("( ", "(").replace(" )", ")")


def _agent_role() -> str:
    """The agent role, quoted; online, it must exist and row-level security must apply to it."""

    return agent_role(read_migration_sql(AGENT_ROLE))


def _set_lock_timeout() -> None:
    """First online statement: give up on a lock after 10 s."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))


def _one_of(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _timestamp(name: str = "created_at") -> sa.Column[Any]:
    return sa.Column(name, TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False)


def _project_id() -> sa.Column[Any]:
    return sa.Column("project_id", UUID, nullable=False)


def _project_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{table}_project_id_projects")


def _text(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.Text(), nullable=nullable)


def _json(name: str) -> sa.Column[Any]:
    return sa.Column(name, JSONB, nullable=False)


def _check(table: str, column: str, values: Sequence[str]) -> sa.CheckConstraint:
    return sa.CheckConstraint(_one_of(column, values), name=op.f(f"ck_{table}_{column}"))


def _create_gap_option() -> None:
    # One offered answer of a gap: 2 to 4 rows, exactly one recommended.
    op.create_table(
        "gap_option",
        _project_id(),
        _text("gap_id"),
        sa.Column("rank", sa.Integer(), nullable=False),
        _text("label"),
        sa.Column("detail", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("recommended", sa.Boolean(), nullable=False),
        _json("edits"),
        _json("basis"),
        _text("created_run", nullable=True),
        sa.CheckConstraint("rank BETWEEN 1 AND 4", name=op.f("ck_gap_option_rank")),
        _project_fk("gap_option"),
        sa.PrimaryKeyConstraint("project_id", "gap_id", "rank", name="pk_gap_option"),
    )
    op.create_index(
        "ix_gap_option_one_recommended",
        "gap_option",
        ["project_id", "gap_id"],
        unique=True,
        postgresql_where=sa.text("recommended"),
    )


def _create_corpus_amendment() -> None:
    # One stored edit of a clarification, applied by the corpus reader.
    op.create_table(
        "corpus_amendment",
        _project_id(),
        _text("amendment_id"),
        _text("clr_id"),
        _text("lineage"),
        _json("heading_path"),
        _text("anchor_text"),
        _text("anchor_hash"),
        _text("operation"),
        _text("text"),
        _text("status"),
        _text("last_revision_id", nullable=True),
        _timestamp(),
        _timestamp("updated_at"),
        _check("corpus_amendment", "operation", AMENDMENT_OPERATIONS),
        _check("corpus_amendment", "status", AMENDMENT_STATUSES),
        _project_fk("corpus_amendment"),
        sa.PrimaryKeyConstraint("project_id", "amendment_id", name="pk_corpus_amendment"),
    )
    op.create_index("ix_corpus_amendment_lineage_status", "corpus_amendment", ["project_id", "lineage", "status"])
    op.create_index("ix_corpus_amendment_clr_id", "corpus_amendment", ["project_id", "clr_id"])


def _create_gap_run() -> None:
    op.create_table(
        "gap_run",
        _project_id(),
        _text("run_id"),
        _text("build_id", nullable=True),
        _text("mode"),
        _text("status"),
        sa.Column("started_at", TIMESTAMPTZ, nullable=False),
        sa.Column("finished_at", TIMESTAMPTZ, nullable=True),
        _json("counts"),
        _json("cost"),
        _text("rules_version"),
        _json("prompt_versions"),
        _check("gap_run", "mode", GAP_RUN_MODES),
        _check("gap_run", "status", GAP_RUN_STATUSES),
        _project_fk("gap_run"),
        sa.PrimaryKeyConstraint("project_id", "run_id", name="pk_gap_run"),
    )
    op.create_index("ix_gap_run_started_at", "gap_run", ["project_id", "started_at"])


def _create_gap_seed() -> None:
    # Every detector hit of a run (append-only).
    op.create_table(
        "gap_seed",
        sa.Column("id", UUID, nullable=False),
        _project_id(),
        _text("run_id"),
        _text("detector"),
        _json("spec_ids"),
        _json("witness"),
        _text("strength"),
        _text("relevance"),
        sa.Column("corroborated", sa.Boolean(), nullable=False),
        _text("site_id", nullable=True),
        _check("gap_seed", "strength", GAP_SEED_STRENGTHS),
        _check("gap_seed", "relevance", GAP_RELEVANCES),
        _project_fk("gap_seed"),
        sa.PrimaryKeyConstraint("id", name="pk_gap_seed"),
    )
    op.create_index("ix_gap_seed_run_id", "gap_seed", ["project_id", "run_id"])


def _create_gap_site() -> None:
    # One investigation entry point of a run; input_hash keys the verdict cache.
    op.create_table(
        "gap_site",
        _project_id(),
        _text("run_id"),
        _text("site_id"),
        _text("input_hash"),
        sa.Column("rank_score", sa.Float(), nullable=False),
        _text("outcome"),
        _text("gap_id", nullable=True),
        _check("gap_site", "outcome", GAP_SITE_OUTCOMES),
        _project_fk("gap_site"),
        sa.PrimaryKeyConstraint("project_id", "run_id", "site_id", name="pk_gap_site"),
    )
    op.create_index("ix_gap_site_input_hash", "gap_site", ["project_id", "input_hash"])


def _create_gap_verdict() -> None:
    op.create_table(
        "gap_verdict",
        _project_id(),
        _text("input_hash"),
        _json("report"),
        _text("model"),
        _text("prompt_version"),
        _timestamp(),
        _project_fk("gap_verdict"),
        sa.PrimaryKeyConstraint("project_id", "input_hash", name="pk_gap_verdict"),
    )


def _create_gap_section_relevance() -> None:
    op.create_table(
        "gap_section_relevance",
        _project_id(),
        _text("lineage"),
        _json("heading_path"),
        _text("text_hash"),
        _text("relevance"),
        _json("signals"),
        _text("by"),
        _check("gap_section_relevance", "relevance", GAP_RELEVANCES),
        _check("gap_section_relevance", "by", GAP_DECIDED_BY),
        _project_fk("gap_section_relevance"),
        sa.PrimaryKeyConstraint(
            "project_id",
            "lineage",
            "heading_path",
            "text_hash",
            name="pk_gap_section_relevance",
        ),
    )


def _create_gap_noise_rule() -> None:
    op.create_table(
        "gap_noise_rule",
        _project_id(),
        _text("rule_id"),
        _text("scope"),
        _text("value"),
        _text("reason"),
        _text("created_by"),
        _timestamp(),
        _check("gap_noise_rule", "scope", GAP_NOISE_SCOPES),
        _project_fk("gap_noise_rule"),
        sa.PrimaryKeyConstraint("project_id", "rule_id", name="pk_gap_noise_rule"),
    )


def _create_gap_suppression() -> None:
    op.create_table(
        "gap_suppression",
        _project_id(),
        _text("signature"),
        _text("gap_id", nullable=True),
        _text("question"),
        _text("reason"),
        _text("dismissed_by"),
        sa.Column("dismissed_at", TIMESTAMPTZ, nullable=False),
        _project_fk("gap_suppression"),
        sa.PrimaryKeyConstraint("project_id", "signature", name="pk_gap_suppression"),
    )


def _create_gap_note() -> None:
    # Info findings of a run: nothing to decide (append-only).
    op.create_table(
        "gap_note",
        sa.Column("id", UUID, nullable=False),
        _project_id(),
        _text("run_id", nullable=True),
        _text("detector"),
        _json("spec_ids"),
        _text("text"),
        _project_fk("gap_note"),
        sa.PrimaryKeyConstraint("id", name="pk_gap_note"),
    )
    op.create_index("ix_gap_note_run_id", "gap_note", ["project_id", "run_id"])


def _create_gap_event() -> None:
    # Every transition, quote and reconcile outcome (append-only).
    op.create_table(
        "gap_event",
        sa.Column("id", UUID, nullable=False),
        _project_id(),
        _text("gap_id", nullable=True),
        _text("clr_id", nullable=True),
        sa.Column("at", TIMESTAMPTZ, nullable=False),
        _text("actor"),
        _text("event"),
        _json("detail"),
        _project_fk("gap_event"),
        sa.PrimaryKeyConstraint("id", name="pk_gap_event"),
    )
    op.create_index("ix_gap_event_gap_id", "gap_event", ["project_id", "gap_id"])
    op.create_index("ix_gap_event_clr_id", "gap_event", ["project_id", "clr_id"])


def _scope_to_agent(role: str) -> None:
    """Row-level security and the policy ``agent_project_scope`` on every gap table; no grant."""

    for table in GAP_TABLES:
        op.execute(read_migration_sql(ENABLE_RLS).replace(TABLE_PLACEHOLDER, table))
        op.execute(
            read_migration_sql(CREATE_POLICY).replace(TABLE_PLACEHOLDER, table).replace(POLICY_ROLE_PLACEHOLDER, role)
        )


def upgrade() -> None:
    """Upgrade schema: the eleven gap tables with row-level security for the agent role."""

    _set_lock_timeout()
    role = _agent_role()
    _create_gap_option()
    _create_corpus_amendment()
    _create_gap_run()
    _create_gap_seed()
    _create_gap_site()
    _create_gap_verdict()
    _create_gap_section_relevance()
    _create_gap_noise_rule()
    _create_gap_suppression()
    _create_gap_note()
    _create_gap_event()
    _scope_to_agent(role)


def downgrade() -> None:
    """Downgrade schema to 0002_project_collaborators; every gap row is lost."""

    _set_lock_timeout()
    for table in reversed(GAP_TABLES):
        op.drop_table(table)
