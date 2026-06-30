"""Tests for the `specflow init` command and its main() guard bypass."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import cli


def _make_repo(root: Path) -> None:
    (root / "specflow-init.sh").write_text("#!/bin/bash\n")
    (root / "docker-compose.yml").write_text("services: {}\n")


def _args(root, **over):
    base = dict(
        root_path=str(root) if root else None,
        max_parallel_runs=None,
        skip_build=False,
        reset_local_db=False,
        provide_own_repos=None,
        dry_run=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


class TestCmdInit:
    @pytest.mark.asyncio
    async def test_repo_not_found(self, tmp_path, capsys):
        # No checkout from cwd, and a non-editable install (no own checkout) →
        # resolve_repo_root returns None.
        with patch("cli.local_env.resolve_repo_root", return_value=None):
            rc = await cli.cmd_init(_args(tmp_path))
        assert rc == 1
        assert "Couldn't locate a SpecFlow checkout" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_env_missing(self, tmp_path, capsys):
        _make_repo(tmp_path)
        rc = await cli.cmd_init(_args(tmp_path))
        assert rc == 1
        err = capsys.readouterr().err
        assert ".env" in err and ".env.quickstart.example" in err

    @pytest.mark.asyncio
    async def test_happy_path_passes_flags_and_returns_code(self, tmp_path, capsys):
        _make_repo(tmp_path)
        (tmp_path / ".env").write_text("GITHUB_TOKEN_DEFAULT=x\n")
        with patch("cli.local_env.run_init", new=AsyncMock(return_value=0)) as run_mock:
            rc = await cli.cmd_init(_args(tmp_path, max_parallel_runs=2, skip_build=True))
        assert rc == 0
        flags = run_mock.call_args.args[1]
        assert flags.max_parallel_runs == 2 and flags.skip_build is True
        # IDE registration hint printed on success.
        assert "Register the MCP server" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_dry_run_skips_registration_hint(self, tmp_path, capsys):
        _make_repo(tmp_path)
        (tmp_path / ".env").write_text("GITHUB_TOKEN_DEFAULT=x\n")
        with patch("cli.local_env.run_init", new=AsyncMock(return_value=0)):
            rc = await cli.cmd_init(_args(tmp_path, dry_run=True))
        assert rc == 0
        out = capsys.readouterr().out
        assert "Register the MCP server" not in out

    @pytest.mark.asyncio
    async def test_failure_returns_nonzero(self, tmp_path):
        _make_repo(tmp_path)
        (tmp_path / ".env").write_text("X=1\n")
        with patch("cli.local_env.run_init", new=AsyncMock(return_value=3)):
            rc = await cli.cmd_init(_args(tmp_path))
        assert rc == 3


class TestMainGuardBypass:
    def test_init_skips_localhost_guard(self, tmp_path, monkeypatch):
        """`init` must dispatch without the localhost guard tripping on a remote URL."""
        monkeypatch.setenv("BACKEND_URL", "https://remote.example.com")
        monkeypatch.setattr("sys.argv", ["specflow", "--root-path", str(tmp_path), "init"])

        guard = patch("cli.check_localhost_guard", side_effect=AssertionError("guard ran"))
        # cmd_init exits 1 (no repo) — we only assert the guard was bypassed and
        # main() exited via that code path rather than the guard.
        with guard, patch("cli.cmd_init", new=AsyncMock(return_value=0)):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 0
