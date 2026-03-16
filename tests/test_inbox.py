"""
tests/test_inbox.py

Tests for Feature A1: Buffer-mediated message injection.
"""
from __future__ import annotations

import pathlib
import types
from unittest.mock import MagicMock, patch

import pytest

import claude_runner.inbox as inbox_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_inbox(tmp_path):
    """Reset module state and redirect pending.md to tmp_path before each test."""
    # Patch the pending file path to tmp_path so we don't touch the real FS.
    pending = tmp_path / "inbox" / "pending.md"
    with patch.object(inbox_mod, "_PENDING_FILE", pending):
        inbox_mod.reset()
        yield
        inbox_mod.reset()


def _read_pending(tmp_path) -> str:
    p = tmp_path / "inbox" / "pending.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ---------------------------------------------------------------------------
# append_message
# ---------------------------------------------------------------------------


class TestAppendMessage:
    def test_writes_to_pending_file(self, tmp_path):
        inbox_mod.append_message("hello world")
        content = _read_pending(tmp_path)
        assert "hello world" in content

    def test_timestamp_header_present(self, tmp_path):
        inbox_mod.append_message("test msg")
        content = _read_pending(tmp_path)
        # Should contain a timestamp header like "**Received 2026-..."
        assert "**Received " in content

    def test_sets_has_pending_messages(self, tmp_path):
        assert inbox_mod.has_pending_messages is False
        inbox_mod.append_message("anything")
        assert inbox_mod.has_pending_messages is True

    def test_appends_multiple_messages(self, tmp_path):
        inbox_mod.append_message("first message")
        inbox_mod.append_message("second message")
        content = _read_pending(tmp_path)
        assert "first message" in content
        assert "second message" in content

    def test_creates_parent_directory(self, tmp_path):
        inbox_mod.append_message("msg")
        assert (tmp_path / "inbox").is_dir()


# ---------------------------------------------------------------------------
# is_pending / has_pending_messages flag
# ---------------------------------------------------------------------------


class TestIsPending:
    def test_false_initially(self):
        assert inbox_mod.is_pending() is False
        assert inbox_mod.has_pending_messages is False

    def test_true_after_append(self, tmp_path):
        inbox_mod.append_message("something")
        assert inbox_mod.is_pending() is True


# ---------------------------------------------------------------------------
# drain
# ---------------------------------------------------------------------------


def _make_mock_process(ack_after_calls: int = 1):
    """
    Return a mock process object that:
    - has a send() method
    - has output_available() that returns True after *ack_after_calls* polls
    """
    call_count = {"n": 0}

    def _output_available():
        call_count["n"] += 1
        return call_count["n"] >= ack_after_calls

    proc = MagicMock()
    proc.output_available = _output_available
    return proc


class TestDrain:
    def test_no_op_when_no_pending(self, tmp_path):
        proc = _make_mock_process()
        inbox_mod.drain(proc)
        proc.send.assert_not_called()

    def test_injects_prompt_when_pending(self, tmp_path):
        inbox_mod.append_message("please do this")
        proc = _make_mock_process()
        inbox_mod.drain(proc)
        proc.send.assert_called_once()
        call_arg = proc.send.call_args[0][0]
        assert "pending.md" in call_arg

    def test_clears_has_pending_after_drain(self, tmp_path):
        inbox_mod.append_message("msg")
        assert inbox_mod.has_pending_messages is True
        proc = _make_mock_process()
        inbox_mod.drain(proc)
        assert inbox_mod.has_pending_messages is False

    def test_truncates_pending_md_after_drain(self, tmp_path):
        inbox_mod.append_message("some content")
        proc = _make_mock_process()
        inbox_mod.drain(proc)
        content = _read_pending(tmp_path)
        assert content == ""

    def test_no_crash_when_process_has_no_send(self, tmp_path):
        """Drain must not crash if the process has no send/write method."""
        inbox_mod.append_message("hi")
        proc = object()  # bare object — no send/write
        # Should not raise
        inbox_mod.drain(proc)

    def test_drain_is_noop_when_flag_false(self, tmp_path):
        """Even if pending.md has content, drain is a no-op when flag is False."""
        # Manually write to the file without setting the flag.
        pending = tmp_path / "inbox" / "pending.md"
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text("old content", encoding="utf-8")
        # Flag is False.
        assert inbox_mod.has_pending_messages is False
        proc = _make_mock_process()
        inbox_mod.drain(proc)
        proc.send.assert_not_called()
        # File unchanged.
        assert pending.read_text(encoding="utf-8") == "old content"


# ---------------------------------------------------------------------------
# Pipeline routing (A1 wiring in pipeline.py)
# ---------------------------------------------------------------------------


class TestPipelineInboxRouting:
    def test_non_yaml_message_routes_to_inbox(self, tmp_path):
        """A free-text message that is not a command and not YAML goes to inbox."""
        from claude_runner.pipeline import Pipeline
        from claude_runner.ntfy_client import NtfyMessage

        daemon = MagicMock()
        ntfy = MagicMock()
        pipeline = Pipeline(daemon=daemon, ntfy_client=ntfy)

        msg = NtfyMessage(id="x1", message="Hey Claude, can you check on X?", timestamp=0)
        pipeline.process(msg)

        assert inbox_mod.has_pending_messages is True

    def test_command_message_does_not_route_to_inbox(self, tmp_path):
        """A recognised control command (e.g. 'status') does not go to inbox."""
        from claude_runner.pipeline import Pipeline
        from claude_runner.ntfy_client import NtfyMessage

        daemon = MagicMock()
        daemon.status.return_value = {"uptime_seconds": 0, "active_task": None, "pid": 1}
        ntfy = MagicMock()
        pipeline = Pipeline(daemon=daemon, ntfy_client=ntfy)

        msg = NtfyMessage(id="x2", message="status", timestamp=0)
        pipeline.process(msg)

        assert inbox_mod.has_pending_messages is False
