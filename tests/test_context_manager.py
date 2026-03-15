"""
tests/test_context_manager.py — Unit tests for claude_runner/context_manager.py

Tests cover token accumulation, threshold checking, checkpoint injection,
resume prompt construction, and progress log I/O.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_runner.context_manager import (
    CHARS_PER_TOKEN,
    CHECKPOINT_PROMPT,
    STRATEGY_CONTINUE,
    STRATEGY_RESTATE,
    STRATEGY_SUMMARIZE,
    ContextManager,
    _chars_to_tokens,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cm(**kwargs) -> ContextManager:
    """Construct a ContextManager with sensible test defaults."""
    defaults = dict(
        threshold_tokens=1000,
        reset_on_rate_limit=True,
        inject_log_on_resume=False,
        progress_log_path=None,
        on_inject_checkpoint=None,
    )
    defaults.update(kwargs)
    return ContextManager(**defaults)


def _token_count(text: str) -> int:
    return _chars_to_tokens(len(text))


# ---------------------------------------------------------------------------
# _chars_to_tokens helper
# ---------------------------------------------------------------------------


class TestCharsToTokens:
    def test_exact_divisible(self):
        assert _chars_to_tokens(8) == 2  # 8 / 4 = 2

    def test_ceiling_rounding(self):
        assert _chars_to_tokens(5) == 2  # ceil(5/4) = 2
        assert _chars_to_tokens(1) == 1  # ceil(1/4) = 1

    def test_zero_chars(self):
        assert _chars_to_tokens(0) == 0


# ---------------------------------------------------------------------------
# Token accumulation
# ---------------------------------------------------------------------------


class TestTokenAccumulation:
    def test_count_input_accumulates(self):
        cm = _make_cm()
        cm.count_input("hello")  # 5 chars → ceil(5/4) = 2 tokens
        assert cm.estimated_tokens == _token_count("hello")

    def test_count_output_accumulates(self):
        cm = _make_cm()
        cm.count_output("world!")  # 6 chars → 2 tokens
        assert cm.estimated_tokens == _token_count("world!")

    def test_count_input_and_output_sum(self):
        cm = _make_cm()
        text_a = "input text here"
        text_b = "output text here"
        cm.count_input(text_a)
        cm.count_output(text_b)
        expected = _token_count(text_a) + _token_count(text_b)
        assert cm.estimated_tokens == expected

    def test_empty_string_does_not_change_count(self):
        cm = _make_cm()
        cm.count_input("")
        cm.count_output("")
        assert cm.estimated_tokens == 0

    def test_large_input_accumulates_correctly(self):
        cm = _make_cm(threshold_tokens=1_000_000)
        text = "x" * 4000  # exactly 1000 tokens
        cm.count_input(text)
        assert cm.estimated_tokens == 1000

    def test_reset_clears_token_estimate(self):
        cm = _make_cm()
        cm.count_input("lots of text")
        cm.reset()
        assert cm.estimated_tokens == 0


# ---------------------------------------------------------------------------
# check_threshold()
# ---------------------------------------------------------------------------


class TestCheckThreshold:
    def test_returns_false_below_threshold(self):
        cm = _make_cm(threshold_tokens=100)
        cm.count_input("a" * (CHARS_PER_TOKEN * 50))  # 50 tokens
        assert cm.check_threshold() is False

    def test_returns_true_at_threshold(self):
        cm = _make_cm(threshold_tokens=10)
        cm.count_input("a" * (CHARS_PER_TOKEN * 10))  # exactly 10 tokens
        assert cm.check_threshold() is True

    def test_returns_true_above_threshold(self):
        cm = _make_cm(threshold_tokens=5)
        cm.count_input("a" * (CHARS_PER_TOKEN * 100))  # 100 tokens >> 5
        assert cm.check_threshold() is True

    def test_returns_false_initially(self):
        cm = _make_cm(threshold_tokens=1000)
        assert cm.check_threshold() is False


# ---------------------------------------------------------------------------
# inject_checkpoint()
# ---------------------------------------------------------------------------


class TestInjectCheckpoint:
    def test_calls_callback_with_checkpoint_prompt(self):
        received = []
        cm = _make_cm(on_inject_checkpoint=received.append)
        cm.count_input("x" * 100)
        cm.inject_checkpoint()
        assert len(received) == 1
        assert received[0] == CHECKPOINT_PROMPT

    def test_resets_token_counter(self):
        cm = _make_cm(on_inject_checkpoint=lambda p: None)
        cm.count_input("a" * (CHARS_PER_TOKEN * 500))  # 500 tokens
        assert cm.estimated_tokens == 500
        cm.inject_checkpoint()
        assert cm.estimated_tokens == 0

    def test_increments_checkpoint_count(self):
        cm = _make_cm(on_inject_checkpoint=lambda p: None)
        assert cm.checkpoint_count == 0
        cm.inject_checkpoint()
        assert cm.checkpoint_count == 1
        cm.inject_checkpoint()
        assert cm.checkpoint_count == 2

    def test_no_callback_logs_warning_and_still_resets(self, caplog):
        """inject_checkpoint with no callback logs a warning but resets the counter."""
        import logging
        cm = _make_cm(on_inject_checkpoint=None)
        cm.count_input("a" * 100)

        with caplog.at_level(logging.WARNING, logger="claude_runner.context_manager"):
            cm.inject_checkpoint()

        assert cm.estimated_tokens == 0
        assert cm.checkpoint_count == 1
        assert any(
            "no on_inject_checkpoint" in r.message.lower()
            or "not sent" in r.message.lower()
            or "not registered" in r.message.lower()
            for r in caplog.records
        )

    def test_callback_exception_propagates(self):
        """If the callback raises, inject_checkpoint re-raises the exception."""
        def bad_cb(prompt):
            raise RuntimeError("write failed")

        cm = _make_cm(on_inject_checkpoint=bad_cb)
        with pytest.raises(RuntimeError, match="write failed"):
            cm.inject_checkpoint()

    def test_invalid_threshold_raises_on_construction(self):
        with pytest.raises(ValueError, match="positive integer"):
            ContextManager(threshold_tokens=0)


# ---------------------------------------------------------------------------
# build_resume_prompt() — "continue" strategy
# ---------------------------------------------------------------------------


class TestBuildResumePromptContinue:
    def test_continue_returns_literal_continue(self):
        cm = _make_cm(inject_log_on_resume=False)
        result = cm.build_resume_prompt("", STRATEGY_CONTINUE)
        assert result == "continue"

    def test_continue_ignores_base_resume_arg(self):
        cm = _make_cm(inject_log_on_resume=False)
        result = cm.build_resume_prompt("something else entirely", STRATEGY_CONTINUE)
        assert result == "continue"

    def test_continue_with_inject_log_prepends_log(self, tmp_path):
        """inject_log_on_resume=True prepends the progress log to 'continue'."""
        log_file = tmp_path / "progress.log"
        log_file.write_text("step 1 done\nstep 2 done", encoding="utf-8")
        cm = _make_cm(
            inject_log_on_resume=True,
            progress_log_path=log_file,
        )
        result = cm.build_resume_prompt("", STRATEGY_CONTINUE)
        assert "progress.log" in result
        assert "step 1 done" in result
        assert "continue" in result

    def test_continue_inject_log_missing_file_still_returns_continue(self, tmp_path):
        """When progress.log is missing with inject_log_on_resume=True, returns 'continue'."""
        cm = _make_cm(
            inject_log_on_resume=True,
            progress_log_path=tmp_path / "missing.log",
        )
        result = cm.build_resume_prompt("", STRATEGY_CONTINUE)
        assert result == "continue"


# ---------------------------------------------------------------------------
# build_resume_prompt() — "restate" strategy
# ---------------------------------------------------------------------------


class TestBuildResumePromptRestate:
    def test_restate_uses_original_prompt(self):
        cm = _make_cm(inject_log_on_resume=False)
        cm.set_original_prompt("Build the thing from scratch.")
        result = cm.build_resume_prompt("", STRATEGY_RESTATE)
        assert "Build the thing from scratch." in result
        assert "Continue from where you left off" in result

    def test_restate_without_original_prompt_falls_back_to_continue(self, caplog):
        """Without set_original_prompt(), restate falls back to 'continue'."""
        import logging
        cm = _make_cm(inject_log_on_resume=False)
        with caplog.at_level(logging.WARNING, logger="claude_runner.context_manager"):
            result = cm.build_resume_prompt("", STRATEGY_RESTATE)
        assert result == "continue"
        assert any("original_prompt" in r.message for r in caplog.records)

    def test_restate_with_inject_log_prepends_log(self, tmp_path):
        """inject_log_on_resume=True prepends progress log for restate strategy."""
        log_file = tmp_path / "progress.log"
        log_file.write_text("checkpoint alpha", encoding="utf-8")
        cm = _make_cm(
            inject_log_on_resume=True,
            progress_log_path=log_file,
        )
        cm.set_original_prompt("Do the important work.")
        result = cm.build_resume_prompt("", STRATEGY_RESTATE)
        assert "checkpoint alpha" in result
        assert "Do the important work." in result


# ---------------------------------------------------------------------------
# build_resume_prompt() — "summarize" strategy
# ---------------------------------------------------------------------------


class TestBuildResumePromptSummarize:
    def test_summarize_reads_progress_log(self, tmp_path):
        log_file = tmp_path / "progress.log"
        log_file.write_text("[DONE] Built the module.\n[PHASE] Writing tests.", encoding="utf-8")
        cm = _make_cm(
            inject_log_on_resume=False,
            progress_log_path=log_file,
        )
        result = cm.build_resume_prompt("", STRATEGY_SUMMARIZE)
        assert "[DONE] Built the module." in result
        assert "Continue from where you left off" in result

    def test_summarize_missing_log_falls_back_to_continue(self, tmp_path, caplog):
        import logging
        cm = _make_cm(
            inject_log_on_resume=False,
            progress_log_path=tmp_path / "no_file.log",
        )
        with caplog.at_level(logging.WARNING, logger="claude_runner.context_manager"):
            result = cm.build_resume_prompt("", STRATEGY_SUMMARIZE)
        assert result == "continue"

    def test_summarize_does_not_double_prepend_log(self, tmp_path):
        """inject_log_on_resume has no extra effect on summarize — log is already in core."""
        log_file = tmp_path / "progress.log"
        log_file.write_text("the log content", encoding="utf-8")
        cm = _make_cm(
            inject_log_on_resume=True,  # should NOT double-insert for summarize
            progress_log_path=log_file,
        )
        result = cm.build_resume_prompt("", STRATEGY_SUMMARIZE)
        # The log content should appear once, not twice.
        assert result.count("the log content") == 1

    def test_invalid_strategy_raises(self):
        cm = _make_cm()
        with pytest.raises(ValueError, match="Unknown resume strategy"):
            cm.build_resume_prompt("", "teleport")


# ---------------------------------------------------------------------------
# read_progress_log()
# ---------------------------------------------------------------------------


class TestReadProgressLog:
    def test_returns_empty_string_when_path_not_set(self):
        cm = _make_cm(progress_log_path=None)
        assert cm.read_progress_log() == ""

    def test_returns_empty_string_for_missing_file(self, tmp_path):
        cm = _make_cm(progress_log_path=tmp_path / "no_such_file.log")
        assert cm.read_progress_log() == ""

    def test_returns_empty_string_for_blank_file(self, tmp_path):
        log_file = tmp_path / "progress.log"
        log_file.write_text("   \n\n  ", encoding="utf-8")
        cm = _make_cm(progress_log_path=log_file)
        assert cm.read_progress_log() == ""

    def test_returns_stripped_content(self, tmp_path):
        log_file = tmp_path / "progress.log"
        log_file.write_text("\n  log line 1\n  log line 2\n\n", encoding="utf-8")
        cm = _make_cm(progress_log_path=log_file)
        result = cm.read_progress_log()
        assert "log line 1" in result
        assert "log line 2" in result
        assert not result.startswith("\n")
        assert not result.endswith("\n")


# ---------------------------------------------------------------------------
# Properties and set_original_prompt / set_on_inject_checkpoint
# ---------------------------------------------------------------------------


class TestProperties:
    def test_threshold_tokens_property(self):
        cm = _make_cm(threshold_tokens=99_999)
        assert cm.threshold_tokens == 99_999

    def test_checkpoint_count_starts_at_zero(self):
        cm = _make_cm()
        assert cm.checkpoint_count == 0

    def test_usage_fraction_below_threshold(self):
        cm = _make_cm(threshold_tokens=1000)
        cm.count_input("a" * (CHARS_PER_TOKEN * 250))  # 250 tokens = 25%
        fraction = cm.usage_fraction()
        assert 0.24 < fraction < 0.26

    def test_usage_fraction_above_threshold(self):
        cm = _make_cm(threshold_tokens=10)
        cm.count_input("a" * (CHARS_PER_TOKEN * 100))
        assert cm.usage_fraction() > 1.0

    def test_set_on_inject_checkpoint_updates_callback(self):
        cm = _make_cm()
        received = []
        cm.set_on_inject_checkpoint(received.append)
        cm.inject_checkpoint()
        assert len(received) == 1
        assert received[0] == CHECKPOINT_PROMPT

    def test_repr_includes_key_info(self):
        cm = _make_cm(threshold_tokens=5000)
        r = repr(cm)
        assert "ContextManager" in r
        assert "5000" in r


# ---------------------------------------------------------------------------
# context_anchors
# ---------------------------------------------------------------------------

class TestContextAnchors:
    ANCHORS = "Read interfaces first.\nNo side effects outside task scope."

    def _cm(self, **kw) -> ContextManager:
        return ContextManager(threshold_tokens=100, context_anchors=self.ANCHORS, **kw)

    def test_context_anchors_active_true_when_set(self):
        cm = self._cm()
        assert cm.context_anchors_active is True

    def test_context_anchors_active_false_when_not_set(self):
        cm = _make_cm()
        assert cm.context_anchors_active is False

    def test_anchors_none_when_whitespace_only(self):
        cm = ContextManager(threshold_tokens=100, context_anchors="   \n  ")
        assert cm.context_anchors_active is False

    def test_checkpoint_prompt_prepended_with_anchors(self):
        received = []
        cm = self._cm(on_inject_checkpoint=received.append)
        cm.inject_checkpoint()
        assert len(received) == 1
        injected = received[0]
        assert injected.startswith(self.ANCHORS)
        assert CHECKPOINT_PROMPT.strip() in injected

    def test_resume_continue_prepended_with_anchors(self):
        cm = self._cm()
        prompt = cm.build_resume_prompt("", STRATEGY_CONTINUE)
        assert prompt.startswith(self.ANCHORS)
        assert "continue" in prompt

    def test_resume_restate_prepended_with_anchors(self):
        cm = self._cm()
        cm.set_original_prompt("Do the task.")
        prompt = cm.build_resume_prompt("", STRATEGY_RESTATE)
        assert prompt.startswith(self.ANCHORS)
        assert "Do the task." in prompt

    def test_resume_summarize_prepended_with_anchors(self, tmp_path):
        log_file = tmp_path / "progress.log"
        log_file.write_text("[2026-01-01] [DONE] Step 1 complete", encoding="utf-8")
        cm = ContextManager(
            threshold_tokens=100,
            context_anchors=self.ANCHORS,
            progress_log_path=log_file,
        )
        prompt = cm.build_resume_prompt("", STRATEGY_SUMMARIZE)
        assert prompt.startswith(self.ANCHORS)
        assert "Step 1 complete" in prompt

    def test_no_anchors_does_not_modify_prompt(self):
        cm = _make_cm()
        prompt = cm.build_resume_prompt("", STRATEGY_CONTINUE)
        assert prompt == "continue"

    def test_anchors_separated_by_blank_line(self):
        cm = self._cm()
        prompt = cm.build_resume_prompt("", STRATEGY_CONTINUE)
        # anchors + "\n\n" + body
        assert self.ANCHORS + "\n\n" in prompt
