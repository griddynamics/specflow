"""The Linear webhook (``docs/linear/gaps.md`` "Webhook"): ``POST /integrations/linear/webhook``, a public route
(Linear cannot sign in) verified by the signature of the raw body.

The route reads the RAW body (the signature is over its bytes), verifies it, answers at once and hands the event to
a background task (Linear gives a delivery five seconds). ``503 linear_webhook_unavailable`` while the signing
secret is not set; ``401 linear_webhook_signature_invalid`` for a missing or wrong signature or a timestamp more
than a minute off. Nothing of the body is logged.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request

from api.deps import RuntimeDep
from api.errors import ApiError
from api.linear_gaps.channel import LinearGaps, revoke_organization
from api.linear_gaps.models import WebhookAck
from api.linear_gaps.webhook import DELIVERY_HEADER, SIGNATURE_HEADER, Event, RejectedError, verify
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("api.routers.linear_webhook")
router = APIRouter(prefix="/integrations/linear", tags=["linear"])


def webhook_secret(runtime: Any) -> str:
    """The signing secret of the app's webhook; ``503 linear_webhook_unavailable`` while it is not set."""

    linear = getattr(getattr(runtime, "settings", None), "linear", None)
    secret = None if linear is None else linear.webhook_secret
    if secret is None:
        raise ApiError(503, "linear_webhook_unavailable", "The Linear webhook is not configured on this server.")
    return str(secret.get_secret_value())


def _event(raw: bytes, request: Request, secret: str) -> Event:
    try:
        return verify(raw, request.headers.get(SIGNATURE_HEADER), secret, int(time.time() * 1000))
    except RejectedError as error:
        log_event(logger, WARNING, "api.linear_webhook.rejected", reason=str(error))
        raise ApiError(
            401, "linear_webhook_signature_invalid", "The webhook signature is missing, wrong or too old."
        ) from None


@router.post("/webhook", response_model=WebhookAck)
async def receive_linear_webhook(request: Request, runtime: RuntimeDep, background: BackgroundTasks) -> WebhookAck:
    raw = await request.body()
    event = _event(raw, request, webhook_secret(runtime))
    log_event(logger, INFO, "api.linear_webhook.received", event_type=event.type, action=event.action)
    channel: LinearGaps | None = runtime.linear_gaps
    if channel is not None:
        channel.receive(event, request.headers.get(DELIVERY_HEADER))  # reads a comment in the background
    if event.revoked():
        background.add_task(revoke_organization, runtime, event)
    return WebhookAck(ok=True)
