"""``auth.emails``: the one email rule (docs/projects/api.md "Email rules") and the Create Project list check."""

from __future__ import annotations

import pytest

from auth.emails import (
    EMAIL_DOMAIN_NOT_ALLOWED,
    INVALID_EMAIL,
    TOO_LONG,
    InvalidCollaboratorsError,
    check_collaborators,
    domain_allowed,
    normalise_email,
)

DOMAINS = frozenset({"example.com"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("bob@example.com", "bob@example.com"),
        ("  Bob@Example.COM \n", "bob@example.com"),
        ("a@b", "a@b"),
        ("x" * 242 + "@example.com", "x" * 242 + "@example.com"),  # exactly 254
        ("x" * 243 + "@example.com", None),  # 255
        ("", None),
        ("   ", None),
        ("no-at-sign", None),
        ("two@@example.com", None),
        ("a@b@example.com", None),
        ("@example.com", None),
        ("bob@", None),
        ("bo b@example.com", None),
        ("bob@exa\tmple.com", None),
        ("bob\x00@example.com", None),
        ("bob\x7f@example.com", None),
        ("bob\u00a0x@example.com", None),  # a no-break space is whitespace inside the address
    ],
)
def test_normalise_email_table(raw: str, expected: str | None) -> None:
    assert normalise_email(raw) == expected


@pytest.mark.parametrize(
    ("email", "domains"),
    [
        ("bob@example.com", {"example.com"}),
        ("bob@EXAMPLE.com", {"example.com"}),
        ("bob@example.com", {"Example.COM"}),
        ("bob@example.com", {"other.org", "example.com"}),
    ],
)
def test_domain_allowed_accepts(email: str, domains: set[str]) -> None:
    assert domain_allowed(email, domains)


@pytest.mark.parametrize(
    ("email", "domains"),
    [
        ("bob@sub.example.com", {"example.com"}),  # exact, no subdomains
        ("bob@example.com.evil.org", {"example.com"}),
        ("bob@other.org", {"example.com"}),
        ("bob@example.com", set()),
    ],
)
def test_domain_allowed_refuses(email: str, domains: set[str]) -> None:
    assert not domain_allowed(email, domains)


def test_check_collaborators_normalises_dedupes_and_drops_the_creator() -> None:
    kept = check_collaborators(
        [" Ann@Example.com", "ann@example.com", "Me@example.com", "bo@example.com"], DOMAINS, "ME@Example.com"
    )
    assert kept == ["ann@example.com", "bo@example.com"]


def test_check_collaborators_empty_is_empty() -> None:
    assert check_collaborators([], DOMAINS, "me@example.com") == []


def test_check_collaborators_names_every_failing_index_and_type() -> None:
    with pytest.raises(InvalidCollaboratorsError) as raised:
        check_collaborators(["ok@example.com", "bad", "a@other.org", "also bad@example.com"], DOMAINS, "me@example.com")
    assert [(e.index, e.type) for e in raised.value.errors] == [
        (1, INVALID_EMAIL),
        (2, EMAIL_DOMAIN_NOT_ALLOWED),
        (3, INVALID_EMAIL),
    ]


def test_check_collaborators_allows_49_and_refuses_50_as_a_list_error() -> None:
    forty_nine = [f"u{i}@example.com" for i in range(49)]
    assert len(check_collaborators(forty_nine, DOMAINS, "me@example.com")) == 49
    with pytest.raises(InvalidCollaboratorsError) as raised:
        check_collaborators([*forty_nine, "u49@example.com"], DOMAINS, "me@example.com")
    assert [(e.index, e.type) for e in raised.value.errors] == [(None, TOO_LONG)]
