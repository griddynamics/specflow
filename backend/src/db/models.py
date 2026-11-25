"""The application tables (migration ``0001_initial``).

Columns follow the data-model schema and the hello-stack
deviations: ``documents`` drops ``connection_id``/``source_item_id`` (DEV-13),
adds ``markdown`` (C7) and the ``summarization*`` columns (C8), and starts in
status ``uploaded`` (DEV-4). Primary keys are uuid4 values generated in Python;
enums are ``text`` plus a CHECK constraint.

The gap tables (docs/graph/gap/data-model.md "Relational store") hold the relational rows of
``graph.gap``; the host reaches them through ``api.gap.records`` (FR-GOWN-04). Each is keyed by
``project_id`` first, and its ids are the gap library's own text ids.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

PROJECT_ROLES: Final = ("owner", "collaborator")
DOCUMENT_STATUSES: Final = (
    "uploaded",
    "processing",
    "parsed",
    "failed",
    "ingested",
    "partially_ingested",
)
DOCUMENT_ORIGINS: Final = ("upload", "connection", "todo_answer")
SUMMARIZATION_STATUSES: Final = ("none", "running", "succeeded", "failed")

# Sources (migration 0001_initial; INT-GDUI-0002, DATA-GDUI-0001, DATA-SRC-0001).
# The Drive-related values mirror
# drive.categories / reasons; a test keeps them equal. They are
# written out here so the models do not import the Drive package.
# ``confluence`` and ``jira_epic`` are imported by the API in-process (migration 0008_project_integrations).
CONNECTION_KINDS: Final = ("gdrive", "upload", "confluence", "jira_epic")
CONNECTION_STATES: Final = ("listing", "importing", "done", "failed")
CONNECTION_FAILURE_REASONS: Final = (
    "invalid_link",
    "no_access",
    "not_a_folder",
    "already_connected",
    "drive_unavailable",
    "setup_error",
    "too_slow",
    "unknown_error",
)
ITEM_CATEGORIES: Final = (
    "google_docs",
    "google_slides",
    "google_sheets",
    "pdf",
    "word",
    "powerpoint",
    "excel",
    "images",
    "videos",
    "other_files",
    "other_google",
    "shortcuts",
)
ITEM_PROCESSING_CLASSES: Final = ("processed", "stored_only", "skipped")
ITEM_STATES: Final = (
    "pending",
    "processing",
    "processed",
    "stored",
    "skipped",
    "error",
    "duplicate",
)
ITEM_ERROR_REASONS: Final = (
    "no_access",
    "drive_unavailable",
    "convert_failed",
    "parse_failed",
    "store_failed",
    "import_failed",
    "unknown_error",
)
ITEM_ERROR_STEPS: Final = ("list", "download", "read", "convert", "store", "parse")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("actor_id"),
        Index(
            "ix_users_auth_subject",
            "auth_subject",
            unique=True,
            postgresql_where=text("auth_subject IS NOT NULL"),
        ),
        # Case-insensitive email lookups (collaborator add, invite adoption).
        Index("ix_users_email_lower", text("lower(email)")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    auth_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(r"name !~ '^\s*$'", name="name_not_blank"),
        Index("ix_projects_created_by", "created_by"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    pinned_commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Files of sources removed since the last build (DATA-SRC-0003).
    removed_files: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "user_id"),
        CheckConstraint(_one_of("role", PROJECT_ROLES), name="role"),
        Index("ix_project_members_user_id", "user_id"),
        # DM1: exactly one owner per project.
        Index(
            "ux_project_members_one_owner",
            "project_id",
            unique=True,
            postgresql_where=text("role = 'owner'"),
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    joined_at: Mapped[datetime] = _created_at()


class ProjectInvite(Base):
    """An email invited to a project that no user row had yet; the first sign-in adopts it."""

    __tablename__ = "project_invites"
    __table_args__ = (
        UniqueConstraint("project_id", "email_norm", name="uq_project_invites_project_id_email_norm"),
        CheckConstraint("email_norm = lower(btrim(email_norm))", name="email_norm"),
        Index("ix_project_invites_email_norm", "email_norm"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    email_norm: Mapped[str] = mapped_column(Text, nullable=False)
    invited_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("project_id", "content_hash", name="uq_documents_project_id_content_hash"),
        CheckConstraint(_one_of("status", DOCUMENT_STATUSES), name="status"),
        CheckConstraint(
            _one_of("summarization_status", SUMMARIZATION_STATUSES),
            name="summarization_status",
        ),
        CheckConstraint(_one_of("origin", DOCUMENT_ORIGINS), name="origin"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_sha256"),
        CheckConstraint(
            "(previous_revision_id IS NULL) = (version = 1)",
            name="previous_revision",
        ),
        Index("ix_documents_project_id_created_at", "project_id", text("created_at DESC")),
        Index("ix_documents_project_id_status", "project_id", "status"),
        Index("ix_documents_source_id", "source_id"),
        # Documents no source holds yet (old pods, rolling deploy): DATA-SRC-0002.
        Index(
            "ix_documents_unheld",
            "project_id",
            postgresql_where=text("source_id IS NULL AND origin IN ('upload', 'connection')"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    original_key: Mapped[str] = mapped_column(Text, nullable=False)
    markdown_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    parser: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    previous_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'uploaded'"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    skipped_fragments: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    # The one source that holds this content (DATA-SRC-0002); NULL until adopted.
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("connections.id"), nullable=True)
    markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    summarization: Mapped[str | None] = mapped_column(Text, nullable=True)
    summarization_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'none'"))
    summarization_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    summarization_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summarized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Connection(Base):
    """One source of a project: a Drive folder (INT-GDUI-0003) or a Local files batch."""

    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "kind",
            "external_id",
            name="uq_connections_project_kind_external",
        ),
        CheckConstraint(_one_of("kind", CONNECTION_KINDS), name="kind"),
        CheckConstraint("kind <> 'gdrive' OR link IS NOT NULL", name="link_for_drive"),
        CheckConstraint(
            "expected_files IS NULL OR expected_files BETWEEN 1 AND 10000",
            name="expected_files",
        ),
        CheckConstraint("name_suffix >= 1", name="name_suffix"),
        CheckConstraint("external_id ~ '^[A-Za-z0-9_-]{10,}$'", name="external_id"),
        CheckConstraint(_one_of("state", CONNECTION_STATES), name="state"),
        CheckConstraint(
            _one_of("failure_reason", CONNECTION_FAILURE_REASONS),
            name="failure_reason",
        ),
        CheckConstraint(
            "(state = 'failed') = (failure_reason IS NOT NULL)",
            name="failure_reason_matches_state",
        ),
        Index("ix_connections_project_id", "project_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # The Drive folder id, `str(id)` of a new batch, or `legacy-YYYYMMDD`. For a
    # Drive source the link below is always the canonical one; a batch has none.
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'listing'"))
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Local files batch: the picked count, the end of the batch, and the
    # number shown after the name when two batches start in one minute.
    expected_files: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    name_suffix: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class ConnectionItem(Base):
    """One listed file of a connection and how far its import got (DATA-GDUI-0001)."""

    __tablename__ = "connection_items"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "external_id",
            name="uq_connection_items_connection_external",
        ),
        CheckConstraint(_one_of("category", ITEM_CATEGORIES), name="category"),
        CheckConstraint(
            _one_of("processing_class", ITEM_PROCESSING_CLASSES),
            name="processing_class",
        ),
        CheckConstraint(_one_of("state", ITEM_STATES), name="state"),
        CheckConstraint(_one_of("error_reason", ITEM_ERROR_REASONS), name="error_reason"),
        CheckConstraint(_one_of("error_step", ITEM_ERROR_STEPS), name="error_step"),
        CheckConstraint(
            "(state = 'error') = (error_reason IS NOT NULL)",
            name="error_reason_matches_state",
        ),
        CheckConstraint(
            "state <> 'duplicate' OR document_id IS NOT NULL",
            name="duplicate_has_document",
        ),
        Index("ix_connection_items_connection_id_state", "connection_id", "state"),
        Index("ix_connection_items_document_id", "document_id"),
        Index("ix_connection_items_project_id", "project_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("connections.id"), nullable=False)
    # Repeated from the connection for row-level security and the aggregates.
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)  # Drive file id, or content hash
    name: Mapped[str] = mapped_column(Text, nullable=False)  # database only, never logged
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    processing_class: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    modified_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    drive_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    md5: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_step: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


# Gap tables (docs/graph/gap/data-model.md "Relational store"). The vocabularies mirror
# graph.gap.names; a test keeps them equal (db does not import graph).
GAP_RUN_MODES: Final = ("update", "full", "site")
GAP_RUN_STATUSES: Final = ("running", "completed", "degraded", "failed")
GAP_SEED_STRENGTHS: Final = ("strong", "weak")
GAP_RELEVANCES: Final = ("spec", "tracking", "mixed")
GAP_SITE_OUTCOMES: Final = ("gap", "not_a_gap", "not_spec", "duplicate", "skipped", "deferred")
GAP_DECIDED_BY: Final = ("rules", "model", "person")
GAP_NOISE_SCOPES: Final = ("document", "section", "pattern")
AMENDMENT_OPERATIONS: Final = ("insert_after", "replace", "append_section")
AMENDMENT_STATUSES: Final = ("pending", "active", "retired", "suspended", "orphaned", "reverted", "undecided")
GAP_OPTIONS_MAX: Final = 4
JIRA_RUN_STATUSES: Final = ("running", "succeeded", "partial", "failed", "skipped")
JIRA_TRIGGERS: Final = ("plan_run", "mcp", "settings", "recovery")
JIRA_ISSUE_KINDS: Final = ("epic", "story", "subtask")
ATLASSIAN_CONNECTION_STATUSES: Final = ("active", "expired")
# GitHub tables (docs/github/data-model.md). The failure codes mirror github.codes.CLONE_FAILURE_CODES; a test keeps
# them equal (db does not import github).
GITHUB_CONNECTION_STATUSES: Final = ("active", "expired")
INTEGRATION_APPS: Final = ("atlassian", "github", "slack")
JIRA_GAP_GROUPINGS: Final = ("one", "area")
GITHUB_PROJECT_TRIGGERS: Final = ("plan_run", "settings", "recovery")
GITHUB_PROJECT_ITEM_KINDS: Final = ("story", "subtask")
REPOSITORY_CLONE_STATES: Final = ("pending", "cloning", "cloned", "failed")
REPOSITORY_FAILURE_CODES: Final = (
    "github_not_connected",
    "github_reconnect_required",
    "github_repo_not_accessible",
    "github_repo_too_large",
    "github_unreachable",
    "github_auth_failed",
    "github_clone_failed",
    "github_clone_timeout",
    "storage_failed",
)


def _project_id() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)


def _json(*, nullable: bool = False) -> Mapped[Any]:
    return mapped_column(JSONB, nullable=nullable)


class GapOption(Base):
    """One offered answer of a gap: 2 to 4 rows, exactly one recommended."""

    __tablename__ = "gap_option"
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "gap_id", "rank"),
        CheckConstraint(f"rank BETWEEN 1 AND {GAP_OPTIONS_MAX}", name="rank"),
        Index(
            "ix_gap_option_one_recommended",
            "project_id",
            "gap_id",
            unique=True,
            postgresql_where=text("recommended"),
        ),
    )

    project_id: Mapped[uuid.UUID] = _project_id()
    gap_id: Mapped[str] = mapped_column(Text, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    recommended: Mapped[bool] = mapped_column(Boolean, nullable=False)
    edits: Mapped[Any] = _json()
    basis: Mapped[Any] = _json()
    created_run: Mapped[str | None] = mapped_column(Text, nullable=True)


class CorpusAmendment(Base):
    """One stored edit of a clarification, applied by the corpus reader."""

    __tablename__ = "corpus_amendment"
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "amendment_id"),
        CheckConstraint(_one_of("operation", AMENDMENT_OPERATIONS), name="operation"),
        CheckConstraint(_one_of("status", AMENDMENT_STATUSES), name="status"),
        Index("ix_corpus_amendment_lineage_status", "project_id", "lineage", "status"),
        Index("ix_corpus_amendment_clr_id", "project_id", "clr_id"),
    )

    project_id: Mapped[uuid.UUID] = _project_id()
    amendment_id: Mapped[str] = mapped_column(Text, nullable=False)
    clr_id: Mapped[str] = mapped_column(Text, nullable=False)
    lineage: Mapped[str] = mapped_column(Text, nullable=False)
    heading_path: Mapped[Any] = _json()
    anchor_text: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_hash: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    last_revision_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class GapRun(Base):
    """One gap run."""

    __tablename__ = "gap_run"
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "run_id"),
        CheckConstraint(_one_of("mode", GAP_RUN_MODES), name="mode"),
        CheckConstraint(_one_of("status", GAP_RUN_STATUSES), name="status"),
        Index("ix_gap_run_started_at", "project_id", "started_at"),
    )

    project_id: Mapped[uuid.UUID] = _project_id()
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    build_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    counts: Mapped[Any] = _json()
    cost: Mapped[Any] = _json()
    rules_version: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_versions: Mapped[Any] = _json()


class GapSeed(Base):
    """One detector hit of a run (append-only)."""

    __tablename__ = "gap_seed"
    __table_args__ = (
        CheckConstraint(_one_of("strength", GAP_SEED_STRENGTHS), name="strength"),
        CheckConstraint(_one_of("relevance", GAP_RELEVANCES), name="relevance"),
        Index("ix_gap_seed_run_id", "project_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_id()
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    detector: Mapped[str] = mapped_column(Text, nullable=False)
    spec_ids: Mapped[Any] = _json()
    witness: Mapped[Any] = _json()
    strength: Mapped[str] = mapped_column(Text, nullable=False)
    relevance: Mapped[str] = mapped_column(Text, nullable=False)
    corroborated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    site_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class GapSite(Base):
    """One investigation entry point of a run; ``input_hash`` keys the verdict cache."""

    __tablename__ = "gap_site"
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "run_id", "site_id"),
        CheckConstraint(_one_of("outcome", GAP_SITE_OUTCOMES), name="outcome"),
        Index("ix_gap_site_input_hash", "project_id", "input_hash"),
    )

    project_id: Mapped[uuid.UUID] = _project_id()
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    site_id: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    rank_score: Mapped[float] = mapped_column(Float, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    gap_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class GapVerdict(Base):
    """A checked report reused while a site's inputs stay the same."""

    __tablename__ = "gap_verdict"
    __table_args__ = (PrimaryKeyConstraint("project_id", "input_hash"),)

    project_id: Mapped[uuid.UUID] = _project_id()
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    report: Mapped[Any] = _json()
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class GapSectionRelevance(Base):
    """The cached relevance of a section (lineage, heading path, text hash)."""

    __tablename__ = "gap_section_relevance"
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "lineage", "heading_path", "text_hash"),
        CheckConstraint(_one_of("relevance", GAP_RELEVANCES), name="relevance"),
        CheckConstraint(_one_of("by", GAP_DECIDED_BY), name="by"),
    )

    project_id: Mapped[uuid.UUID] = _project_id()
    lineage: Mapped[str] = mapped_column(Text, nullable=False)
    heading_path: Mapped[Any] = _json()
    text_hash: Mapped[str] = mapped_column(Text, nullable=False)
    relevance: Mapped[str] = mapped_column(Text, nullable=False)
    signals: Mapped[Any] = _json()
    by: Mapped[str] = mapped_column(Text, nullable=False)


class GapNoiseRule(Base):
    """A document, section or pattern a person marked "not spec"."""

    __tablename__ = "gap_noise_rule"
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "rule_id"),
        CheckConstraint(_one_of("scope", GAP_NOISE_SCOPES), name="scope"),
    )

    project_id: Mapped[uuid.UUID] = _project_id()
    rule_id: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class GapSuppression(Base):
    """A dismissed gap signature, quiet until it changes."""

    __tablename__ = "gap_suppression"
    __table_args__ = (PrimaryKeyConstraint("project_id", "signature"),)

    project_id: Mapped[uuid.UUID] = _project_id()
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    gap_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)  # database only, never logged
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    dismissed_by: Mapped[str] = mapped_column(Text, nullable=False)
    dismissed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GapNote(Base):
    """An info finding of a run: nothing to decide (append-only)."""

    __tablename__ = "gap_note"
    __table_args__ = (Index("ix_gap_note_run_id", "project_id", "run_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_id()
    run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    detector: Mapped[str] = mapped_column(Text, nullable=False)
    spec_ids: Mapped[Any] = _json()
    text: Mapped[str] = mapped_column(Text, nullable=False)


class GapEvent(Base):
    """One transition, quote or reconcile outcome of a gap or clarification (append-only)."""

    __tablename__ = "gap_event"
    __table_args__ = (
        Index("ix_gap_event_gap_id", "project_id", "gap_id"),
        Index("ix_gap_event_clr_id", "project_id", "clr_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_id()
    gap_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    clr_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[Any] = _json()


class GapAssignment(Base):
    """The member who resolves a gap (migration ``0004_gap_assignment``): one row per gap. The key to the member row
    deletes it with the membership, so a member who leaves or is removed holds no gaps."""

    __tablename__ = "gap_assignment"
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "gap_id"),
        ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["project_members.project_id", "project_members.user_id"],
            ondelete="CASCADE",
        ),
        Index("ix_gap_assignment_user_id", "project_id", "user_id"),
    )

    project_id: Mapped[uuid.UUID] = _project_id()
    gap_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    assigned_by: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JiraTarget(Base):
    """The Jira settings of a project and its last sync pass (migration ``0005_jira_sync``); holds no credential."""

    __tablename__ = "jira_targets"
    __table_args__ = (
        CheckConstraint(_one_of("last_status", JIRA_RUN_STATUSES), name="last_status"),
        CheckConstraint(_one_of("last_trigger", JIRA_TRIGGERS), name="last_trigger"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), primary_key=True)
    jira_project: Mapped[str] = mapped_column(Text, nullable=False)
    epic_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_trigger: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_counts: Mapped[Any] = _json(nullable=True)
    # oauth mode: the owner whose Atlassian connection the sync acts as (migration ``0006_atlassian_connections``)
    connected_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class JiraIssue(Base):
    """The Jira issue of a plan node: a pending row (no key) before the create, the applied content after."""

    __tablename__ = "jira_issues"
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "node_id"),
        CheckConstraint(_one_of("kind", JIRA_ISSUE_KINDS), name="kind"),
        Index(
            "ux_jira_issues_jira_key",
            "project_id",
            "jira_key",
            unique=True,
            postgresql_where=text("jira_key IS NOT NULL"),
        ),
    )

    project_id: Mapped[uuid.UUID] = _project_id()
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    jira_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content: Mapped[Any] = _json(nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AtlassianConnection(Base):
    """One user's Atlassian OAuth connection (migration ``0006_atlassian_connections``, docs/jira-sync/oauth.md
    "Data"): the refresh token is Fernet ciphertext; the access token is never stored. Not project-scoped: row-level
    security is on with no policy and no grant, so the agent role reads nothing of it."""

    __tablename__ = "atlassian_connections"
    __table_args__ = (CheckConstraint(_one_of("status", ATLASSIAN_CONNECTION_STATUSES), name="status"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    cloud_id: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    account_name: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GitHubConnection(Base):
    """One user's GitHub connection (migration ``0007_github``, docs/github/data-model.md): the refresh token is Fernet
    ciphertext; the access token is never stored. Not project-scoped: row-level security is on with no policy and no
    grant, so the agent role reads nothing of it."""

    __tablename__ = "github_connections"
    __table_args__ = (CheckConstraint(_one_of("status", GITHUB_CONNECTION_STATUSES), name="status"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    github_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    login: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectRepository(Base):
    """A GitHub repository of a project and its last clone check (migration ``0007_github``). ``added_by`` is the
    owner whose GitHub connection every clone uses; null once that user is no longer a member. The snapshot fields keep
    the last good clone after a later failure."""

    __tablename__ = "project_repositories"
    __table_args__ = (
        UniqueConstraint("project_id", "github_repo_id", name="uq_project_repositories_project_repo"),
        CheckConstraint(_one_of("clone_state", REPOSITORY_CLONE_STATES), name="clone_state"),
        CheckConstraint(_one_of("failure_code", REPOSITORY_FAILURE_CODES), name="failure_code"),
        CheckConstraint("(clone_state = 'failed') = (failure_code IS NOT NULL)", name="failure_code_matches_state"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    github_repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    html_url: Mapped[str] = mapped_column(Text, nullable=False)
    private: Mapped[bool] = mapped_column(Boolean, nullable=False)
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    clone_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    failure_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_branch: Mapped[str | None] = mapped_column(Text, nullable=True)
    head_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    snapshot_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cloned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class ProjectIntegration(Base):
    """The member whose per-user connection a project's uses of an app act as (migration
    ``0008_project_integrations``): one row per app per project, with the one workspace (Atlassian site, GitHub account,
    Slack workspace) of the project. ``user_id`` is null once that member is no longer a member. Holds no credential."""

    __tablename__ = "project_integrations"
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "app"),
        CheckConstraint(_one_of("app", INTEGRATION_APPS), name="app"),
    )

    project_id: Mapped[uuid.UUID] = _project_id()
    app: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_name: Mapped[str] = mapped_column(Text, nullable=False)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JiraGapTarget(Base):
    """The Jira gap channel of a project (migration ``0009_jira_gaps``): where its open questions are posted and how
    they are grouped, the one Story they hang under, and the poll cursor (the newest Jira ``updated`` time read).
    Holds no credential; the acting account is the member the project's Atlassian is connected by."""

    __tablename__ = "jira_gap_targets"
    __table_args__ = (CheckConstraint(_one_of("grouping", JIRA_GAP_GROUPINGS), name="grouping"),)

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), primary_key=True)
    jira_project: Mapped[str] = mapped_column(Text, nullable=False)
    grouping: Mapped[str] = mapped_column(Text, nullable=False)
    story_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    poll_cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)


class JiraGapIssue(Base):
    """A gap posted to Jira: the sub-task that asks it, its ``area`` (the sub-task's group), its number in the sub-task
    and when it was posted and answered. Holds no Jira text."""

    __tablename__ = "jira_gap_issues"
    __table_args__ = (PrimaryKeyConstraint("project_id", "gap_id"),)

    project_id: Mapped[uuid.UUID] = _project_id()
    gap_id: Mapped[str] = mapped_column(Text, nullable=False)
    issue_key: Mapped[str] = mapped_column(Text, nullable=False)
    area: Mapped[str] = mapped_column(Text, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GitHubProjectTarget(Base):
    """The GitHub Projects destination of a project and its last export pass (migration ``0010_github_projects``,
    docs/github/projects.md): the repository the issues are created in, the organization project they are added to, and
    the member whose GitHub connection the export acts as. Holds no credential."""

    __tablename__ = "github_project_targets"
    __table_args__ = (
        CheckConstraint(_one_of("last_status", JIRA_RUN_STATUSES), name="last_status"),
        CheckConstraint(_one_of("last_trigger", GITHUB_PROJECT_TRIGGERS), name="last_trigger"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), primary_key=True)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_repositories.id", ondelete="CASCADE"), nullable=False
    )
    project_node_id: Mapped[str] = mapped_column(Text, nullable=False)
    project_title: Mapped[str] = mapped_column(Text, nullable=False)
    project_url: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_status: Mapped[str | None] = mapped_column(Text)
    last_trigger: Mapped[str | None] = mapped_column(Text)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_counts: Mapped[Any] = _json(nullable=True)
    connected_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class GitHubProjectItem(Base):
    """The issue of a plan node: a pending row (no number) before the create, the applied content after. A story is an
    issue with an item on the project; a task is a sub-issue of its story's issue."""

    __tablename__ = "github_project_items"
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "node_id"),
        UniqueConstraint("project_id", "issue_number"),
        CheckConstraint(_one_of("kind", GITHUB_PROJECT_ITEM_KINDS), name="kind"),
    )

    project_id: Mapped[uuid.UUID] = _project_id()
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issue_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    issue_node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content: Mapped[Any] = _json(nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
