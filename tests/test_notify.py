"""
tests/test_notify.py — Unit tests for claude_runner/notify.py

Tests cover NotificationManager dispatch routing, email guard logic,
format_email_body(), and channel-type handling.

apprise is mocked throughout to avoid real network calls.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock, patch

import pytest

from claude_runner.notify import (
    EMAIL_EXCLUDED_EVENTS,
    EMAIL_GUARD_MINUTES,
    NotificationManager,
)


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------


def _make_notify_config(on=None, channels=None):
    """Create a minimal mock notify config object."""
    cfg = MagicMock()
    cfg.on = on or []
    cfg.channels = channels or []
    return cfg


def _make_channel(ch_type: str, **kwargs) -> dict:
    """Return a channel dict (as consumed by NotificationManager)."""
    ch = {"type": ch_type}
    ch.update(kwargs)
    return ch


def _make_manager(
    notify_config=None,
    task_name: str = "Test Task",
    secrets_config=None,
    on_fault=None,
    apprise_module=None,
) -> NotificationManager:
    """Construct a NotificationManager with a pre-injected mock apprise module."""
    if on_fault is None:
        on_fault = MagicMock()
    nm = NotificationManager(
        notify_config=notify_config,
        task_name=task_name,
        secrets_config=secrets_config,
        on_fault=on_fault,
    )
    # Inject mock apprise so no real notifications are sent.
    if apprise_module is not None:
        nm._apprise_module = apprise_module
    return nm


# ---------------------------------------------------------------------------
# EMAIL_EXCLUDED_EVENTS routing
# ---------------------------------------------------------------------------


class TestEmailExcludedEvents:
    """Events in EMAIL_EXCLUDED_EVENTS must not trigger email sends."""

    @pytest.mark.parametrize("event", sorted(EMAIL_EXCLUDED_EVENTS))
    def test_excluded_event_skips_email(self, event, mock_apprise):
        """Email channel is skipped for excluded events."""
        email_ch = _make_channel("email", to="user@example.com")
        cfg = _make_notify_config(
            on=[event],
            channels=[email_ch],
        )
        nm = _make_manager(notify_config=cfg, apprise_module=mock_apprise)

        nm.dispatch(event=event, message="something happened")

        # apprise.Apprise().notify() should NOT have been called for email.
        # (Desktop notifications use the same apprise mock, so we check that
        # no mailtos:// URL was added — we do this by inspecting the add calls.)
        instance = mock_apprise.Apprise.return_value
        add_calls = [str(call) for call in instance.add.call_args_list]
        # No email-style URL should have been added.
        assert not any("mailtos" in c or "mailto" in c for c in add_calls), (
            f"Email URL was added for excluded event {event!r}: {add_calls}"
        )

    def test_complete_event_allows_email(self, mock_apprise):
        """'complete' is NOT an excluded event — email should be attempted."""
        secrets = MagicMock()
        secrets.smtp_host = "smtp.example.com"
        secrets.smtp_port = 587
        secrets.smtp_user = "user"
        secrets.smtp_password = "pass"
        secrets.email_to = "dest@example.com"

        email_ch = _make_channel("email", to="dest@example.com")
        cfg = _make_notify_config(on=["complete"], channels=[email_ch])
        nm = _make_manager(
            notify_config=cfg,
            secrets_config=secrets,
            apprise_module=mock_apprise,
        )

        nm.dispatch(event="complete", message="Task done.")
        instance = mock_apprise.Apprise.return_value
        assert instance.notify.called


# ---------------------------------------------------------------------------
# Email guard
# ---------------------------------------------------------------------------


class TestEmailGuard:
    def _make_email_nm(self, mock_apprise) -> tuple[NotificationManager, MagicMock]:
        """Return a manager configured with an email channel and working SMTP secrets."""
        secrets = MagicMock()
        secrets.smtp_host = "smtp.example.com"
        secrets.smtp_port = 587
        secrets.smtp_user = "user"
        secrets.smtp_password = "secret"
        secrets.email_to = "to@example.com"

        email_ch = _make_channel("email", to="to@example.com")
        cfg = _make_notify_config(on=["complete", "error"], channels=[email_ch])
        fault_cb = MagicMock()
        nm = _make_manager(
            notify_config=cfg,
            secrets_config=secrets,
            on_fault=fault_cb,
            apprise_module=mock_apprise,
        )
        return nm, fault_cb

    def test_first_email_is_sent(self, mock_apprise):
        """The first email within a session is always sent."""
        nm, _ = self._make_email_nm(mock_apprise)
        nm.dispatch("complete", "done")
        assert mock_apprise.Apprise.return_value.notify.called

    def test_second_email_within_guard_is_blocked(self, mock_apprise):
        """A second email before the guard interval elapses is blocked."""
        nm, fault_cb = self._make_email_nm(mock_apprise)

        # Manually set _last_email_time to simulate a recent send.
        nm._last_email_time = time.monotonic()

        # Reset call count tracker.
        mock_apprise.Apprise.return_value.notify.reset_mock()

        nm.dispatch("complete", "done again")

        # on_fault must have been called (BUG guard triggered).
        fault_cb.assert_called_once()
        bug_msg = fault_cb.call_args[0][0]
        assert "[BUG]" in bug_msg

    def test_email_allowed_after_guard_interval(self, mock_apprise):
        """Email is allowed after the guard interval has elapsed."""
        nm, fault_cb = self._make_email_nm(mock_apprise)
        guard_seconds = EMAIL_GUARD_MINUTES * 60

        # Set _last_email_time to well before the guard interval.
        nm._last_email_time = time.monotonic() - (guard_seconds + 10)

        mock_apprise.Apprise.return_value.notify.reset_mock()
        nm.dispatch("complete", "second send — should be allowed")

        # Fault callback must NOT be called.
        fault_cb.assert_not_called()
        assert mock_apprise.Apprise.return_value.notify.called

    def test_guard_fires_bug_log_entry(self, mock_apprise, caplog):
        """When the email guard fires, an ERROR-level log entry is written."""
        nm, _ = self._make_email_nm(mock_apprise)
        nm._last_email_time = time.monotonic()  # just sent

        with caplog.at_level(logging.ERROR, logger="claude_runner.notify"):
            nm.dispatch("complete", "duplicate")

        assert any("[BUG]" in record.message for record in caplog.records)

    def test_guard_calls_on_fault_callback(self, mock_apprise):
        """on_fault is called exactly once when the email guard fires."""
        nm, fault_cb = self._make_email_nm(mock_apprise)
        nm._last_email_time = time.monotonic()

        nm.dispatch("complete", "duplicate")
        fault_cb.assert_called_once()

    def test_guard_desktop_notification_on_bug(self, mock_apprise):
        """When guard fires, a desktop BUG notification is sent."""
        nm, _ = self._make_email_nm(mock_apprise)
        nm._last_email_time = time.monotonic()

        mock_apprise.Apprise.return_value.notify.reset_mock()
        nm.dispatch("complete", "duplicate")

        # A desktop notification should have been sent with a BUG title.
        call_args_list = mock_apprise.Apprise.return_value.notify.call_args_list
        titles = [str(call) for call in call_args_list]
        assert any("BUG" in t for t in titles)


# ---------------------------------------------------------------------------
# dispatch() — event subscription filtering
# ---------------------------------------------------------------------------


class TestDispatchEventFiltering:
    def test_event_not_in_on_list_is_skipped(self, mock_apprise, caplog):
        """Events not in notify.on are silently dropped without sending anything."""
        desktop_ch = _make_channel("desktop")
        cfg = _make_notify_config(on=["complete"], channels=[desktop_ch])
        nm = _make_manager(notify_config=cfg, apprise_module=mock_apprise)

        with caplog.at_level(logging.DEBUG, logger="claude_runner.notify"):
            nm.dispatch("start", "task started")

        mock_apprise.Apprise.return_value.notify.assert_not_called()

    def test_subscribed_event_triggers_notification(self, mock_apprise):
        """An event in notify.on triggers a desktop notification."""
        desktop_ch = _make_channel("desktop")
        cfg = _make_notify_config(on=["start"], channels=[desktop_ch])
        nm = _make_manager(notify_config=cfg, apprise_module=mock_apprise)
        nm.dispatch("start", "task started")
        mock_apprise.Apprise.return_value.notify.assert_called()

    def test_unknown_event_is_ignored(self, mock_apprise, caplog):
        """Unknown event names log a warning and do not send anything."""
        desktop_ch = _make_channel("desktop")
        cfg = _make_notify_config(on=["complete"], channels=[desktop_ch])
        nm = _make_manager(notify_config=cfg, apprise_module=mock_apprise)

        with caplog.at_level(logging.WARNING, logger="claude_runner.notify"):
            nm.dispatch("bogus_event", "???")

        mock_apprise.Apprise.return_value.notify.assert_not_called()
        assert any("unknown event" in r.message.lower() for r in caplog.records)

    def test_empty_on_list_allows_all_events(self, mock_apprise):
        """When notify.on is empty, all events pass through to channels."""
        desktop_ch = _make_channel("desktop")
        cfg = _make_notify_config(on=[], channels=[desktop_ch])
        nm = _make_manager(notify_config=cfg, apprise_module=mock_apprise)
        nm.dispatch("start", "hello")
        mock_apprise.Apprise.return_value.notify.assert_called()


# ---------------------------------------------------------------------------
# format_email_body()
# ---------------------------------------------------------------------------


class TestFormatEmailBody:
    def _nm(self) -> NotificationManager:
        return _make_manager(task_name="My Task")

    def test_required_fields_in_body(self):
        nm = self._nm()
        body = nm.format_email_body(
            task_name="My Task",
            status="COMPLETE",
            runtime_str="4h 22m",
            change_summary="",
        )
        assert "Task:    My Task" in body
        assert "Status:  COMPLETE" in body
        assert "Runtime: 4h 22m" in body

    def test_change_summary_indented_when_present(self):
        nm = self._nm()
        body = nm.format_email_body(
            task_name="T",
            status="COMPLETE",
            runtime_str="1h",
            change_summary="src/foo.py | 10 +++\n3 files changed",
        )
        assert "Changed files:" in body
        assert "  src/foo.py | 10 +++" in body
        assert "  3 files changed" in body

    def test_no_change_summary_shows_placeholder(self):
        nm = self._nm()
        body = nm.format_email_body(
            task_name="T",
            status="ERROR",
            runtime_str="2m",
            change_summary="",
        )
        assert "(no change summary available)" in body

    def test_multi_line_change_summary(self):
        nm = self._nm()
        lines = ["file_a.py |  5 ++", "file_b.py | 10 ---", "2 files changed"]
        body = nm.format_email_body(
            task_name="T",
            status="COMPLETE",
            runtime_str="30m",
            change_summary="\n".join(lines),
        )
        for line in lines:
            assert f"  {line}" in body


# ---------------------------------------------------------------------------
# Unknown channel type
# ---------------------------------------------------------------------------


class TestUnknownChannelType:
    def test_unknown_channel_logs_warning_does_not_crash(self, mock_apprise, caplog):
        """An unrecognised channel type logs a warning but does not raise."""
        weird_ch = _make_channel("slack_plus_ultra")
        cfg = _make_notify_config(on=["complete"], channels=[weird_ch])
        nm = _make_manager(notify_config=cfg, apprise_module=mock_apprise)

        with caplog.at_level(logging.WARNING, logger="claude_runner.notify"):
            nm.dispatch("complete", "task done")  # must not raise

        assert any(
            "unknown" in r.message.lower() and "channel" in r.message.lower()
            for r in caplog.records
        )

    def test_unknown_channel_does_not_send_notification(self, mock_apprise):
        """An unrecognised channel type results in no apprise notification."""
        weird_ch = _make_channel("pigeon")
        cfg = _make_notify_config(on=["complete"], channels=[weird_ch])
        nm = _make_manager(notify_config=cfg, apprise_module=mock_apprise)
        nm.dispatch("complete", "task done")
        mock_apprise.Apprise.return_value.notify.assert_not_called()
