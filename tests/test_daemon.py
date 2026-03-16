"""
tests/test_daemon.py

Unit tests for claude_runner.daemon — MarathonDaemon.

All external interactions (NtfyClient, Pipeline, PID file I/O) are mocked.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from unittest.mock import MagicMock, patch, call

import pytest

from claude_runner.daemon import MarathonDaemon, read_daemon_pid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeMarathonConfig:
    enabled: bool = True
    poll_interval_minutes: int = 5


class FakeConfig:
    def __init__(self):
        self.marathon = FakeMarathonConfig()


def make_daemon(**kwargs) -> MarathonDaemon:
    cfg = FakeConfig()
    return MarathonDaemon(config=cfg, **kwargs)


# ---------------------------------------------------------------------------
# Tests: stop()
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_sets_shutdown_event(self):
        daemon = make_daemon()
        assert not daemon._shutdown.is_set()
        daemon.stop()
        assert daemon._shutdown.is_set()

    def test_daemon_exits_cleanly_after_stop(self):
        """run() must return promptly after stop() is called from another thread."""
        daemon = make_daemon()

        with (
            patch("claude_runner.daemon._write_pid_file"),
            patch("claude_runner.daemon._remove_pid_file"),
            patch.object(daemon, "_notify_out"),
            patch.object(daemon, "_poll_once"),
        ):
            t = threading.Thread(target=daemon.run, daemon=True)
            t.start()
            time.sleep(0.05)
            daemon.stop()
            t.join(timeout=2.0)
            assert not t.is_alive(), "Daemon thread did not stop within 2 seconds"


# ---------------------------------------------------------------------------
# Tests: _poll_once()
# ---------------------------------------------------------------------------


class TestPollOnce:
    def test_poll_once_no_crash_on_empty_result(self):
        """_poll_once() must not crash when NtfyClient.poll() returns empty list."""
        import sys
        daemon = make_daemon()
        mock_client = MagicMock()
        mock_client.poll.return_value = []

        stub_pipeline = MagicMock()
        stub_pipeline.Pipeline = MagicMock()

        with (
            patch.object(daemon, "_get_ntfy_client", return_value=mock_client),
            patch("claude_runner.daemon._load_ntfy_state", return_value={}),
            patch.dict(sys.modules, {"claude_runner.pipeline": stub_pipeline}),
        ):
            daemon._poll_once()  # must not raise

        mock_client.poll.assert_called_once()

    def test_poll_once_skips_when_no_client(self):
        """_poll_once() must do nothing when NtfyClient is unavailable."""
        daemon = make_daemon()
        with patch.object(daemon, "_get_ntfy_client", return_value=None):
            daemon._poll_once()  # must not raise

    def test_poll_once_dispatches_messages(self):
        """_poll_once() must call Pipeline.process() for each message."""
        import sys
        daemon = make_daemon()
        mock_client = MagicMock()

        msg1 = MagicMock()
        msg1.id = "abc"
        msg2 = MagicMock()
        msg2.id = "def"
        mock_client.poll.return_value = [msg1, msg2]

        mock_pipeline_instance = MagicMock()
        mock_pipeline_cls = MagicMock(return_value=mock_pipeline_instance)

        # Provide a stub pipeline module so the lazy import inside _poll_once works
        stub_pipeline = MagicMock()
        stub_pipeline.Pipeline = mock_pipeline_cls

        with (
            patch.object(daemon, "_get_ntfy_client", return_value=mock_client),
            patch("claude_runner.daemon._load_ntfy_state", return_value={}),
            patch.dict(sys.modules, {"claude_runner.pipeline": stub_pipeline}),
        ):
            daemon._poll_once()

        assert mock_pipeline_instance.process.call_count == 2


# ---------------------------------------------------------------------------
# Tests: scheduling / poll called on time
# ---------------------------------------------------------------------------


class TestPollScheduling:
    def test_poll_once_is_called_during_run(self):
        """run() must call _poll_once() at least once before stop() is signalled."""
        daemon = make_daemon()
        call_count = {"n": 0}

        original_poll = daemon._poll_once

        def counting_poll():
            call_count["n"] += 1
            daemon.stop()  # Stop after first poll so test doesn't hang

        with (
            patch("claude_runner.daemon._write_pid_file"),
            patch("claude_runner.daemon._remove_pid_file"),
            patch.object(daemon, "_notify_out"),
            patch.object(daemon, "_poll_once", side_effect=counting_poll),
        ):
            daemon.run()

        assert call_count["n"] >= 1, "_poll_once was never called"


# ---------------------------------------------------------------------------
# Tests: status()
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_returns_dict(self):
        daemon = make_daemon()
        result = daemon.status()
        assert isinstance(result, dict)
        assert "uptime_seconds" in result
        assert "active_task" in result

    def test_status_uptime_positive(self):
        daemon = make_daemon()
        time.sleep(0.01)
        s = daemon.status()
        assert s["uptime_seconds"] > 0

    def test_status_active_task_none_by_default(self):
        daemon = make_daemon()
        assert daemon.status()["active_task"] is None


# ---------------------------------------------------------------------------
# Tests: PID file helpers
# ---------------------------------------------------------------------------


class TestPidFile:
    def test_read_daemon_pid_returns_none_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_runner.daemon._PID_FILE", tmp_path / "daemon.pid")
        assert read_daemon_pid() is None
