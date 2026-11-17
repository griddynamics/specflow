"""Project collaborators: members, invites, leave, rename, suggestions (``docs/projects/api.md``).

Every function runs inside the caller's session and transaction: it flushes but never commits. Writes that must
not race (add, remove, leave) lock the project row first (``SELECT ... FOR UPDATE``), so the 50-person limit holds
under concurrent adds. Failures are the typed ``MemberError`` subclasses below; the API maps each ``code`` to its
status. Emails, names and query text are personal data: they appear in return values, never in a log record.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast

from sqlalchemy import Select, case, delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.emails import domain_allowed, normalise_email
from db.models import Project, ProjectInvite, ProjectMember, User

MEMBER_LIMIT: Final = 50
SUGGESTION_MIN_CHARS: Final = 3
SUGGESTION_LIMIT: Final = 8
_SUGGESTION_FETCH: Final = 64  # rows read before duplicate emails collapse to at most SUGGESTION_LIMIT
OWNER: Final = "owner"
COLLABORATOR: Final = "collaborator"


# Typed outcomes and errors ----------------------------------------------------


class InviteWriteError(RuntimeError):
    """An invite insert failed for a reason that is not a duplicate. Fixed text: the driver error carries the SQL
    parameters (the normalised email), so it is never chained to this one."""

    def __init__(self) -> None:
        super().__init__("invite insert failed")


class MemberError(Exception):
    """Base of the collaborator failures; ``code`` is the API error code (``docs/projects/api.md#errors``)."""

    code: str = "member_error"

    def __init__(self) -> None:
        super().__init__(self.code)


class ProjectMissingError(MemberError):
    code = "project_not_found"


class InvalidEmailError(MemberError):
    code = "invalid_email"


class EmailDomainNotAllowedError(MemberError):
    code = "email_domain_not_allowed"


class AlreadyMemberError(MemberError):
    code = "already_member"


class AlreadyInvitedError(MemberError):
    code = "already_invited"


class MemberLimitError(MemberError):
    code = "member_limit"


class MemberNotFoundError(MemberError):
    code = "member_not_found"


class CannotRemoveOwnerError(MemberError):
    code = "cannot_remove_owner"


class InviteNotFoundError(MemberError):
    code = "invite_not_found"


class OwnerCannotLeaveError(MemberError):
    code = "owner_cannot_leave"


@dataclass(frozen=True)
class MemberRow:
    """One member. ``name`` is ``None`` when the display name is an address (``shown_name``)."""

    user_id: uuid.UUID
    name: str | None
    email: str
    role: str
    joined_at: datetime
    is_you: bool


@dataclass(frozen=True)
class InviteRow:
    id: uuid.UUID
    email: str  # normalised
    invited_at: datetime


@dataclass(frozen=True)
class Members:
    limit: int
    members: list[MemberRow]
    invites: list[InviteRow]


@dataclass(frozen=True)
class MemberAdded:
    member: MemberRow
    kind: Literal["member"] = "member"


@dataclass(frozen=True)
class InviteAdded:
    invite: InviteRow
    kind: Literal["invite"] = "invite"


@dataclass(frozen=True)
class Suggestion:
    name: str | None  # ``None`` when the display name is an address
    email: str


def shown_name(display_name: str, email: str | None) -> str | None:
    """The one name rule: ``None`` when the display name holds an ``@`` or is the email in any case (the fallback of
    ``upsert_user``), else the display name. Shared by ``repositories.member_names`` and this module."""

    is_address = "@" in display_name or display_name.casefold() == (email or "").casefold()
    return None if is_address else display_name


# Helpers ----------------------------------------------------------------------


async def lock_project(session: AsyncSession, project_id: uuid.UUID) -> None:
    """``SELECT ... FOR UPDATE`` on the project row; ``ProjectMissingError`` when there is none."""

    found = await session.scalar(select(Project.id).where(Project.id == project_id).with_for_update())
    if found is None:
        raise ProjectMissingError


async def count_people(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Members plus pending invites of the project (the limit counts both)."""

    members = await session.scalar(select(func.count()).where(ProjectMember.project_id == project_id))
    invites = await session.scalar(select(func.count()).where(ProjectInvite.project_id == project_id))
    return (members or 0) + (invites or 0)


async def newest_user_by_email(session: AsyncSession, email_norm: str) -> User | None:
    """The user whose email equals ``email_norm`` ignoring case; the newest when several rows match."""

    return await session.scalar(
        select(User)
        .where(func.lower(User.email) == email_norm)
        .order_by(User.created_at.desc(), User.id.desc())
        .limit(1)
    )


def _member_row(user: User, role: str, joined_at: datetime, caller_id: uuid.UUID | None) -> MemberRow:
    return MemberRow(
        user.id, shown_name(user.display_name, user.email), user.email, role, joined_at, user.id == caller_id
    )


def _invite_row(invite_id: uuid.UUID, email_norm: str, created_at: datetime) -> InviteRow:
    return InviteRow(invite_id, email_norm, created_at)


# Listing ----------------------------------------------------------------------


async def list_members(session: AsyncSession, project_id: uuid.UUID, caller_id: uuid.UUID) -> Members:
    """Members (the owner first, then by ``joined_at``) and invites (by ``created_at``) in two queries; the caller's
    role is the route's one membership query, not read again here."""

    rows = await session.execute(
        select(User, ProjectMember.role, ProjectMember.joined_at)
        .join(ProjectMember, ProjectMember.user_id == User.id)
        .where(ProjectMember.project_id == project_id)
        .order_by(case((ProjectMember.role == OWNER, 0), else_=1), ProjectMember.joined_at, User.id)
    )
    members = [_member_row(user, role, joined_at, caller_id) for user, role, joined_at in rows]
    invites = await session.execute(
        select(ProjectInvite.id, ProjectInvite.email_norm, ProjectInvite.created_at)
        .where(ProjectInvite.project_id == project_id)
        .order_by(ProjectInvite.created_at, ProjectInvite.id)
    )
    return Members(MEMBER_LIMIT, members, [_invite_row(*row) for row in invites])


# Add --------------------------------------------------------------------------


async def add_member_or_invite(
    session: AsyncSession, project_id: uuid.UUID, invited_by: uuid.UUID, email: str, allowed_domains: Collection[str]
) -> MemberAdded | InviteAdded:
    """Add ``email`` as a ``collaborator`` (a user has it) or as a pending invite (none has).

    One transaction with the project row locked, in the order of api.md "Add a member or invite": email format,
    domain, user lookup (newest on duplicates), already a member, limit. The member check precedes the limit so an
    existing member's email answers ``already_member`` at 50 people.
    Raises ``InvalidEmailError``, ``EmailDomainNotAllowedError``, ``MemberLimitError``, ``AlreadyMemberError``,
    ``AlreadyInvitedError``, ``ProjectMissingError``.
    """

    norm = normalise_email(email)
    if norm is None:
        raise InvalidEmailError
    if not domain_allowed(norm, allowed_domains):
        raise EmailDomainNotAllowedError
    await lock_project(session, project_id)
    user = await newest_user_by_email(session, norm)
    if user is not None:
        await _refuse_a_member(session, project_id, user)  # an existing member is already_member even at the limit
    if await count_people(session, project_id) >= MEMBER_LIMIT:
        raise MemberLimitError
    if user is not None:
        return await _add_user(session, project_id, user)
    return await _add_invite(session, project_id, invited_by, norm)


async def _refuse_a_member(session: AsyncSession, project_id: uuid.UUID, user: User) -> None:
    held = await session.scalar(
        select(ProjectMember.user_id).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user.id)
    )
    if held is not None:
        raise AlreadyMemberError


async def _add_user(session: AsyncSession, project_id: uuid.UUID, user: User) -> MemberAdded:
    joined_at = (
        await session.execute(
            insert(ProjectMember)
            .values(project_id=project_id, user_id=user.id, role=COLLABORATOR)
            .returning(ProjectMember.joined_at)
        )
    ).scalar_one()
    return MemberAdded(_member_row(user, COLLABORATOR, joined_at, None))


async def _insert_invite(
    session: AsyncSession, invite_id: uuid.UUID, project_id: uuid.UUID, invited_by: uuid.UUID, norm: str
) -> datetime | None:
    """``created_at`` of the new invite; ``None`` when the insert hit an integrity error (the caller classifies)."""

    try:
        async with session.begin_nested():
            return (
                await session.execute(
                    insert(ProjectInvite)
                    .values(id=invite_id, project_id=project_id, email_norm=norm, invited_by=invited_by)
                    .returning(ProjectInvite.created_at)
                )
            ).scalar_one()
    except IntegrityError:
        return None


async def _add_invite(session: AsyncSession, project_id: uuid.UUID, invited_by: uuid.UUID, norm: str) -> InviteAdded:
    invite_id = uuid.uuid4()
    created_at = await _insert_invite(session, invite_id, project_id, invited_by, norm)
    if created_at is not None:
        return InviteAdded(_invite_row(invite_id, norm, created_at))
    # The unique constraint turns a duplicate (also a concurrent one) into already_invited; any other integrity
    # failure is InviteWriteError, raised outside the ``except`` so the driver error (SQL parameters, the email) is
    # neither its cause nor its context.
    existing = await session.scalar(
        select(ProjectInvite.id).where(ProjectInvite.project_id == project_id, ProjectInvite.email_norm == norm)
    )
    if existing is None:
        raise InviteWriteError
    raise AlreadyInvitedError


async def add_checked_collaborators(
    session: AsyncSession, project_id: uuid.UUID, creator_id: uuid.UUID, emails: Sequence[str]
) -> None:
    """Create Project: a ``collaborator`` row per email a user has, an invite per other email.

    ``emails`` are the output of ``auth.emails.check_collaborators`` (normalised, de-duplicated, creator's own
    dropped, at most 49), so the limit cannot be reached and no lock is needed on the project this transaction just
    created. Runs in the caller's transaction: a later failure rolls everything back with it.
    """

    if not emails:
        return
    users = await session.scalars(
        select(User)
        .ext(distinct_on(func.lower(User.email)))  # the newest row per address
        .where(func.lower(User.email).in_(list(emails)))
        .order_by(func.lower(User.email), User.created_at.desc(), User.id.desc())
    )
    newest = {user.email.lower(): user for user in users}
    taken = {creator_id}
    for email in emails:
        user = newest.get(email)
        if user is None:
            session.add(ProjectInvite(project_id=project_id, email_norm=email, invited_by=creator_id))
        elif user.id not in taken:
            taken.add(user.id)
            session.add(ProjectMember(project_id=project_id, user_id=user.id, role=COLLABORATOR))
    await session.flush()


# Remove, cancel, leave, rename ------------------------------------------------


async def _delete_collaborator(
    session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID, *, owner_error: type[MemberError]
) -> None:
    await lock_project(session, project_id)
    role = await session.scalar(
        select(ProjectMember.role).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
    )
    if role is None:
        raise MemberNotFoundError
    if role == OWNER:
        raise owner_error
    await session.execute(
        delete(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
    )


async def remove_member(session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Delete a member row. ``MemberNotFoundError`` when none; ``CannotRemoveOwnerError`` for the owner."""

    await _delete_collaborator(session, project_id, user_id, owner_error=CannotRemoveOwnerError)


async def leave_project(session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """The caller leaves. ``OwnerCannotLeaveError`` for the owner, ``MemberNotFoundError`` for a non-member."""

    await _delete_collaborator(session, project_id, user_id, owner_error=OwnerCannotLeaveError)


async def cancel_invite(session: AsyncSession, project_id: uuid.UUID, invite_id: uuid.UUID) -> None:
    """Delete a pending invite of the project; ``InviteNotFoundError`` when there is none."""

    deleted = await session.scalar(
        delete(ProjectInvite)
        .where(ProjectInvite.project_id == project_id, ProjectInvite.id == invite_id)
        .returning(ProjectInvite.id)
    )
    if deleted is None:
        raise InviteNotFoundError


async def rename_project(session: AsyncSession, project_id: uuid.UUID, name: str) -> Project | None:
    """Set ``projects.name`` (the caller validated it) and ``updated_at``; ``None`` when the project is gone."""

    statement = (
        update(Project)
        .where(Project.id == project_id)
        .values(name=name, updated_at=func.now())
        .returning(Project)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    return (await session.scalars(statement)).one_or_none()


# Suggestions ------------------------------------------------------------------


def _escape_like(text_: str) -> str:
    return text_.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def suggestion_statement(
    caller_id: uuid.UUID, q: str, allowed_domains: Collection[str], project_id: uuid.UUID | None
) -> Select[Any]:
    """The suggestions query (api.md "Suggestions"). ``q`` is already normalised; ``%``, ``_`` and ``\\`` in it
    are plain characters."""

    pattern = _escape_like(q)
    display = func.lower(User.display_name)
    domain = func.lower(func.split_part(User.email, "@", 2))
    conditions = [
        User.id != caller_id,
        domain.in_([d.lower() for d in allowed_domains]),
        or_(
            func.lower(User.email).like(f"{pattern}%", escape="\\"),
            display.like(f"{pattern}%", escape="\\"),
            display.like(f"% {pattern}%", escape="\\"),
        ),
    ]
    if project_id is not None:
        conditions += [
            User.id.not_in(select(ProjectMember.user_id).where(ProjectMember.project_id == project_id)),
            func.lower(User.email).not_in(
                select(ProjectInvite.email_norm).where(ProjectInvite.project_id == project_id)
            ),
        ]
    statement = (
        select(User.display_name, User.email)
        .where(*conditions)
        .order_by(display, func.lower(User.email), User.id)
        .limit(_SUGGESTION_FETCH)
    )
    return cast("Select[Any]", statement)  # the stubs read the two-column select as Select[str, str]


async def suggest_users(
    session: AsyncSession,
    caller_id: uuid.UUID,
    q: str,
    allowed_domains: Collection[str],
    project_id: uuid.UUID | None = None,
) -> list[Suggestion]:
    """Up to 8 signed-in users whose email, name or a name word starts with ``q`` (ordered by name, then email).

    ``q`` is normalised here (strip, lower-case); the length rule (3 to 254) is the API's. Excludes the caller,
    emails outside ``allowed_domains`` and, with ``project_id``, its members and invited emails. The caller has
    already proved membership of ``project_id``. One address appears once, whatever number of user rows hold it.
    """

    needle = q.strip().lower()
    rows = await session.execute(suggestion_statement(caller_id, needle, allowed_domains, project_id))
    seen: set[str] = set()
    items: list[Suggestion] = []
    for display_name, email in rows:
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(Suggestion(shown_name(display_name, email), email))
        if len(items) == SUGGESTION_LIMIT:
            break
    return items
