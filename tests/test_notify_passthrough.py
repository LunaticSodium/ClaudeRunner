"""
tests/test_notify_passthrough.py

Tests for Feature A4: Claude Code output passthrough to ntfy.
"""
from __future__ import annotations

import pytest

from claude_runner.notify import (
    NotificationManager,
    _NTFY_MAX_CHARS,
    _TRUNCATION_MARKER,
    extract_completion_summary,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_notifier(task_name="test-task"):
    """Return a minimal NotificationManager (no notify config, no secrets)."""
    return NotificationManager(
        notify_config=None,
        task_name=task_name,
        secrets_config=None,
        on_fault=lambda msg: None,
    )


# ---------------------------------------------------------------------------
# extract_completion_summary
# ---------------------------------------------------------------------------


class TestExtractCompletionSummary:
    def test_empty_lines_returns_empty(self):
        assert extract_completion_summary([]) == ""

    def test_only_marker_lines_returns_empty(self):
        lines = [
            "##RUNNER:COMPLETE##",
        ]
        assert extract_completion_summary(lines) == ""

    def test_nl_block_after_tool_lines_extracted(self):
        lines = [
            "Starting task…",
            "Read(foo.py)",
            "Tool: bash",
            "Here is the summary of what I did.",
            "All done.",
            "##RUNNER:COMPLETE##",
        ]
        result = extract_completion_summary(lines)
        assert "Here is the summary of what I did." in result
        assert "All done." in result

    def test_tool_lines_not_included(self):
        lines = [
            "Read(foo.py)",
            "Write(bar.py)",
            "The task is complete.",
        ]
        result = extract_completion_summary(lines)
        assert "Read(" not in result
        assert "Write(" not in result
        assert "The task is complete." in result

    def test_no_tool_lines_returns_all_content(self):
        lines = ["Line one.", "Line two.", "Line three."]
        result = extract_completion_summary(lines)
        assert "Line one." in result
        assert "Line three." in result

    def test_truncates_at_3kb(self):
        # Create a line that is > 3 KB total.
        big_line = "x" * 400
        lines = ["Plain text line."] + [big_line] * 10
        result = extract_completion_summary(lines)
        assert len(result.encode("utf-8")) <= 3 * 1024

    def test_strips_trailing_runner_complete(self):
        lines = ["Nice summary line.", "##RUNNER:COMPLETE##"]
        result = extract_completion_summary(lines)
        assert "##RUNNER:COMPLETE##" not in result
        assert "Nice summary line." in result

    def test_strips_trailing_runner_error(self):
        lines = ["Some output.", "##RUNNER:ERROR:oops##"]
        result = extract_completion_summary(lines)
        assert "##RUNNER:ERROR" not in result


# ---------------------------------------------------------------------------
# build_completion_ntfy_message
# ---------------------------------------------------------------------------


class TestBuildCompletionNtfyMessage:
    def test_structured_fields_always_present(self):
        notifier = _make_notifier("my-task")
        output_lines = ["All done. I completed everything."]
        msg = notifier.build_completion_ntfy_message(
            task_name="my-task",
            duration_str="00:05:00",
            rate_limit_cycles=2,
            output_lines=output_lines,
        )
        assert "Task: my-task" in msg
        assert "Duration: 00:05:00" in msg
        assert "RL cycles: 2" in msg

    def test_summary_included_when_nl_block_found(self):
        notifier = _make_notifier()
        output_lines = [
            "Read(file.py)",
            "I have successfully completed the task.",
            "##RUNNER:COMPLETE##",
        ]
        msg = notifier.build_completion_ntfy_message(
            task_name="t",
            duration_str="00:01:00",
            rate_limit_cycles=0,
            output_lines=output_lines,
        )
        assert "I have successfully completed the task." in msg

    def test_fallback_when_no_nl_block(self):
        notifier = _make_notifier()
        output_lines = [
            "Read(file.py)",
            "Write(out.py)",
            "##RUNNER:COMPLETE##",
        ]
        msg = notifier.build_completion_ntfy_message(
            task_name="t",
            duration_str="00:00:30",
            rate_limit_cycles=0,
            output_lines=output_lines,
        )
        # Falls back gracefully — only prefix present
        assert "Task: t" in msg
        assert "Duration: 00:00:30" in msg

    def test_truncates_at_4000_chars(self):
        notifier = _make_notifier()
        # Use a long task_name so prefix + 3KB summary > 4000 chars.
        # prefix = "Task: <1000 chars> | Duration: 01:00:00 | RL cycles: 5\n\n" ≈ 1030
        # summary ≈ 3072 bytes (3KB cap)  →  total ≈ 4102 > 4000
        long_task_name = "X" * 1000
        long_line = "A" * 400
        output_lines = [long_line] * 20  # 20*400=8000 bytes → capped at 3KB by extract
        msg = notifier.build_completion_ntfy_message(
            task_name=long_task_name,
            duration_str="01:00:00",
            rate_limit_cycles=5,
            output_lines=output_lines,
        )
        assert len(msg) <= _NTFY_MAX_CHARS
        assert msg.endswith(_TRUNCATION_MARKER)

    def test_no_truncation_when_short(self):
        notifier = _make_notifier()
        output_lines = ["Short summary."]
        msg = notifier.build_completion_ntfy_message(
            task_name="t",
            duration_str="00:00:01",
            rate_limit_cycles=0,
            output_lines=output_lines,
        )
        assert _TRUNCATION_MARKER not in msg
        assert len(msg) <= _NTFY_MAX_CHARS

    def test_empty_output_lines(self):
        notifier = _make_notifier()
        msg = notifier.build_completion_ntfy_message(
            task_name="t",
            duration_str="00:00:00",
            rate_limit_cycles=0,
            output_lines=[],
        )
        assert "Task: t" in msg
