"""The closed codes of the Box integration (docs/box/codes.md). ``str(error)`` is the code: no Box text, token, code,
``state``, link, file name or e-mail is ever inside an error."""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class BoxCode(StrEnum):
    NOT_CONNECTED = "box_not_connected"
    RECONNECT_REQUIRED = "box_reconnect_required"
    STATE_INVALID = "box_state_invalid"
    CODE_INVALID = "box_code_invalid"
    UNREACHABLE = "box_unreachable"  # Box is down, slow, throttled past the wait budget, or the network failed
    AUTH_FAILED = "box_auth_failed"  # Box refused the app's own credentials or a fresh token (a setup problem)
    LINK_INVALID = "box_link_invalid"  # not a folder, file or shared link on app.box.com; All Files; a web link
    LINK_PROTECTED = "box_link_password"  # a password-protected shared link (the check refuses it)
    NO_ACCESS = "box_no_access"  # the member's Box account cannot read the item (not found or forbidden)
    # Inside only: a dead refresh token (``invalid_grant``, a rotated key) becomes RECONNECT_REQUIRED; any other
    # refusal of the token endpoint becomes CODE_INVALID (sign-in) or AUTH_FAILED (refresh). A 401 on a read is
    # ACCESS_REFUSED: the caller refreshes once and asks again. NOT_READY is a download that stayed ``202``.
    GRANT_INVALID = "box_grant_invalid"
    REFUSED = "box_refused"
    ACCESS_REFUSED = "box_token_refused"
    NOT_READY = "box_not_ready"


# The codes a check or add route may answer for a link (docs/box/codes.md "Source routes"); a test keeps the API
# errors (``api.box``) equal to the union of these and the connection codes.
LINK_CODES: Final = (
    BoxCode.LINK_INVALID.value,
    BoxCode.LINK_PROTECTED.value,
    BoxCode.NO_ACCESS.value,
)

# The closed ``connections.failure_reason`` values a Box import ends with, and the item reasons and steps it writes
# (all are in ``db.models``; Box adds none, AC24).
SOURCE_FAILURE_REASONS: Final = ("no_access", "unknown_error")
ITEM_ERROR_REASONS: Final = ("no_access", "convert_failed", "unknown_error")
ITEM_ERROR_STEPS: Final = ("read", "download", "convert")


class BoxError(Exception):
    """A Box failure as one closed code; ``str(e)`` is the code."""

    def __init__(self, code: BoxCode) -> None:
        super().__init__(code.value)
        self.code = code
