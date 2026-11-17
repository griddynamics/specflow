"""``POST /users/suggestions``: signed-in users to add as collaborators (``docs/projects/api.md#suggestions``).

POST so the typed text stays out of URLs and access logs. The text is never logged (``api.users.suggested`` has the
caller id and the count). With ``project_id`` the caller must be a member (404 ``project_not_found`` otherwise).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from api.deps import RuntimeDep, UserDep
from api.errors import project_not_found, required
from api.schemas import SuggestionOut, SuggestionsIn, SuggestionsOut
from db import members
from db import repositories as repo
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.users")
router = APIRouter(tags=["users"])


@router.post("/users/suggestions", response_model=SuggestionsOut)
async def suggest_users(body: SuggestionsIn, runtime: RuntimeDep, user: UserDep) -> SuggestionsOut:
    domains = required(runtime.settings.auth, "the api role requires auth settings").allowed_email_domains
    project_id: uuid.UUID | None = None
    if body.project_id is not None:
        try:
            project_id = uuid.UUID(body.project_id)
        except ValueError:
            raise project_not_found() from None
    async with runtime.transaction() as session:
        if project_id is not None and await repo.get_membership(session, user.id, project_id) is None:
            raise project_not_found()
        found = await members.suggest_users(session, user.id, body.q, domains, project_id)
    log_event(logger, INFO, "api.users.suggested", user_id=str(user.id), count=len(found))
    return SuggestionsOut(items=[SuggestionOut(name=item.name, email=item.email) for item in found])
