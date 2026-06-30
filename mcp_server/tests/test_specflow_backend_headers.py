"""Tests for SpecFlowBackendService._get_headers() — both install modes.

LOCAL mode  (no SPECFLOW_API_KEY):  X-API-Key must be absent; X-User-Email must be present.
HOSTED mode (SPECFLOW_API_KEY set): X-API-Key must be present.
"""
from services.specflow_backend import SpecFlowBackendService


class TestGetHeadersLocalMode:
    """No SPECFLOW_API_KEY set — local / self-hosted install."""

    def test_omits_api_key_header_when_key_not_set(self, monkeypatch):
        monkeypatch.delenv("SPECFLOW_API_KEY", raising=False)
        monkeypatch.setenv("USER_EMAIL", "dev@example.com")
        svc = SpecFlowBackendService()
        headers = svc._get_headers()
        assert "X-API-Key" not in headers

    def test_omits_api_key_header_when_key_is_blank(self, monkeypatch):
        monkeypatch.setenv("SPECFLOW_API_KEY", "")
        monkeypatch.setenv("USER_EMAIL", "dev@example.com")
        svc = SpecFlowBackendService()
        headers = svc._get_headers()
        assert "X-API-Key" not in headers

    def test_includes_user_email_header_when_email_set(self, monkeypatch):
        monkeypatch.delenv("SPECFLOW_API_KEY", raising=False)
        monkeypatch.setenv("USER_EMAIL", "dev@example.com")
        svc = SpecFlowBackendService()
        headers = svc._get_headers()
        assert headers.get("X-User-Email") == "dev@example.com"

    def test_omits_user_email_header_when_email_not_set(self, monkeypatch):
        monkeypatch.delenv("SPECFLOW_API_KEY", raising=False)
        monkeypatch.delenv("USER_EMAIL", raising=False)
        svc = SpecFlowBackendService()
        headers = svc._get_headers()
        assert "X-User-Email" not in headers
        assert "X-API-Key" not in headers


class TestGetHeadersHostedMode:
    """SPECFLOW_API_KEY set — hosted install."""

    def test_includes_api_key_header_when_key_set(self, monkeypatch):
        monkeypatch.setenv("SPECFLOW_API_KEY", "sk-test-key-123")
        monkeypatch.delenv("USER_EMAIL", raising=False)
        svc = SpecFlowBackendService()
        headers = svc._get_headers()
        assert headers.get("X-API-Key") == "sk-test-key-123"

    def test_includes_both_headers_when_key_and_email_set(self, monkeypatch):
        monkeypatch.setenv("SPECFLOW_API_KEY", "sk-test-key-123")
        monkeypatch.setenv("USER_EMAIL", "user@example.com")
        svc = SpecFlowBackendService()
        headers = svc._get_headers()
        assert headers.get("X-API-Key") == "sk-test-key-123"
        assert headers.get("X-User-Email") == "user@example.com"
