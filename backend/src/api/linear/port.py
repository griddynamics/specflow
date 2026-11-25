"""What the routers and runners of the API read from ``runtime.linear`` (the member connections, built by stream S1):
the integrations router asks whether a member has an active connection and in which workspace."""

from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from linear.gateway import LinearGateway


class LinearConnectionView(Protocol):
    """A member's connection as the API shows it: no token."""

    @property
    def status(self) -> str:  # active | expired | revoked
        ...

    @property
    def linear_user_id(self) -> str: ...

    @property
    def organization_id(self) -> str: ...

    @property
    def organization_name(self) -> str: ...


class LinearConnectionsPort(Protocol):
    async def get(self, user_id: uuid.UUID) -> LinearConnectionView | None: ...

    def gateway(self, user_id: uuid.UUID) -> AbstractAsyncContextManager[LinearGateway]:
        """A gateway that acts as the member: the token is read, and refreshed under the shared lock, on every call.
        ``LinearUnauthorizedError`` (from the call) when the member has no active connection."""
