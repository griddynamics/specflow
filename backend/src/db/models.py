"""The application tables (migration ``0001_initial``).

Columns follow the data-model schema and the hello-stack
deviations: ``documents`` drops ``connection_id``/``source_item_id`` (DEV-13),
adds ``markdown`` (C7) and the ``summarization*`` columns (C8), and starts in
status ``uploaded`` (DEV-4). Primary keys are uuid4 values generated in Python;
enums are ``text`` plus a CHECK constraint.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

PROJECT_ROLES: Final = ("owner",)
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
CONNECTION_KINDS: Final = ("gdrive", "upload")
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
    )

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'owner'"))
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    joined_at: Mapped[datetime] = _created_at()


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
