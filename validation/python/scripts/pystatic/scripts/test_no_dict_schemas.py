from __future__ import annotations

from pystatic.harness.docref import doc_anchor_exists
from pystatic.scripts import no_dict_schemas as rule

POSITIVE = """
from typing import Any
from pydantic import BaseModel

class UserProfileOut(BaseModel):
    id: str
    metadata: dict[str, Any]
    tags: dict[str, str]
"""

NEGATIVE = """
from pydantic import BaseModel

class TagDetail(BaseModel):
    name: str
    value: str

class UserProfileOut(BaseModel):
    id: str
    tags: TagDetail
"""


def test_positive_fixture_detects_banned_dicts() -> None:
    findings = rule.check_source("src/schemas/user.py", POSITIVE)
    assert len(findings) == 2
    assert "dict[str, Any]" in findings[0].message
    assert "dict[str, str]" in findings[1].message


def test_negative_fixture_is_clean() -> None:
    assert rule.check_source("src/schemas/user.py", NEGATIVE) == []


def test_doc_anchor_resolves() -> None:
    assert doc_anchor_exists(rule.DOC) is True


def test_syntax_error_returns_empty_findings() -> None:
    assert rule.check_source("src/schemas/bad.py", "def bad_syntax:") == []


def test_run_step_result() -> None:
    result = rule.run()
    assert result.step == rule.STEP
    assert result.exit_code == 0
