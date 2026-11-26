"""The closed codes of the Dropbox integration (docs/dropbox/codes.md). ``str(error)`` is the code: no Dropbox text,
token, code, ``state``, link, path, file name or e-mail is ever inside an error."""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class DropboxCode(StrEnum):
    NOT_CONNECTED = "dropbox_not_connected"
    RECONNECT_REQUIRED = "dropbox_reconnect_required"
    STATE_INVALID = "dropbox_state_invalid"
    CODE_INVALID = "dropbox_code_invalid"
    UNREACHABLE = "dropbox_unreachable"  # Dropbox is down, slow, throttled past the wait budget, or the network failed
    AUTH_FAILED = "dropbox_auth_failed"  # Dropbox refused the app's own credentials or a fresh token (a setup problem)
    # not a Dropbox shared link or /home path on www.dropbox.com; a password link; the Dropbox root; another host
    LINK_INVALID = "dropbox_link_invalid"
    # the bound member's Dropbox cannot read it, or it is not in their Dropbox (no ``path_lower``): add it first
    NO_ACCESS = "dropbox_no_access"
    # Inside only: a dead refresh token (``invalid_grant``) or one that no longer decrypts becomes RECONNECT_REQUIRED;
    # any other refusal of the token endpoint becomes CODE_INVALID (sign-in) or AUTH_FAILED (refresh). A 401 on a call
    # (``expired_access_token``, ``invalid_access_token``) is ACCESS_REFUSED: the caller refreshes once and asks again.
    # INVALID_ROOT is a 422 ``invalid_root`` for the Path-Root header: the caller reads the root namespace once more.
    # NOT_FOUND is a 409 whose error names ``not_found`` (or ``restricted_content``/``no_permission``: RESTRICTED);
    # UNSUPPORTED is a 409 ``unsupported_file``/``non_exportable``/``invalid_export_format``.
    GRANT_INVALID = "dropbox_grant_invalid"
    REFUSED = "dropbox_refused"
    ACCESS_REFUSED = "dropbox_token_refused"
    INVALID_ROOT = "dropbox_invalid_root"
    NOT_FOUND = "dropbox_not_found"
    RESTRICTED = "dropbox_restricted"
    UNSUPPORTED = "dropbox_unsupported"


# The codes a check or add route may answer for a link (docs/dropbox/codes.md "Source routes"); a test keeps the API
# errors (``api.dropbox``) equal to the union of these and the connection codes.
LINK_CODES: Final = (DropboxCode.LINK_INVALID.value, DropboxCode.NO_ACCESS.value)

# The closed ``connections.failure_reason`` values a Dropbox import ends with, and the item reasons and steps it writes
# (all are in ``db.models``; Dropbox adds none, AC24).
SOURCE_FAILURE_REASONS: Final = ("no_access", "unknown_error")
ITEM_ERROR_REASONS: Final = ("no_access", "convert_failed", "unknown_error")
ITEM_ERROR_STEPS: Final = ("read", "download", "convert")


class DropboxError(Exception):
    """A Dropbox failure as one closed code; ``str(e)`` is the code."""

    def __init__(self, code: DropboxCode) -> None:
        super().__init__(code.value)
        self.code = code
