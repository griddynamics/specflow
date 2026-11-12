"""``refers_to`` validation, the dedupe tuple and the other argument checks of the findings library.

FR-GAP-05: a bad value raises ``invalid_ref`` before any query, and no value is ever fetched or opened.
"""

from __future__ import annotations

import logging
import socket
from typing import Any, cast

import pytest

from gap_support import Clock, MemoryGapExecutor, spec_graph
from graph.core.errors import ConfigurationError, GraphError
from graph.core.ontology import load_ontology
from graph.gap import FindingRef, Findings
from graph.gap import arguments as check
from graph.gap.names import RefType

GOOD: list[tuple[str, str, str]] = [
    ("pr", "42", "42"),
    (
        "pr",
        "https://github.com/acme/shop/pull/42",
        "https://github.com/acme/shop/pull/42",
    ),
    ("pr", "http://git.example.com/p/1", "http://git.example.com/p/1"),
    ("commit", "ABCDEF1", "abcdef1"),
    (
        "commit",
        "0123456789abcdef0123456789abcdef01234567",
        "0123456789abcdef0123456789abcdef01234567",
    ),
    ("commit", "f" * 64, "f" * 64),
    ("file", "backend/src/graph/gap/findings.py", "backend/src/graph/gap/findings.py"),
    ("file", "docs/a b.md", "docs/a b.md"),
    ("file", "..hidden/file", "..hidden/file"),
    ("url", "https://example.com/spec?id=1#part", "https://example.com/spec?id=1#part"),
    ("url", "https://[::1]:8443/x", "https://[::1]:8443/x"),
]
BAD: list[tuple[str, str]] = [
    ("pr", ""),
    ("pr", "0x2a"),
    ("pr", "4" * 10),
    ("pr", "٤٢"),  # Arabic-Indic digits are not a PR number
    ("pr", "ftp://example.com/p/1"),
    ("pr", "https://user:secret@github.com/acme/shop/pull/1"),
    ("commit", "abcdef"),  # 6 hex
    ("commit", "g" * 7),
    ("commit", "a" * 65),
    ("commit", "abc def1"),
    ("file", ""),
    ("file", "/etc/passwd"),
    ("file", "../secret"),
    ("file", "a/../b"),
    ("file", "a/b/.."),
    ("file", "a\\b"),
    ("file", "a\x00b"),
    ("file", "a\nb"),
    ("file", "a" * 501),
    ("file", "C:/Windows/system32"),
    ("url", ""),
    ("url", "example.com/page"),
    ("url", "javascript:alert(1)"),
    ("url", "file:///etc/passwd"),
    ("url", "https:///nohost"),
    ("url", "https://user@example.com/"),
    ("url", "https://user:pass@example.com/"),
    ("url", "https://exa mple.com/"),
    ("url", "https://example.com/\nx"),
    ("url", "https://example.com/" + "a" * 2000),
    ("url", "https://[::1/"),
    ("issue", "12"),
]


@pytest.mark.parametrize(("kind", "value", "stored"), GOOD)
def test_a_valid_reference_is_stored_in_its_normal_form(kind: str, value: str, stored: str) -> None:
    assert check.references([(kind, value)]) == (FindingRef(type=cast(RefType, kind), value=stored),)


@pytest.mark.parametrize(("kind", "value"), BAD)
def test_an_invalid_reference_is_refused(kind: str, value: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        check.references([(kind, value)])
    assert caught.value.code == "invalid_ref"
    assert value[:20] not in str(caught.value) or not value  # the value is not echoed


def test_the_length_limits_are_inclusive() -> None:
    assert check.references([("file", "a" * 500)])
    assert check.references([("url", "https://example.com/" + "a" * (2000 - 20))])
    assert len(check.references([("commit", f"{n:07x}") for n in range(10)])) == 10


def test_duplicate_references_collapse_after_normalising() -> None:
    assert check.references([("commit", "ABCDEF1"), ("commit", "abcdef1")]) == (
        FindingRef(type="commit", value="abcdef1"),
    )


def test_the_dedupe_key_ignores_order_case_spacing_and_width_but_not_author_kind_or_ids() -> None:
    base = check.dedupe_key("u1", "gap", ["B", "A"], "The  Spec\tis silent")
    assert base == check.dedupe_key("u1", "gap", ["A", "B", "A"], " the spec is silent ")
    assert base == check.dedupe_key(
        "u1", "gap", ["A", "B"], "\uff34\uff28\uff25 \uff33\uff30\uff25\uff23 is silent"
    )  # NFKC
    assert len(base) == 64
    assert base != check.dedupe_key("u2", "gap", ["A", "B"], "the spec is silent")
    assert base != check.dedupe_key("u1", "question", ["A", "B"], "the spec is silent")
    assert base != check.dedupe_key("u1", "gap", ["A"], "the spec is silent")


@pytest.fixture
def guarded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every attempt to open a socket, resolve a name or start a process is recorded and refused."""

    attempts: list[str] = []

    def refuse(name: str) -> Any:
        def blocked(*_args: Any, **_kwargs: Any) -> Any:
            attempts.append(name)
            raise AssertionError(f"{name} is not allowed")

        return blocked

    for name in ("create_connection", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, name, refuse(f"socket.{name}"))
    monkeypatch.setattr(socket.socket, "connect", refuse("socket.connect"))
    return attempts


async def test_a_reference_is_never_fetched(guarded: list[str]) -> None:
    # Scenario: add and refresh findings citing a url and a pr URL under a guard that fails on any network use.
    executor = spec_graph(MemoryGapExecutor())
    findings = Findings(executor, load_ontology(), clock=Clock())

    added = await findings.add(
        "shop",
        created_by="u1",
        kind="gap",
        text="See the pages.",
        about=["REQ::FR::CARD_PAYMENT"],
        refers_to=[
            ("url", "https://example.com/spec"),
            ("pr", "https://github.com/acme/shop/pull/7"),
            ("pr", "7"),
        ],
    )
    await findings.refresh("shop")

    assert len(added.finding.refers_to) == 3 and guarded == []


async def test_no_text_note_or_reference_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Scenario: add, repeat, resolve, dismiss, withdraw, refresh and a refused call, with a canary in each free-text
    # field; every log record and every error message is searched for the canaries.
    executor = spec_graph(MemoryGapExecutor())
    findings = Findings(executor, load_ontology(), clock=Clock())
    text, note = "CANARY-TEXT-7f3a spec says nothing", "CANARY-NOTE-91bc"
    refs = [
        ("url", "https://example.com/CANARY-URL-55d0"),
        ("file", "src/CANARY-FILE-02e8.py"),
    ]
    about = ["REQ::FR::CARD_PAYMENT"]
    messages: list[str] = []
    with caplog.at_level(logging.DEBUG):
        first = (
            await findings.add(
                "shop",
                created_by="u1",
                kind="gap",
                text=text,
                about=about,
                refers_to=refs,
            )
        ).finding
        await findings.add("shop", created_by="u1", kind="gap", text=text, about=about)
        other = (await findings.add("shop", created_by="u1", kind="note", text=text, about=about)).finding
        third = (await findings.add("shop", created_by="u1", kind="question", text=text, about=about)).finding
        await findings.resolve("shop", first.finding_id, by="u9", note=note)
        await findings.dismiss("shop", other.finding_id, by="u9", note=note)
        await findings.withdraw("shop", third.finding_id)
        await findings.refresh("shop")
        for failing in (
            findings.add("shop", created_by="u1", kind="gap", text=text, about=["REQ::CANARY-ID"]),
            findings.add(
                "shop",
                created_by="u1",
                kind="gap",
                text=text,
                about=about,
                refers_to=[("url", "CANARY-URL-1 x")],
            ),
            findings.resolve("shop", first.finding_id, by="u9", note=note),
            findings.resolve("shop", "FND::NONE", by="u9", note=note),
        ):
            try:
                await failing
            except GraphError as error:
                messages.append(str(error))

    output = "\n".join(
        [
            *(record.getMessage() for record in caplog.records),
            *(str(r.__dict__) for r in caplog.records),
            *messages,
        ]
    )
    assert [r.getMessage() for r in caplog.records if r.name.startswith("graph.gap")]  # events were emitted
    for canary in ("CANARY-TEXT", "CANARY-NOTE", "CANARY-URL", "CANARY-FILE"):
        assert canary not in output
    events = {record.getMessage() for record in caplog.records if record.name.startswith("graph.gap")}
    assert {
        "graph.gap.finding_added",
        "graph.gap.finding_closed",
        "graph.gap.finding_withdrawn",
        "graph.gap.findings_refreshed",
    } <= events
