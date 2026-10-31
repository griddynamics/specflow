"""``make env-local``: generated secrets and the LLM settings it asks for.

The target runs on a scratch env file (``ENV_FILE``), never on the repository's ``.env.local``. Without a
terminal on stdin it asks nothing; the interactive tests give it a pseudo-terminal and type the answers.
All values are synthetic.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GENERATED = (
    "POSTGRES_ADMIN_PASSWORD",
    "DATABASE_PASSWORD",
    "DATABASE_AGENT_PASSWORD",
    "NEO4J_PASSWORD",
    "AUTH_DEV_SIGNING_KEY",
    "SESSION_SECRET",
    "RUSTFS_ACCESS_KEY",
    "RUSTFS_SECRET_KEY",
    "KEYCLOAK_ADMIN_PASSWORD",
    "KEYCLOAK_QA_PASSWORD",
    "KEYCLOAK_WEB_CLIENT_SECRET",
)
INPUTS = ("AGENT_MODEL", "LLM_GATEWAY_URL", "LLM_GATEWAY_API_KEY")
COMPLETE = "".join(f"{name}=synthetic-{index}\n" for index, name in enumerate(GENERATED + INPUTS))

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="make env-local needs openssl")


def _run(env_file: Path, answers: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the target; ``answers`` are typed on a pseudo-terminal, ``None`` gives it no terminal."""

    command = [shutil.which("make") or "make", "--no-print-directory", "env-local", f"ENV_FILE={env_file}"]
    if answers is None:
        return subprocess.run(
            command, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False
        )
    leader, follower = os.openpty()
    try:
        os.write(leader, "".join(f"{answer}\n" for answer in answers).encode())
        return subprocess.run(
            command, cwd=ROOT, stdin=follower, capture_output=True, text=True, timeout=60, check=False
        )
    finally:
        os.close(follower)
        os.close(leader)


def _values(env_file: Path) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in env_file.read_text().splitlines():
        name, _, value = line.partition("=")
        values.setdefault(name, []).append(value)
    return values


def test_a_new_file_gets_every_secret_private_and_unprinted_and_names_the_llm_settings(tmp_path):
    env_file = tmp_path / "scratch.env"
    result = _run(env_file)
    assert result.returncode == 0, result.stderr
    values = _values(env_file)
    assert list(values) == list(GENERATED)
    for name, (value,) in values.items():
        assert re.fullmatch(r"[0-9a-f]{20}" if name == "RUSTFS_ACCESS_KEY" else r"[0-9a-f]{64}", value)
        assert value not in result.stdout + result.stderr
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert f"add {' '.join(INPUTS)} to {env_file}" in result.stdout


def test_an_existing_file_keeps_its_lines_and_gets_only_the_missing_or_empty_secrets(tmp_path):
    env_file = tmp_path / "scratch.env"
    kept = "DATABASE_PASSWORD=keep-me\n# a comment\nCUSTOM=1\nAGENT_MODEL=model-x\n"
    env_file.write_text(kept + 'KEYCLOAK_QA_PASSWORD=""\nSESSION_SECRET=')  # no newline at the end
    result = _run(env_file)
    assert result.returncode == 0, result.stderr
    text = env_file.read_text()
    assert text.startswith(kept)
    values = _values(env_file)
    assert values["DATABASE_PASSWORD"] == ["keep-me"] and values["AGENT_MODEL"] == ["model-x"]
    assert set(GENERATED) <= set(values)
    assert all(len(values[name]) == 1 and values[name][0] for name in GENERATED)
    appended = re.search(r"appended (.+) to ", result.stdout)
    assert appended is not None
    assert {"KEYCLOAK_QA_PASSWORD", "SESSION_SECRET"} <= set(appended.group(1).split())
    assert "DATABASE_PASSWORD" not in appended.group(1).split()
    assert "add LLM_GATEWAY_URL LLM_GATEWAY_API_KEY to" in result.stdout


def test_a_complete_file_is_left_byte_for_byte_unchanged(tmp_path):
    env_file = tmp_path / "scratch.env"
    env_file.write_text(COMPLETE)
    result = _run(env_file, answers=["never-read"])
    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == COMPLETE
    assert "complete, left unchanged" in result.stdout
    assert "Enter skips" not in result.stderr


def test_on_a_terminal_it_asks_only_for_missing_llm_settings_and_hides_the_key(tmp_path):
    env_file = tmp_path / "scratch.env"
    missing = ("AGENT_MODEL=", "LLM_GATEWAY_API_KEY=")
    env_file.write_text("".join(line for line in COMPLETE.splitlines(keepends=True) if not line.startswith(missing)))
    result = _run(env_file, answers=["model-y", "synthetic-key-value"])
    assert result.returncode == 0, result.stderr
    values = _values(env_file)
    assert values["AGENT_MODEL"] == ["model-y"] and values["LLM_GATEWAY_API_KEY"] == ["synthetic-key-value"]
    assert values["LLM_GATEWAY_URL"] == ["synthetic-12"]
    assert "AGENT_MODEL (" in result.stderr and "LLM_GATEWAY_API_KEY (input hidden" in result.stderr
    assert "LLM_GATEWAY_URL (" not in result.stderr
    assert "synthetic-key-value" not in result.stdout + result.stderr
    assert "set AGENT_MODEL LLM_GATEWAY_API_KEY in" in result.stdout


def test_an_empty_answer_skips_and_an_empty_line_is_replaced_not_duplicated(tmp_path):
    env_file = tmp_path / "scratch.env"
    base = "".join(line for line in COMPLETE.splitlines(keepends=True) if not line.startswith(INPUTS))
    env_file.write_text(base + "AGENT_MODEL=\n")
    result = _run(env_file, answers=["model-z", "", ""])
    assert result.returncode == 0, result.stderr
    values = _values(env_file)
    assert values["AGENT_MODEL"] == ["model-z"]
    assert "LLM_GATEWAY_URL" not in values and "LLM_GATEWAY_API_KEY" not in values
    assert "add LLM_GATEWAY_URL LLM_GATEWAY_API_KEY to" in result.stdout


def test_an_answer_with_special_characters_is_single_quoted_and_a_single_quote_is_refused(tmp_path):
    env_file = tmp_path / "scratch.env"
    base = "".join(line for line in COMPLETE.splitlines(keepends=True) if not line.startswith(INPUTS))
    env_file.write_text(base)
    result = _run(env_file, answers=["model-q", "https://gateway.example.com/v1?a=$HOME #x", "it's"])
    assert result.returncode == 0, result.stderr
    values = _values(env_file)
    assert values["LLM_GATEWAY_URL"] == ["'https://gateway.example.com/v1?a=$HOME #x'"]
    assert "LLM_GATEWAY_API_KEY" not in values
    assert "LLM_GATEWAY_API_KEY holds a single quote" in result.stderr
    assert "it's" not in result.stdout + result.stderr
