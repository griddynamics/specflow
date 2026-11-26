"""What the Linear importer and its routes share (docs/sources/importers.md): the member the project's Linear is
connected by, a gateway acting as that member, and the closed outcomes of a refused read.

The acting account is the member bound to the project (R1), never the caller; the import runs in this process with that
member's tokens. The token refresh (one try, then ``LinearUnauthorizedError``) lives in the gateway the connections
build, so an importer sees only the final refusal. Fixed messages only: nothing Linear says reaches an error or a log.
"""

from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Final, Protocol

from api.imports.model import ImportFailedError
from api.linear.errors import linear_connections, linear_error, linear_not_connected
from api.linear.port import LinearConnectionView
from api.project_integrations import acting_member
from api.runtime import Runtime
from db.integrations import LINEAR
from linear.errors import LinearError, LinearForbiddenError, LinearNotFoundError, LinearUnauthorizedError
from linear.gateway import LinearGateway

NO_ACCESS: Final = "no_access"  # the closed reason of an item or a source the member cannot read
UNKNOWN: Final = "unknown_error"
READ_STEP: Final = "read"
_NO_ACCESS: Final = (LinearUnauthorizedError, LinearForbiddenError, LinearNotFoundError)


class LinearGateways(Protocol):
    """The part of the member connections (``runtime.linear``, stream S1) the importer uses: a connection view, and a
    gateway acting as one member (it refreshes the token once on a refusal; the caller closes it)."""

    async def get(self, user_id: uuid.UUID) -> LinearConnectionView | None: ...

    def gateway(self, user_id: uuid.UUID) -> AbstractAsyncContextManager[LinearGateway]: ...


@dataclass(frozen=True)
class LinearAccess:
    """The member a project's Linear acts as and what the reads of theirs need."""

    user_id: uuid.UUID
    connections: LinearGateways

    def gateway(self) -> AbstractAsyncContextManager[LinearGateway]:
        return self.connections.gateway(self.user_id)


async def project_access(runtime: Runtime, project_id: uuid.UUID) -> LinearAccess:
    """The access of the member the project's Linear is connected by; ``ApiError`` when there is none to use
    (``linear_unavailable`` when the integration is off, ``linear_not_connected``, ``linear_reconnect_required``)."""

    connections: LinearGateways = linear_connections(runtime)
    member = await acting_member(runtime, project_id, LINEAR)
    if member is None:
        raise linear_not_connected()
    view = await connections.get(member)
    if view is None:
        raise linear_not_connected()
    if view.status != "active":
        raise linear_error(LinearUnauthorizedError())
    return LinearAccess(member, connections)


def import_failure(error: LinearError) -> ImportFailedError:
    """The closed ``connections.failure_reason`` of an import a read stopped: no access, else unknown."""

    return ImportFailedError(NO_ACCESS if isinstance(error, _NO_ACCESS) else UNKNOWN)
