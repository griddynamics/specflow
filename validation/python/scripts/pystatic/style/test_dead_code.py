"""Dead implementations fail while framework registrations remain live."""

from __future__ import annotations

from pathlib import Path

import pytest

from pystatic.style.dead_code import check_project, check_sources


def test_unused_function_is_a_finding(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("def abandoned():\n    return 42\n")
    assert any("abandoned" in finding for finding in check_sources([source], []))


def test_consumer_keeps_a_tool_helper_live(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("def helper():\n    return 42\n")
    consumer = tmp_path / "test_source.py"
    consumer.write_text("from source import helper\nassert helper() == 42\n")
    assert not check_sources([source], [consumer])


def test_route_registration_keeps_handler_but_not_unused_body_live(tmp_path: Path) -> None:
    source = tmp_path / "routes.py"
    source.write_text('@router.get("/")\ndef list_rows():\n    abandoned = 42\n    return []\n')
    findings = check_sources([source], [])
    assert any("abandoned" in finding for finding in findings)
    assert not any("list_rows" in finding for finding in findings)


def test_protocol_arguments_and_pydantic_fields_are_contracts(tmp_path: Path) -> None:
    source = tmp_path / "models.py"
    source.write_text(
        "from typing import Protocol\nfrom pydantic import BaseModel\n"
        "class Dispatch(Protocol):\n    def call(self, function: str) -> int: ...\n"
        "class Output(BaseModel):\n    label: str\n"
        "print(Dispatch, Output)\n"
    )
    assert not check_sources([source], [])


def test_empty_input_is_an_error() -> None:
    with pytest.raises(ValueError, match="no owned Python"):
        check_sources([], [])


def test_owned_unused_attribute_fails_but_external_effect_does_not(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text(
        "class Handler:\n    def __init__(self):\n        self.abandoned = 42\n"
        "client.request_timeout = 5\nprint(Handler())\n"
    )
    findings = check_sources([source], [])
    assert any("abandoned" in finding for finding in findings)
    assert not any("request_timeout" in finding for finding in findings)


def test_owned_support_checks_helpers_but_preserves_pytest_registration(tmp_path: Path) -> None:
    source = tmp_path / "test_support.py"
    source.write_text(
        "import pytest\n"
        "def abandoned_arrangement():\n    return 7\n"
        "def used_arrangement():\n    return 42\n"
        "@pytest.fixture\ndef arranged_value():\n    return used_arrangement()\n"
        "@pytest.hookimpl\ndef pytest_collection_modifyitems(items):\n    print(items)\n"
        "def test_behavior(arranged_value):\n    assert arranged_value == 42\n"
    )
    findings = check_sources([source], [])
    assert len(findings) == 1
    assert "abandoned_arrangement" in findings[0]


def test_owned_test_scope_does_not_keep_production_only_code_alive(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("def only_tests_call_this():\n    return 42\n")
    support = tmp_path / "test_source.py"
    support.write_text(
        "from source import only_tests_call_this\n"
        "def abandoned_support():\n    return 7\n"
        "def used_support():\n    return 42\n"
        "def test_behavior():\n    assert used_support() == only_tests_call_this()\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[[tool.dead-code.scopes]]\nsources = ["source.py"]\n'
        '[[tool.dead-code.scopes]]\nsources = ["test_source.py"]\n'
        'consumers = ["source.py"]\n'
    )
    findings = check_project(tmp_path)
    assert len(findings) == 2
    assert any("only_tests_call_this" in finding for finding in findings)
    assert any("abandoned_support" in finding for finding in findings)


def test_finding_renders_path_line_rule_and_message(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("def abandoned():\n    return 42\n")
    [finding] = check_sources([source], [])
    prefix = f"{source.resolve()}:1:1: vulture-dead-code: unused function 'abandoned'"
    assert finding.startswith(prefix)
