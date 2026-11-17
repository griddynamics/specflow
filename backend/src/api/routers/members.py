"""Project people: the members and invites list, add, remove, cancel an invite and leave (``docs/projects/api.md``).

Access is one membership query (``project_member`` / ``project_admin``): a non-member is 404 ``project_not_found``, a
collaborator on an admin route 403 ``admin_required`` before the body is read further. Removal and leave commit the
membership delete first, then free the person's story claims best effort (``member_claims``). Events carry ids only.
"""

from __future__ import annotations

import uuid
from typing import cast

from fastapi import APIRouter, Response

from api.deps import AdminDep, MemberDep, RuntimeDep, UserDep
from api.errors import ApiError, invite_not_found, member_error, member_not_found, required
from api.member_claims import release_member_claims
from api.schemas import (
    ClaimsOutcome,
    InviteOut,
    MemberAdd,
    MemberAdded,
    MemberAddedInvite,
    MemberAddedMember,
    MemberOut,
    MembersOut,
    Role,
)
from db import members
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.members")
router = APIRouter(prefix="/projects/{pid}", tags=["members"])


def _uuid(raw: str, missing: ApiError) -> uuid.UUID:
    """A path id as a UUID; a malformed one is the ``missing`` error (never FastAPI's raw 422)."""

    try:
        return uuid.UUID(raw)
    except ValueError:
        raise missing from None


def _member_out(row: members.MemberRow) -> MemberOut:
    return MemberOut(
        user_id=row.user_id,
        name=row.name,
        email=row.email,
        role=cast(Role, row.role),
        joined_at=row.joined_at,
        is_you=row.is_you,
    )


def _invite_out(row: members.InviteRow) -> InviteOut:
    return InviteOut(id=row.id, email=row.email, invited_at=row.invited_at)


@router.get("/members", response_model=MembersOut)
async def list_members(runtime: RuntimeDep, user: UserDep, access: MemberDep) -> MembersOut:
    async with runtime.transaction() as session:
        listing = await members.list_members(session, access.project.id, user.id)
    return MembersOut(
        role=cast(Role, access.role),
        limit=listing.limit,
        members=[_member_out(row) for row in listing.members],
        invites=[_invite_out(row) for row in listing.invites],
    )


@router.post("/members", status_code=201, response_model=MemberAdded)
async def add_member(body: MemberAdd, runtime: RuntimeDep, user: UserDep, access: AdminDep) -> MemberAdded:
    domains = required(runtime.settings.auth, "the api role requires auth settings").allowed_email_domains
    try:
        async with runtime.transaction() as session:
            added = await members.add_member_or_invite(session, access.project.id, user.id, body.email, domains)
    except members.MemberError as error:
        raise member_error(error.code) from None
    if isinstance(added, members.MemberAdded):
        log_event(
            logger,
            INFO,
            "api.member.added",
            user_id=str(user.id),
            project_id=str(access.project.id),
            kind="member",
            member_user_id=str(added.member.user_id),
        )
        return MemberAddedMember(member=_member_out(added.member))
    log_event(
        logger,
        INFO,
        "api.member.added",
        user_id=str(user.id),
        project_id=str(access.project.id),
        kind="invite",
        invite_id=str(added.invite.id),
    )
    return MemberAddedInvite(invite=_invite_out(added.invite))


@router.delete("/members/{user_id}", response_model=ClaimsOutcome)
async def remove_member(user_id: str, runtime: RuntimeDep, user: UserDep, access: AdminDep) -> ClaimsOutcome:
    target = _uuid(user_id, member_not_found())
    try:
        async with runtime.transaction() as session:
            await members.remove_member(session, access.project.id, target)
    except members.MemberError as error:
        raise member_error(error.code) from None
    log_event(
        logger,
        INFO,
        "api.member.removed",
        user_id=str(user.id),
        project_id=str(access.project.id),
        member_user_id=str(target),
    )
    outcome = await release_member_claims(runtime, access.project.id, target)
    return ClaimsOutcome(claims_released=outcome.released, claims_release_failed=outcome.failed)


@router.delete("/invites/{invite_id}", status_code=204)
async def cancel_invite(invite_id: str, runtime: RuntimeDep, user: UserDep, access: AdminDep) -> Response:
    target = _uuid(invite_id, invite_not_found())
    try:
        async with runtime.transaction() as session:
            await members.cancel_invite(session, access.project.id, target)
    except members.MemberError as error:
        raise member_error(error.code) from None
    log_event(
        logger,
        INFO,
        "api.invite.cancelled",
        user_id=str(user.id),
        project_id=str(access.project.id),
        invite_id=str(target),
    )
    return Response(status_code=204)


@router.post("/leave", response_model=ClaimsOutcome)
async def leave_project(runtime: RuntimeDep, user: UserDep, access: MemberDep) -> ClaimsOutcome:
    try:
        async with runtime.transaction() as session:
            await members.leave_project(session, access.project.id, user.id)
    except members.MemberError as error:
        raise member_error(error.code) from None
    log_event(
        logger,
        INFO,
        "api.member.left",
        user_id=str(user.id),
        project_id=str(access.project.id),
        member_user_id=str(user.id),
    )
    outcome = await release_member_claims(runtime, access.project.id, user.id)
    return ClaimsOutcome(claims_released=outcome.released, claims_release_failed=outcome.failed)
