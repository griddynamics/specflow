"""Tests for the OS-level agent sandbox (BACKEND_RUNTIME=process).

Covers get_agent_sandbox_settings (off in docker, fail-closed in process) and
check_agent_sandbox_available (platform-specific, fail-closed preflight). The
module reads the global settings singleton at call time, so tests monkeypatch
``settings.BACKEND_RUNTIME`` / ``settings.AGENT_SANDBOX_ALLOWED_DOMAINS`` and the
platform/PATH probes.
"""

import app.agents_sandboxing.os_sandbox as os_sandbox
from app.agents_sandboxing.os_sandbox import (
    DEFAULT_AGENT_SANDBOX_ALLOWED_DOMAINS,
    check_agent_sandbox_available,
    get_agent_sandbox_settings,
)
from app.core.config import settings
from app.core.enums import BackendRuntime


class TestGetAgentSandboxSettings:
    def test_none_in_docker_mode(self, monkeypatch):
        monkeypatch.setattr(settings, "BACKEND_RUNTIME", BackendRuntime.DOCKER)
        assert get_agent_sandbox_settings() is None

    def test_fail_closed_settings_in_process_mode(self, monkeypatch):
        monkeypatch.setattr(settings, "BACKEND_RUNTIME", BackendRuntime.PROCESS)
        monkeypatch.setattr(settings, "AGENT_SANDBOX_ALLOWED_DOMAINS", None)
        cfg = get_agent_sandbox_settings()
        assert cfg is not None
        assert cfg["enabled"] is True
        # Fail closed: no dangerouslyDisableSandbox escape hatch.
        assert cfg["allowUnsandboxedCommands"] is False
        assert cfg["network"]["allowedDomains"] == list(DEFAULT_AGENT_SANDBOX_ALLOWED_DOMAINS)

    def test_allowed_domains_override(self, monkeypatch):
        monkeypatch.setattr(settings, "BACKEND_RUNTIME", BackendRuntime.PROCESS)
        monkeypatch.setattr(
            settings, "AGENT_SANDBOX_ALLOWED_DOMAINS", "example.com, foo.test ,"
        )
        cfg = get_agent_sandbox_settings()
        assert cfg["network"]["allowedDomains"] == ["example.com", "foo.test"]


class TestCheckAgentSandboxAvailable:
    def test_none_in_docker_mode_regardless_of_platform(self, monkeypatch):
        monkeypatch.setattr(settings, "BACKEND_RUNTIME", BackendRuntime.DOCKER)
        monkeypatch.setattr(os_sandbox.sys, "platform", "win32")
        assert check_agent_sandbox_available() is None

    def test_macos_ok_when_sandbox_exec_present(self, monkeypatch):
        monkeypatch.setattr(settings, "BACKEND_RUNTIME", BackendRuntime.PROCESS)
        monkeypatch.setattr(os_sandbox.sys, "platform", "darwin")
        monkeypatch.setattr(os_sandbox.shutil, "which", lambda name: "/usr/bin/sandbox-exec")
        assert check_agent_sandbox_available() is None

    def test_macos_fail_closed_when_missing(self, monkeypatch):
        monkeypatch.setattr(settings, "BACKEND_RUNTIME", BackendRuntime.PROCESS)
        monkeypatch.setattr(os_sandbox.sys, "platform", "darwin")
        monkeypatch.setattr(os_sandbox.shutil, "which", lambda name: None)
        unavailable = check_agent_sandbox_available()
        assert unavailable is not None
        assert unavailable.dependency == "sandbox-exec"

    def test_linux_ok_when_both_present(self, monkeypatch):
        monkeypatch.setattr(settings, "BACKEND_RUNTIME", BackendRuntime.PROCESS)
        monkeypatch.setattr(os_sandbox.sys, "platform", "linux")
        monkeypatch.setattr(os_sandbox.shutil, "which", lambda name: f"/usr/bin/{name}")
        assert check_agent_sandbox_available() is None

    def test_linux_reports_each_missing_dep(self, monkeypatch):
        monkeypatch.setattr(settings, "BACKEND_RUNTIME", BackendRuntime.PROCESS)
        monkeypatch.setattr(os_sandbox.sys, "platform", "linux")
        monkeypatch.setattr(
            os_sandbox.shutil,
            "which",
            lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
        )
        unavailable = check_agent_sandbox_available()
        assert unavailable is not None
        assert "socat" in unavailable.dependency
        assert "bwrap" not in unavailable.dependency

    def test_unsupported_platform_fails_closed(self, monkeypatch):
        monkeypatch.setattr(settings, "BACKEND_RUNTIME", BackendRuntime.PROCESS)
        monkeypatch.setattr(os_sandbox.sys, "platform", "win32")
        unavailable = check_agent_sandbox_available()
        assert unavailable is not None
        assert "win32" in unavailable.message
