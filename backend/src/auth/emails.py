"""The one email rule shared by sign-in (``auth.claims``) and the collaborator routes.

Framework-free: this module imports nothing but the standard library, so the MCP role that loads ``auth`` never
pulls FastAPI or a database in (``docs/mcp-server/auth.md``, "Token validation"). The rules are in
``docs/projects/api.md#email-rules``.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final

EMAIL_MAX_LENGTH: Final = 254
COLLABORATORS_MAX_AT_CREATE: Final = 49

INVALID_EMAIL: Final = "invalid_email"
EMAIL_DOMAIN_NOT_ALLOWED: Final = "email_domain_not_allowed"
TOO_LONG: Final = "too_long"


def normalise_email(raw: str) -> str | None:
    """Strip and lower-case ``raw``; ``None`` when it is not an address.

    An address has exactly one ``@``, a non-empty local part and domain, no whitespace or control character and at
    most 254 characters.
    """

    email = raw.strip().lower()
    if not email or len(email) > EMAIL_MAX_LENGTH or email.count("@") != 1:
        return None
    local, _, domain = email.partition("@")
    if not local or not domain:
        return None
    if any(char.isspace() or unicodedata.category(char) == "Cc" for char in email):
        return None
    return email


def domain_allowed(email: str, domains: Collection[str]) -> bool:
    """True when the domain of ``email`` (the text after its ``@``) equals one of ``domains``.

    Exact match, no subdomains, case-insensitive: the sign-in rule. ``email`` must hold an ``@``.
    """

    domain = email.partition("@")[2].lower()
    return domain in {allowed.lower() for allowed in domains}


@dataclass(frozen=True)
class CollaboratorError:
    """One failed rule of ``check_collaborators``: ``index`` is the position in the submitted list (``None`` for the
    list as a whole) and ``type`` one of ``invalid_email``, ``email_domain_not_allowed``, ``too_long``."""

    index: int | None
    type: str


class InvalidCollaboratorsError(Exception):
    """Create Project refused its collaborator list; nothing is written. ``errors`` names every failure."""

    def __init__(self, errors: Sequence[CollaboratorError]) -> None:
        super().__init__("invalid collaborator list")
        self.errors = tuple(errors)


def check_collaborators(emails: Sequence[str], domains: Collection[str], creator_email: str) -> list[str]:
    """The normalised, de-duplicated emails of a Create Project request, or ``InvalidCollaboratorsError``.

    All-or-nothing (api.md "Create with collaborators"): more than 49 emails is ``too_long`` for the list; every
    other failure is reported with its index. Duplicates (case-insensitive) and the creator's own email are dropped
    after the checks, keeping the first spelling's position.
    """

    if len(emails) > COLLABORATORS_MAX_AT_CREATE:
        raise InvalidCollaboratorsError([CollaboratorError(None, TOO_LONG)])
    errors: list[CollaboratorError] = []
    kept: dict[str, None] = {}
    own = normalise_email(creator_email)
    for index, raw in enumerate(emails):
        email = normalise_email(raw)
        if email is None:
            errors.append(CollaboratorError(index, INVALID_EMAIL))
        elif not domain_allowed(email, domains):
            errors.append(CollaboratorError(index, EMAIL_DOMAIN_NOT_ALLOWED))
        elif email != own:
            kept[email] = None
    if errors:
        raise InvalidCollaboratorsError(errors)
    return list(kept)
