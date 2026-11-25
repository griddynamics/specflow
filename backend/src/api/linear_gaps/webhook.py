"""The Linear webhook (docs/linear/gaps.md "Webhook"): verifying a delivery and reading its event.

Linear signs the RAW body: ``Linear-Signature`` is the hex HMAC-SHA256 of it with the signing secret of the app's
webhook, and the JSON carries ``webhookTimestamp`` (milliseconds). A delivery is trusted only when the signature
matches (compared in constant time) and the timestamp is within ``TOLERANCE_MS`` of now (a replay of an old body is
refused). Nothing of the body is logged. Linear retries a delivery it did not get a 2xx for, and may send one twice,
so the route remembers the ids of the deliveries it handled (``Deliveries``; the answers are idempotent anyway).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from api.linear_gaps.answers import Incoming

SIGNATURE_HEADER: Final = "Linear-Signature"
DELIVERY_HEADER: Final = "Linear-Delivery"
TOLERANCE_MS: Final = 60_000  # Linear's own advice: reject a delivery more than a minute off
REMEMBERED: Final = 4096  # delivery ids kept
COMMENT: Final = "Comment"
OAUTH_APP: Final = "OAuthApp"
CREATE: Final = "create"
REVOKED: Final = "revoked"


class RejectedError(Exception):
    """The delivery is not trusted: the signature is missing or wrong, or the timestamp is not recent. The message is
    never shown; the route answers one closed code."""


@dataclass(frozen=True)
class Event:
    """A verified delivery: ``type`` and ``action`` as Linear names them, the workspace and the ``data`` object."""

    type: str
    action: str
    organization_id: str
    webhook_id: str
    data: Mapping[str, Any]
    user_id: str | None

    def comment(self) -> Incoming | None:
        """The comment of a ``Comment`` ``create`` event; ``None`` for any other event or a comment that is not on an
        issue (a project update or a document comment)."""

        if (self.type, self.action) != (COMMENT, CREATE):
            return None
        comment_id, issue_id, body = self.data.get("id"), self.data.get("issueId"), self.data.get("body")
        if not (isinstance(comment_id, str) and isinstance(issue_id, str) and isinstance(body, str)):
            return None
        user = self.data.get("userId")
        return Incoming(comment_id, issue_id, self.organization_id, user if isinstance(user, str) else None, body)

    def revoked(self) -> bool:
        """Whether the workspace withdrew the OAuth app's authorization."""

        return (self.type, self.action) == (OAUTH_APP, REVOKED)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def verify(raw: bytes, signature: str | None, secret: str, now_ms: int) -> Event:
    """The event of a delivery whose signature and timestamp hold; ``RejectedError`` otherwise."""

    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if signature is None or not hmac.compare_digest(expected.encode(), signature.strip().lower().encode()):
        raise RejectedError("signature")
    try:
        body = json.loads(raw)
    except ValueError:
        raise RejectedError("body") from None
    if not isinstance(body, dict):
        raise RejectedError("body")
    stamp = body.get("webhookTimestamp")
    if isinstance(stamp, bool) or not isinstance(stamp, int | float) or abs(now_ms - stamp) > TOLERANCE_MS:
        raise RejectedError("timestamp")
    data = body.get("data")
    return Event(
        _text(body.get("type")),
        _text(body.get("action")),
        _text(body.get("organizationId")),
        _text(body.get("webhookId")),
        data if isinstance(data, dict) else {},
        _text(body.get("userId")) or None,
    )


class Deliveries:
    """The ids of the last ``REMEMBERED`` deliveries handled in this process."""

    def __init__(self) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()

    def first(self, delivery: str | None) -> bool:
        """``True`` the first time this delivery is seen (a delivery without an id is always new)."""

        if not delivery:
            return True
        if delivery in self._seen:
            return False
        self._seen[delivery] = None
        while len(self._seen) > REMEMBERED:
            self._seen.popitem(last=False)
        return True
