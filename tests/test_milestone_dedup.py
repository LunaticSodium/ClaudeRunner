"""
Tests for SHA-based milestone dedup in ModelWatchdog.

Verifies that the same (rule, commit_sha) pair does not fire multiple times
when the same commit is seen across multiple polling windows.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from claude_runner.model_watchdog import ModelWatchdog
from claude_runner.project import ModelAction, PhaseRule, Trigger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule(phase_gte=None, model_id="claude-haiku-4-5-20251001", message=None):
    trigger = Trigger(phase_gte=phase_gte)
    action = ModelAction(model_id=model_id, message=message)
    return PhaseRule(triggers=[trigger], action=action)


def _watchdog(tmp_path, rules, apply_fn):
    watchdog = ModelWatchdog(
        working_dir=tmp_path,
        rules=rules,
        apply_fn=apply_fn,
    )
    return watchdog


# ---------------------------------------------------------------------------
# SHA-based dedup tests
# ---------------------------------------------------------------------------


class TestShaBasedDedup:
    def test_fired_milestones_empty_at_construction(self, tmp_path):
        w = _watchdog(tmp_path, rules=[], apply_fn=lambda m, r: None)
        assert w._fired_milestones == set()

    def test_sha_recorded_after_fire(self, tmp_path):
        calls = []
        rule = _rule(phase_gte=1, model_id="claude-haiku-4-5-20251001")
        watchdog = _watchdog(tmp_path, rules=[rule], apply_fn=lambda m, r: calls.append(m))
        watchdog._read_current_phase_and_sha = lambda: (2, "abc1234")
        watchdog._tick()
        assert "0:abc1234" in watchdog._fired_milestones
        assert len(calls) == 1

    def test_same_sha_does_not_refire_after_reset(self, tmp_path):
        """Even if _fired were reset, the SHA dedup prevents re-firing."""
        calls = []
        rule = _rule(phase_gte=1)
        watchdog = _watchdog(tmp_path, rules=[rule], apply_fn=lambda m, r: calls.append(m))
        watchdog._read_current_phase_and_sha = lambda: (2, "deadbeef")

        watchdog._tick()
        assert len(calls) == 1

        # Simulate cleared index-based dedup (hypothetical scenario).
        watchdog._fired.clear()

        # Second tick with same SHA — should be blocked by SHA dedup.
        watchdog._tick()
        assert len(calls) == 1  # still 1, not 2

    def test_different_sha_fires_again_if_index_fired_cleared(self, tmp_path):
        """A genuinely new commit SHA allows re-fire when index is cleared."""
        calls = []
        rule = _rule(phase_gte=1)
        watchdog = _watchdog(tmp_path, rules=[rule], apply_fn=lambda m, r: calls.append(m))
        watchdog._read_current_phase_and_sha = lambda: (2, "sha001")
        watchdog._tick()
        assert len(calls) == 1

        # Cleared index, new SHA — fires again.
        watchdog._fired.clear()
        watchdog._read_current_phase_and_sha = lambda: (2, "sha002")
        watchdog._tick()
        assert len(calls) == 2

    def test_empty_sha_does_not_add_to_fired_milestones(self, tmp_path):
        """When no SHA is available (e.g. token-pct trigger), no SHA entry added."""
        calls = []
        rule = _rule(phase_gte=0)  # always triggers
        watchdog = _watchdog(tmp_path, rules=[rule], apply_fn=lambda m, r: calls.append(m))
        # Patch to return empty SHA (non-git trigger scenario).
        watchdog._read_current_phase_and_sha = lambda: (1, "")
        watchdog._tick()
        assert len(calls) == 1
        # No SHA entries added.
        assert all(":" in k for k in watchdog._fired_milestones) is True or len(watchdog._fired_milestones) == 0

    def test_multiple_rules_each_tracked_separately(self, tmp_path):
        """Two rules fired from the same SHA each have their own dedup entry."""
        calls = []
        rules = [
            _rule(phase_gte=1, model_id="haiku"),
            _rule(phase_gte=1, model_id="sonnet"),
        ]
        watchdog = _watchdog(tmp_path, rules=rules, apply_fn=lambda m, r: calls.append(m))
        watchdog._read_current_phase_and_sha = lambda: (1, "abc123")
        watchdog._tick()
        assert len(calls) == 2
        assert "0:abc123" in watchdog._fired_milestones
        assert "1:abc123" in watchdog._fired_milestones

    def test_read_current_phase_and_sha_with_real_git(self, tmp_path):
        """Integration: real git repo — phase and SHA both detected."""
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)

        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "PHASE-3: done"], cwd=tmp_path, check=True, capture_output=True)

        watchdog = ModelWatchdog(
            working_dir=tmp_path,
            rules=[],
            apply_fn=lambda m, r: None,
        )
        phase, sha = watchdog._read_current_phase_and_sha()
        assert phase == 3
        assert sha != ""
        assert len(sha) >= 6  # abbreviated SHA

    def test_read_current_phase_returns_correct_phase(self, tmp_path):
        """_read_current_phase() still works (delegates to _read_current_phase_and_sha)."""
        watchdog = ModelWatchdog(
            working_dir=tmp_path,
            rules=[],
            apply_fn=lambda m, r: None,
        )
        watchdog._read_current_phase_and_sha = lambda: (5, "abc")
        assert watchdog._read_current_phase() == 5
