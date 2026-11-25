"""The project integrations the routes share (docs/mission-control/integrations-api.md): the member a project's uses
of an app act as, and the API errors of the integration routes. Fixed messages; no token, account id or workspace
text reaches an error."""

from __future__ import annotations

import uuid

from api.errors import ApiError
from api.runtime import Runtime
from db import integrations as integrations_db


def integration_workspace_mismatch() -> ApiError:
    return ApiError(
        409,
        "integration_workspace_mismatch",
        "The project is connected to another workspace of this app. Remove that connection first.",
    )


def integration_held() -> ApiError:
    return ApiError(
        409,
        "integration_already_connected",
        "Another member's active connection is the project's connection to this app. "
        "They or the owner must remove it first.",
    )


def integration_not_found() -> ApiError:
    return ApiError(404, "integration_not_found", "The project has no connection to this app.")


def integration_forbidden() -> ApiError:
    return ApiError(403, "integration_forbidden", "Only the member who connected the app or the owner can remove it.")


def atlassian_not_connected() -> ApiError:
    return ApiError(409, "atlassian_not_connected", "Connect your Atlassian account first.")


def atlassian_reconnect_required() -> ApiError:
    return ApiError(
        409,
        "atlassian_reconnect_required",
        "The Atlassian connection of the member who connected the project expired. Connect again.",
    )


def atlassian_site_mismatch() -> ApiError:
    return ApiError(422, "atlassian_site_mismatch", "The link is not on the Atlassian site of this server.")


def atlassian_unreachable() -> ApiError:
    return ApiError(502, "atlassian_unreachable", "Atlassian could not be reached. Try again later.")


async def acting_member(runtime: Runtime, project_id: uuid.UUID, app: str) -> uuid.UUID | None:
    """The member the project's ``app`` is connected by, whose account every use of it acts as; ``None`` when the
    project has no binding or its member is gone."""

    async with runtime.transaction() as session:
        bound = await integrations_db.get_integration(session, project_id, app)
    return None if bound is None else bound.user_id
