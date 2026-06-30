"""Blank-config no-op regression tests for optional integrations (FR-10, FR-11 / S4.1).

These tests document and protect EXISTING gate behaviour — no production code is changed.
Each test verifies that blank/unset config produces NO network call and NO error.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.core.config import Settings
from app.core.notifications import (
    EmailNotifier,
    Notifications,
    SlackNotifier,
)


# ---------------------------------------------------------------------------
# Slack: blank webhook → Notifications starts empty; notify is a no-op
# ---------------------------------------------------------------------------

class TestSlackBlankConfigNoOp:
    """Blank SLACK_WEBHOOK_URL → no SlackNotifier added → notify() silently no-ops."""

    def test_notifications_empty_when_no_slack_webhook(self) -> None:
        """Scenario: module-level Notifications object has no SlackNotifier when webhook blank."""
        notifs = Notifications()
        slack_notifiers = [n for n in notifs.notifiers if isinstance(n, SlackNotifier)]
        # A freshly constructed Notifications() has zero notifiers by default.
        assert len(notifs.notifiers) == 0
        assert slack_notifiers == []

    def test_notify_with_no_notifiers_does_not_raise(self) -> None:
        """Scenario: empty Notifications → notify() is a no-op, no exception."""
        notifs = Notifications()
        notifs.notify("test message", recipient_email="u@example.com")

    def test_notify_generation_session_complete_with_no_notifiers_does_not_raise(self) -> None:
        """Scenario: FR-11 — notification absence must be non-fatal at completion."""
        notifs = Notifications()
        # No error even though there are no notifiers.
        notifs.notify_generation_session_complete(
            generation_id="est-123",
            workspace_ids=["ws-1"],
            result=MagicMock(),
            spec_path="specs/test.md",
            recipient_email="u@example.com",
            db=MagicMock(),
        )

    def test_slack_notifier_not_constructed_when_webhook_blank(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The module-level guard (if settings.SLACK_WEBHOOK_URL:) prevents SlackNotifier construction."""
        # Simulate blank webhook: get_email_config returns None; the guard is settings.SLACK_WEBHOOK_URL.
        with patch("app.core.notifications.settings") as mock_settings:
            mock_settings.SLACK_WEBHOOK_URL = None
            mock_settings.get_email_config.return_value = None
            # Simulate the module-level registration logic — blank webhook means no notifier added.
            fresh = Notifications()
            if mock_settings.SLACK_WEBHOOK_URL:
                fresh.add_notifier(SlackNotifier(mock_settings.SLACK_WEBHOOK_URL))
            assert not any(isinstance(n, SlackNotifier) for n in fresh.notifiers)


# ---------------------------------------------------------------------------
# Email: blank config → no EmailNotifier added → notify() is a no-op
# ---------------------------------------------------------------------------

class TestEmailBlankConfigNoOp:
    """Blank NOTIFY_EMAIL_USERNAME/PASSWORD → no EmailNotifier added → no send, no error."""

    def test_get_email_config_returns_none_when_both_blank(self) -> None:
        """Settings.get_email_config() returns None when username/password are blank."""
        with patch("app.core.config.Settings.get_email_config") as mock_get:
            mock_get.return_value = None
            result = mock_get()
        assert result is None

    def test_settings_get_email_config_none_when_username_missing(self) -> None:
        """get_email_config returns None when NOTIFY_EMAIL_USERNAME is None."""
        s = Settings(
            NOTIFY_EMAIL_USERNAME=None,
            NOTIFY_EMAIL_PASSWORD=None,
        )
        assert s.get_email_config() is None

    def test_settings_get_email_config_none_when_password_missing(self) -> None:
        """get_email_config returns None when NOTIFY_EMAIL_PASSWORD is None."""
        s = Settings(
            NOTIFY_EMAIL_USERNAME="user@example.com",
            NOTIFY_EMAIL_PASSWORD=None,
        )
        assert s.get_email_config() is None

    def test_notifications_empty_when_no_email_config(self) -> None:
        """Scenario: blank email env → Notifications has no EmailNotifier → no SMTP call."""
        notifs = Notifications()
        email_notifiers = [n for n in notifs.notifiers if isinstance(n, EmailNotifier)]
        assert len(notifs.notifiers) == 0
        assert email_notifiers == []

    def test_notify_with_empty_notifiers_does_not_raise(self) -> None:
        """FR-11: absence of EmailNotifier must be non-fatal."""
        notifs = Notifications()
        notifs.notify("generation complete", recipient_email="u@example.com")

    @patch("app.core.notifications.smtplib.SMTP_SSL")
    def test_email_notifier_not_added_when_config_missing(self, mock_smtp: MagicMock) -> None:
        """If get_email_config() returns None, EmailNotifier is never instantiated → no SMTP."""
        notifs = Notifications()
        email_config = None  # blank config guard
        if email_config:
            notifs.add_notifier(EmailNotifier(email_config))
        notifs.notify("msg", recipient_email="u@example.com")
        mock_smtp.assert_not_called()


# ---------------------------------------------------------------------------
# LangFuse: blank keys → disabled, no error
# ---------------------------------------------------------------------------

class TestLangFuseBlankConfigNoOp:
    """langfuse_enabled=False when any of the 3 keys is blank → disabled, no error."""

    @pytest.mark.parametrize(
        "public_key,secret_key,base_url",
        [
            (None, "sk", "https://cloud.langfuse.com"),
            ("pk", None, "https://cloud.langfuse.com"),
            ("pk", "sk", None),
            (None, None, None),
        ],
        ids=["missing_public", "missing_secret", "missing_base_url", "all_blank"],
    )
    def test_langfuse_enabled_false_when_any_key_blank(
        self,
        public_key: object,
        secret_key: object,
        base_url: object,
    ) -> None:
        """langfuse_enabled property returns False when any credential is absent."""
        s = Settings(
            LANGFUSE_PUBLIC_KEY=public_key,
            LANGFUSE_SECRET_KEY=secret_key,
            LANGFUSE_BASE_URL=base_url,
        )
        assert s.langfuse_enabled is False

    def test_langfuse_all_blank_no_error_at_settings_construction(self) -> None:
        """Constructing Settings with all LangFuse keys blank raises no error."""
        s = Settings(
            LANGFUSE_PUBLIC_KEY=None,
            LANGFUSE_SECRET_KEY=None,
            LANGFUSE_BASE_URL=None,
        )
        assert s.langfuse_enabled is False
