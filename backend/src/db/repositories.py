"""Application-role data access (SPECS section 6, AN section 3.3).

Every function runs inside the caller's session and transaction: it flushes
but never commits. Status transitions are single UPDATE statements; the
"started" transitions (``mark_processing``, ``mark_summarizing``) are
conditional, so a repeated click changes nothing and returns ``False``. Error
columns only ever hold one of the fixed codes below, never exception text.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, make_dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from sqlalchemy import ColumnElement, Select, and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from db.models import Document, Project, ProjectMember, User
from db.sources import UPLOAD_STALE_AFTER, DocumentFile
from logger.logger import INFO, get_logger, log_event

logger = get_logger("db.repositories")

PARSE_ERROR_CODES: Final = frozenset(
    {
        "pdf_parse_failed",
        "document_not_found",
        "original_not_found",
        "workflow_start_failed",
        "internal_error",
    }
)
SUMMARIZATION_ERROR_CODES: Final = frozenset({"agent_failed", "tool_failed", "workflow_start_failed", "internal_error"})
DISPLAY_NAME_MAX: Final = 200
PDF_MEDIA_TYPE: Final = "application/pdf"
UPLOAD_ORIGIN: Final = "upload"

# The two columns that can be megabytes. List reads and updates never need them.
LARGE_COLUMNS: Final = ("markdown", "summarization")

_ACTOR_CHARS = re.compile(r"[^a-z0-9._-]")


class UserPrincipal(Protocol):
    """The verified identity fields ``upsert_user`` needs (``auth.Principal``)."""

    @property
    def subject(self) -> str: ...

    @property
    def email(self) -> str: ...

    @property
    def name(self) -> str | None: ...


@dataclass(frozen=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None


class InvalidCursorError(ValueError):
    """The opaque pagination cursor could not be decoded."""


# An immutable copy of a ``documents`` row: the ORM attribute names, no session.
DocumentSnapshot = make_dataclass(
    "DocumentSnapshot",
    [column.key for column in Document.__table__.columns],
    frozen=True,
)


def snapshot_document(document: Document) -> Any:
    """A ``DocumentSnapshot`` of a loaded row; readable after commit or rollback."""

    return DocumentSnapshot(**{column.key: getattr(document, column.key) for column in Document.__table__.columns})


class DuplicateDocumentError(Exception):
    """``UNIQUE(project_id, content_hash)``: the project already has this content.

    ``existing`` is a ``DocumentSnapshot`` (same attribute names as
    ``Document``), so it stays readable after the session rolls back.
    """

    def __init__(self, existing: Document | Any) -> None:
        super().__init__("duplicate document content in this project")
        self.existing = snapshot_document(existing) if isinstance(existing, Document) else existing


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        stamp, row_id = base64.urlsafe_b64decode(padded).decode().split("|")
        created_at = datetime.fromisoformat(stamp)
        parsed_id = uuid.UUID(row_id)
        if created_at.tzinfo is not None:
            created_at.astimezone(UTC)  # OverflowError when the UTC instant is out of range (asyncpg would fail)
    except (ValueError, OverflowError, binascii.Error, UnicodeDecodeError) as error:
        raise InvalidCursorError("invalid cursor") from error
    if created_at.tzinfo is None:
        raise InvalidCursorError("invalid cursor")
    return created_at, parsed_id


def actor_id_for(email: str, subject: str | None = None) -> str:
    """``human:<local part>``, lowercased and limited to ``[a-z0-9._-]``.

    When no valid character is left, ``human:<subject>`` (if given).
    """

    local = _ACTOR_CHARS.sub("", email.split("@", 1)[0].lower())
    if not local and subject:
        return f"human:{subject}"
    return f"human:{local}"


async def _page[M: (Project, Document)](
    session: AsyncSession,
    statement: Select[M],
    model: type[M],
    limit: int,
    cursor: str | None,
) -> Page[M]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if cursor is not None:
        created_at, row_id = decode_cursor(cursor)
        statement = statement.where(
            or_(
                model.created_at < created_at,
                and_(model.created_at == created_at, model.id < row_id),
            )
        )
    statement = statement.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)
    rows = list((await session.scalars(statement)).all())
    more = len(rows) > limit
    if more:
        rows.pop()  # the probe row that only told us another page exists
    last = rows[-1] if more else None
    return Page(rows, encode_cursor(last.created_at, last.id) if last is not None else None)


# Users and projects -------------------------------------------------------


async def upsert_user(session: AsyncSession, principal: UserPrincipal) -> User:
    """Find the user by ``auth_subject`` or create it just in time.

    Email and display name are refreshed from the token on every call. The
    actor id is ``human:<local part>``, or ``human:<sub>`` when that is taken.
    """

    display_name = (principal.name or principal.email)[:DISPLAY_NAME_MAX]
    user = await session.scalar(select(User).where(User.auth_subject == principal.subject))
    if user is not None:
        if user.email != principal.email:
            log_event(logger, INFO, "db.user.email_changed", user_id=str(user.id))
        user.email = principal.email
        user.display_name = display_name
        await session.flush()
        return user
    candidates = dict.fromkeys((actor_id_for(principal.email, principal.subject), f"human:{principal.subject}"))
    for actor_id in candidates:
        user = User(
            actor_id=actor_id,
            display_name=display_name,
            email=principal.email,
            auth_subject=principal.subject,
        )
        try:
            async with session.begin_nested():
                session.add(user)
        except IntegrityError:
            # A concurrent first sign-in of the same subject won the race.
            raced = await session.scalar(select(User).where(User.auth_subject == principal.subject))
            if raced is not None:
                return raced
            continue
        return user
    raise RuntimeError("could not provision the user: both actor ids are taken")


async def create_project(session: AsyncSession, user_id: uuid.UUID, name: str) -> Project:
    """Insert the project and its creator as the ``owner`` member."""

    project = Project(name=name, created_by=user_id)
    session.add(project)
    await session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=user_id, role="owner"))
    await session.flush()
    await session.refresh(project)
    return project


def _member_projects(user_id: uuid.UUID) -> Select[Project]:
    return (
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user_id)
    )


async def list_projects(
    session: AsyncSession, user_id: uuid.UUID, limit: int, cursor: str | None = None
) -> Page[Project]:
    return await _page(session, _member_projects(user_id), Project, limit, cursor)


async def get_member_project(session: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID) -> Project | None:
    """The project when ``user_id`` is a member, else ``None`` (the API answers 404)."""

    return await session.scalar(_member_projects(user_id).where(Project.id == project_id))


# Documents ----------------------------------------------------------------


async def insert_document(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    file: DocumentFile,
    uploaded_by: uuid.UUID | None,
    source_id: uuid.UUID | None = None,
) -> Document:
    """Insert an uploaded PDF in status ``uploaded``.

    ``source_id`` is the holder: the Local files batch the file arrived in
    (DATA-SRC-0002). ``None`` only for callers that predate batches.

    Raises ``DuplicateDocumentError`` (with the existing row) when the project
    already holds the same content (C20).
    """

    document = Document(
        id=file.document_id,
        project_id=project_id,
        content_hash=file.content_hash,
        original_filename=file.filename,
        media_type=PDF_MEDIA_TYPE,
        size_bytes=file.size_bytes,
        origin=UPLOAD_ORIGIN,
        original_key=file.original_key,
        uploaded_by=uploaded_by,
        source_id=source_id,
    )
    try:
        async with session.begin_nested():
            session.add(document)
    except IntegrityError:
        existing = await find_by_hash(session, project_id, file.content_hash)
        if existing is None:
            raise
        raise DuplicateDocumentError(existing) from None
    await session.refresh(document)
    return document


async def find_by_hash(session: AsyncSession, project_id: uuid.UUID, content_hash: str) -> Document | None:
    return await session.scalar(
        select(Document).where(Document.project_id == project_id, Document.content_hash == content_hash)
    )


async def list_documents(
    session: AsyncSession, project_id: uuid.UUID, limit: int, cursor: str | None = None
) -> Page[Document]:
    statement = (
        select(Document)
        .where(Document.project_id == project_id)
        .options(*_defer_large())  # the list view shows neither (DocumentOut)
    )
    return await _page(session, statement, Document, limit, cursor)


def _defer_large() -> tuple[Any, ...]:
    return tuple(defer(getattr(Document, name)) for name in LARGE_COLUMNS)


async def get_document_state(session: AsyncSession, project_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
    """Like ``get_document`` without ``markdown`` and ``summarization``.

    For callers that only look at the status. Reading a deferred column on the
    result raises (async sessions cannot lazy load), so it cannot be misused.
    """

    return await session.scalar(
        select(Document).where(Document.project_id == project_id, Document.id == document_id).options(*_defer_large())
    )


async def get_document(session: AsyncSession, project_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
    return await session.scalar(select(Document).where(Document.project_id == project_id, Document.id == document_id))


async def _update_document(
    session: AsyncSession,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    values: Mapping[str, object],
    *conditions: ColumnElement[bool],
) -> bool:
    """One UPDATE ... RETURNING; a loaded ``Document`` is refreshed in place.

    RETURNING (not expiry) keeps attributes such as ``updated_at`` readable
    without a lazy load, which async sessions cannot do.
    """

    statement = (
        update(Document)
        .where(Document.project_id == project_id, Document.id == document_id, *conditions)
        .values(**values, updated_at=func.now())
        .returning(Document)
        .options(*_defer_large())
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    row = (await session.scalars(statement)).one_or_none()
    if row is None:
        return False
    # RETURNING skips the two large columns; a row the caller already holds
    # gets the values just written (plain strings only) instead.
    written = {n: values[n] for n in LARGE_COLUMNS if isinstance(values.get(n), str)}
    held = session.identity_map.get(identity_key(Document, document_id)) if written else None
    if held is not None:
        for name, value in written.items():
            set_committed_value(held, name, value)
    return True


def _checked(code: str, allowed: frozenset[str]) -> str:
    if code not in allowed:
        raise ValueError(f"unknown error code; expected one of {sorted(allowed)}")
    return code


async def mark_processing(
    session: AsyncSession,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    include_stale: bool = False,
) -> bool:
    """``uploaded``/``failed`` -> ``processing``; ``False`` when nothing changed.

    ``include_stale`` also takes a ``processing`` or ``uploaded`` document whose last
    write is older than ``UPLOAD_STALE_AFTER``: the file reads Failed (DATA-SRC-0001
    rule 5), so a re-upload or a hand-over may start its parse again (R10). Off by
    default, so every existing caller keeps today's behaviour.
    """

    allowed: ColumnElement[bool] = Document.status.in_(("uploaded", "failed"))
    if include_stale:
        stale = and_(
            Document.status.in_(("processing", "uploaded")),
            Document.updated_at < func.now() - UPLOAD_STALE_AFTER,
        )
        allowed = or_(allowed, stale)
    return await _update_document(
        session,
        project_id,
        document_id,
        {"status": "processing", "error": None},
        allowed,
    )


async def mark_parsed(
    session: AsyncSession,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    markdown: str,
    parser: str,
) -> bool:
    """Store the markdown and set ``parsed``; ``False`` when the row does not exist."""

    values = {"markdown": markdown, "parser": parser, "status": "parsed", "error": None}
    return await _update_document(session, project_id, document_id, values)


async def mark_failed(session: AsyncSession, project_id: uuid.UUID, document_id: uuid.UUID, code: str) -> bool:
    code = _checked(code, PARSE_ERROR_CODES)
    return await _update_document(session, project_id, document_id, {"status": "failed", "error": code})


async def mark_summarizing(session: AsyncSession, project_id: uuid.UUID, document_id: uuid.UUID) -> bool:
    """A parsed document's summary -> ``running``; ``False`` when already running or not parsed."""

    return await _update_document(
        session,
        project_id,
        document_id,
        {"summarization_status": "running", "summarization_error": None},
        Document.status == "parsed",
        Document.summarization_status != "running",
    )


async def save_summarization(
    session: AsyncSession,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    summary: str,
    model: str,
) -> bool:
    """Overwrite the summary (C8) and set ``succeeded``."""

    values = {
        "summarization": summary,
        "summarization_model": model,
        "summarization_status": "succeeded",
        "summarization_error": None,
        "summarized_at": func.now(),
    }
    return await _update_document(session, project_id, document_id, values)


async def mark_summarization_failed(
    session: AsyncSession, project_id: uuid.UUID, document_id: uuid.UUID, code: str
) -> bool:
    """Set ``failed`` with a fixed code; an earlier summary stays."""

    code = _checked(code, SUMMARIZATION_ERROR_CODES)
    values = {"summarization_status": "failed", "summarization_error": code}
    return await _update_document(session, project_id, document_id, values)
