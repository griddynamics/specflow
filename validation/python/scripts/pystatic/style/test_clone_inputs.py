"""Assertion oracles are independent; setup and mutations still enter clone detection."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from pystatic.style.clone_inputs import assertion_lines, import_lines, main


def test_import_lines_cover_multi_line_imports_only() -> None:
    source = "from os import (\n    path,\n    sep,\n)\nimport sys\nassert sys.argv\n"
    assert import_lines(source) == {1, 2, 3, 4, 5}


def test_assertion_helpers_are_oracles_but_mutating_arguments_remain_compared() -> None:
    source = """from support import assert_error_envelope
result = client.post('/records', json=payload)
assert_error_envelope(
    result,
    422,
    'VALIDATION',
)
assert result.status_code == 422
assert_error_envelope(client.post('/records'), 422, 'VALIDATION')
"""
    assert assertion_lines(source) == {1, 3, 4, 5, 6, 7, 8}


def test_assignments_and_non_assertion_calls_remain_compared() -> None:
    source = """fixture = {'expected': 3}
actual = calculate(fixture)
record_result(actual)
assert actual == fixture['expected']
"""
    assert assertion_lines(source) == {4}


def test_native_asserts_preserve_effects_and_omit_only_value_oracles() -> None:
    source = """assert admin.post('/records', json=payload).status_code == 201
assert await _write(rows) == expected
assert _publish(candidate) is not None
assert (saved := response) is not None
assert cache.get(key) == expected
assert response.status_code == 200, create_message()
assert response.status_code == 200
assert len(records) == 2
assert set(ids) == {'a', 'b'}
"""
    assert assertion_lines(source) == {7, 8, 9}


def test_shadowed_intrinsics_and_unknown_helper_calls_remain_compared() -> None:
    source = """from storage import len
assert len(records) == 2
assert_error_envelope(cache.get(key), 200, 'OK')
assert_error_envelope(result, 422, 'VALIDATION')
"""
    assert assertion_lines(source) == {1, 4}


def test_relative_path_resolves_against_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "t.py").write_text("import os\nx = 1\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(["t.py"])))
    assert main(["--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"t.py": [1]}


def test_unreadable_path_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(["missing.py"])))
    assert main(["--root", str(tmp_path)]) == 2
    assert "missing.py:1:1: clone-inputs-unreadable" in capsys.readouterr().out
