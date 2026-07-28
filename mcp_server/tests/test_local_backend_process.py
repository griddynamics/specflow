"""Unit tests for the bare-metal process-control helpers
(services/local_backend_process.py) — split out of local_env.

Subprocess/HTTP-backed helpers are exercised with mocks so no docker or backend
is required; the machine-wide pidfile is redirected via the injectable ``home``
arg so tests never touch the real ~/.specflow.
"""

import pytest

from services import local_backend_process


class TestBuildProcessBackendEnv:
    def test_forces_process_and_fills_host_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_backend_process, "read_dotenv", lambda root: {"OPENROUTER_API_KEY": "k"})
        for var in ("DATABASE_TYPE", "SQLITE_DB_PATH", "WORKSPACE_BASE_PATH"):
            monkeypatch.delenv(var, raising=False)
        env = local_backend_process.build_process_backend_env(tmp_path)
        assert env["BACKEND_RUNTIME"] == "process"
        assert env["OPENROUTER_API_KEY"] == "k"
        assert env["DATABASE_TYPE"] == "sqlite"
        assert env["WORKSPACE_BASE_PATH"] == str(tmp_path / "workspaces")

    def test_respects_user_overrides(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_backend_process, "read_dotenv", lambda root: {"DATABASE_TYPE": "firestore"})
        monkeypatch.setenv("WORKSPACE_BASE_PATH", "/custom/ws")
        env = local_backend_process.build_process_backend_env(tmp_path)
        assert env["DATABASE_TYPE"] == "firestore"  # from .env, not overwritten
        assert env["WORKSPACE_BASE_PATH"] == "/custom/ws"  # from env, not overwritten


class TestProcessControl:
    @pytest.mark.asyncio
    async def test_start_records_pid_and_spawns_detached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_backend_process, "read_dotenv", lambda root: {})

        class _FakePopen:
            def __init__(self, argv, **kwargs):
                self.pid = 4321
                _FakePopen.argv = argv
                _FakePopen.kwargs = kwargs

        monkeypatch.setattr(local_backend_process.subprocess, "Popen", _FakePopen)
        pid = await local_backend_process.start_backend_process(tmp_path, home=tmp_path)
        assert pid == 4321
        assert local_backend_process._read_backend_pid(tmp_path) == 4321
        # Detached from the terminal + bound to localhost uvicorn from backend/.
        assert _FakePopen.kwargs["start_new_session"] is True
        assert _FakePopen.kwargs["cwd"] == str(tmp_path / "backend")
        assert _FakePopen.argv[:4] == ["uv", "run", "uvicorn", "app.main:app"]
        assert local_backend_process.backend_log_path(tmp_path).exists()

    def test_running_true_when_pid_alive(self, tmp_path, monkeypatch):
        local_backend_process.backend_pid_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        local_backend_process.backend_pid_path(tmp_path).write_text("999")
        monkeypatch.setattr(local_backend_process, "_pid_alive", lambda pid: True)
        assert local_backend_process.backend_process_running(tmp_path) is True

    def test_running_false_when_no_pidfile(self, tmp_path):
        assert local_backend_process.backend_process_running(tmp_path) is False

    def test_stop_signals_group_and_clears_pidfile(self, tmp_path, monkeypatch):
        local_backend_process.backend_pid_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        local_backend_process.backend_pid_path(tmp_path).write_text("999")
        monkeypatch.setattr(local_backend_process, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(local_backend_process.os, "getpgid", lambda pid: pid)
        killed: list[int] = []
        monkeypatch.setattr(local_backend_process.os, "killpg", lambda pgid, sig: killed.append(pgid))
        assert local_backend_process.stop_backend_process(tmp_path) is True
        assert killed == [999]
        assert not local_backend_process.backend_pid_path(tmp_path).exists()

    def test_stop_noop_when_nothing_recorded(self, tmp_path):
        assert local_backend_process.stop_backend_process(tmp_path) is False


class TestAgentSandboxUnavailableReason:
    def test_macos_ok_when_present(self, monkeypatch):
        monkeypatch.setattr(local_backend_process.sys, "platform", "darwin")
        monkeypatch.setattr(local_backend_process.shutil, "which", lambda name: "/usr/bin/sandbox-exec")
        assert local_backend_process.agent_sandbox_unavailable_reason() is None

    def test_linux_reports_missing_deps(self, monkeypatch):
        monkeypatch.setattr(local_backend_process.sys, "platform", "linux")
        monkeypatch.setattr(local_backend_process.shutil, "which", lambda name: None)
        reason = local_backend_process.agent_sandbox_unavailable_reason()
        assert reason is not None
        assert "bwrap" in reason and "socat" in reason

    def test_unsupported_platform(self, monkeypatch):
        monkeypatch.setattr(local_backend_process.sys, "platform", "win32")
        reason = local_backend_process.agent_sandbox_unavailable_reason()
        assert reason is not None and "win32" in reason
