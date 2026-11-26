"""The Linear gateway of a pass: the member the project's Linear is bound to acts, for every pass (rule R1).

``GatewayFactory`` is what the host provides (``runtime.linear.gateway(user_id)``: an async context manager of a
gateway with a fresh token for one member; it closes the client and renews a refused token once;
``LinearUnauthorizedError`` when the connection cannot act). A project with no binding is ``NotConnectedError``."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from linear.gateway import LinearGateway

GatewayFactory = Callable[[uuid.UUID], AbstractAsyncContextManager[LinearGateway]]


class NotConnectedError(Exception):
    """The project has no Linear binding (or its member is gone): no Linear call is made."""
