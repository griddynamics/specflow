"""The account the Jira gap channel acts as: the member the project's Atlassian is connected by (R1), with that member's
gateway and Atlassian account id. Never a token or an id in an error; ``code`` is a closed Jira reason."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

from db import integrations as integrations_db
from db import jira_gaps as gaps_db
from db.integrations import ATLASSIAN
from jira.gateway import JiraGateway
from jira.reasons import Reason

UNAVAILABLE: Final = "jira_unavailable"  # no Atlassian OAuth on this server


class ActingError(Exception):
    """The project has no member to act as; ``code`` is ``jira_not_connected`` or ``jira_unavailable``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Acting:
    user_id: uuid.UUID
    account_id: str
    gateway: JiraGateway


async def acting_gateway(runtime: Any, project_id: uuid.UUID) -> Acting:
    """The bound member's gateway and account id; ``ActingError`` without Atlassian OAuth, a binding or a connection,
    ``JiraError`` (``jira_reconnect_required``) when their connection expired."""

    jira = runtime.jira
    if jira is None or jira.connections is None:
        raise ActingError(UNAVAILABLE)
    async with runtime.transaction() as session:
        bound = await integrations_db.get_integration(session, project_id, ATLASSIAN)
        member = None if bound is None else bound.user_id
        account = None if member is None else await gaps_db.account_of(session, member)
    if member is None:
        raise ActingError(Reason.NOT_CONNECTED.value)
    if account is None:
        raise ActingError(Reason.NOT_CONNECTED.value)
    return Acting(member, account, await jira.gateway_for(member))
