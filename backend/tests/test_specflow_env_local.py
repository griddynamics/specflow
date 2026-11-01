"""``make env-local``: generated secrets, the personal sign-in identity and the LLM settings it asks for.

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
    "SESSION_SECRET",
    "RUSTFS_ACCESS_KEY",
    "RUSTFS_SECRET_KEY",
    "KEYCLOAK_ADMIN_PASSWORD",
    "KEYCLOAK_QA_PASSWORD",
    "KEYCLOAK_WEB_CLIENT_SECRET",
    "LOCAL_TEST_USER_PASSWORD",
    "LOCAL_TEST_USER_ID",
)
IDENTITY = ("LOCAL_TEST_USER_EMAIL", "AUTH_ALLOWED_EMAIL_DOMAINS")
INPUTS = ("AGENT_MODEL", "LLM_GATEWAY_URL", "LLM_GATEWAY_API_KEY")
COMPLETE = "".join(f"{name}=synthetic-{index}\n" for index, name in enumerate(GENERATED + INPUTS))
COMPLETE += "LOCAL_TEST_USER_EMAIL=me@corp.io\nAUTH_ALLOWED_EMAIL_DOMAINS=corp.io,example.com\n"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
GIT_EMAIL = "Git.User@Git-Corp.io"
# git's own environment config beats the repository and global files, so the default email is the same everywhere.
GIT_ENV = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.email", "GIT_CONFIG_VALUE_0": GIT_EMAIL}

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="make env-local needs openssl")


def _run(env_file: Path, answers: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the target; ``answers`` are typed on a pseudo-terminal, ``None`` gives it no terminal."""

    command = [shutil.which("make") or "make", "--no-print-directory", "env-local", f"ENV_FILE={env_file}"]
    if answers is None:
        return subprocess.run(
            command, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False,
            env=os.environ | GIT_ENV,
        )
    leader, follower = os.openpty()
    try:
        os.write(leader, "".join(f"{answer}\n" for answer in answers).encode())
        return subprocess.run(
            command, cwd=ROOT, stdin=follower, capture_output=True, text=True, timeout=60, check=False,
            env=os.environ | GIT_ENV,
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
    assert list(values) == list(GENERATED + IDENTITY)
    for name in GENERATED:
        (value,) = values[name]
        shape = {"RUSTFS_ACCESS_KEY": r"[0-9a-f]{20}", "LOCAL_TEST_USER_ID": UUID}.get(name, r"[0-9a-f]{64}")
        assert re.fullmatch(shape, value), name
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
    assert values["LLM_GATEWAY_URL"] == [f"synthetic-{(GENERATED + INPUTS).index('LLM_GATEWAY_URL')}"]
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


def test_the_personal_password_differs_from_the_qa_password_and_no_dev_key_is_generated(tmp_path):
    env_file = tmp_path / "scratch.env"
    assert _run(env_file).returncode == 0
    values = _values(env_file)

    assert values["LOCAL_TEST_USER_PASSWORD"] != values["KEYCLOAK_QA_PASSWORD"]
    assert "AUTH_DEV_SIGNING_KEY" not in values


def test_without_a_terminal_the_email_is_dev_example_com_and_the_domains_follow(tmp_path):
    env_file = tmp_path / "scratch.env"
    result = _run(env_file)
    values = _values(env_file)

    assert values["LOCAL_TEST_USER_EMAIL"] == ["dev@example.com"]
    assert values["AUTH_ALLOWED_EMAIL_DOMAINS"] == ["example.com"]  # deduplicated
    assert "LOCAL_TEST_USER_EMAIL (" not in result.stderr


def test_on_a_terminal_enter_takes_the_git_email_lowercased_and_the_domains_are_derived(tmp_path):
    env_file = tmp_path / "scratch.env"
    result = _run(env_file, answers=["", "", "", ""])
    values = _values(env_file)

    assert f"LOCAL_TEST_USER_EMAIL (Enter = {GIT_EMAIL.lower()})" in result.stderr
    assert values["LOCAL_TEST_USER_EMAIL"] == [GIT_EMAIL.lower()]
    assert values["AUTH_ALLOWED_EMAIL_DOMAINS"] == ["git-corp.io,example.com"]


def test_a_typed_email_is_lowercased_and_malformed_or_qa_addresses_are_asked_again(tmp_path):
    env_file = tmp_path / "scratch.env"
    bad = ["no-at-sign", "two@@ats.io", "a@nodot", "a@b..io", "me@x.io;rm", "alice@example.com", "carol@other.example"]
    result = _run(env_file, answers=[*bad[:2], "Jane.Doe@Corp.IO", "", "", ""])
    values = _values(env_file)

    assert values["LOCAL_TEST_USER_EMAIL"] == ["jane.doe@corp.io"]
    assert result.stderr.count("not usable as your sign-in email") == 2
    for address in bad[2:]:
        env_file.unlink()
        _run(env_file, answers=[address, address, address, "", "", ""])
        assert _values(env_file)["LOCAL_TEST_USER_EMAIL"] == ["dev@example.com"], address


def test_domains_are_written_only_when_missing_and_a_short_list_is_warned_about_never_changed(tmp_path):
    env_file = tmp_path / "scratch.env"
    env_file.write_text(COMPLETE.replace("corp.io,example.com", "Other.org"))
    before = env_file.read_text()
    result = _run(env_file)

    assert env_file.read_text() == before
    assert "lacks corp.io example.com;" in result.stderr
    assert "other.org" not in result.stderr.lower()


def test_a_list_with_both_domains_gives_no_warning(tmp_path):
    env_file = tmp_path / "scratch.env"
    env_file.write_text(COMPLETE.replace("corp.io,example.com", "EXAMPLE.com,x.io,Corp.io"))
    result = _run(env_file)

    assert "lacks" not in result.stderr
    assert "complete, left unchanged" in result.stdout


def test_an_empty_domains_line_is_replaced_and_an_existing_email_is_kept(tmp_path):
    env_file = tmp_path / "scratch.env"
    env_file.write_text(COMPLETE.replace("AUTH_ALLOWED_EMAIL_DOMAINS=corp.io,example.com", "AUTH_ALLOWED_EMAIL_DOMAINS="))
    _run(env_file)
    values = _values(env_file)

    assert values["LOCAL_TEST_USER_EMAIL"] == ["me@corp.io"]
    assert values["AUTH_ALLOWED_EMAIL_DOMAINS"] == ["corp.io,example.com"]


def test_nothing_the_target_generates_or_asks_is_printed(tmp_path):
    env_file = tmp_path / "scratch.env"
    result = _run(env_file, answers=["Secret.Person@Private-Corp.io", "", "", ""])
    values = _values(env_file)
    shown = result.stdout + result.stderr.replace("Enter = " + GIT_EMAIL.lower(), "")

    assert "private-corp.io" not in shown.lower() and "secret.person" not in shown.lower()
    for name in GENERATED:
        assert values[name][0] not in shown
