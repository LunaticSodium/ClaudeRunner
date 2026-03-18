"""
Tests for claude_runner.model_watchdog.ModelWatchdog.

All tests are purely unit-level: no Docker, no real git, no network.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_runner.model_watchdog import ModelWatchdog, _PHASE_RE
from claude_runner.project import ModelAction, ModelSchedule, PhaseRule, Trigger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rule(phase_gte=None, phase_lte=None, token_pct_gte=None, model_id="claude-haiku-4-5-20251001", message=None):
    trigger = Trigger(
        phase_gte=phase_gte,
        phase_lte=phase_lte,
        token_pct_gte=token_pct_gte,
    )
    action = ModelAction(model_id=model_id, message=message)
    return PhaseRule(triggers=[trigger], action=action)


# ---------------------------------------------------------------------------
# Trigger.matches() unit tests
# ---------------------------------------------------------------------------

class TestTriggerMatches:
    def test_no_conditions_always_matches(self):
        t = Trigger()
        assert t.matches(phase=0, token_pct=0.0) is True
        assert t.matches(phase=99, token_pct=1.0) is True

    def test_phase_gte_matches(self):
        t = Trigger(phase_gte=3)
        assert t.matches(phase=2, token_pct=0.0) is False
        assert t.matches(phase=3, token_pct=0.0) is True
        assert t.matches(phase=5, token_pct=0.0) is True

    def test_phase_lte_matches(self):
        t = Trigger(phase_lte=3)
        assert t.matches(phase=3, token_pct=0.0) is True
        assert t.matches(phase=4, token_pct=0.0) is False

    def test_phase_range_matches(self):
        t = Trigger(phase_gte=2, phase_lte=4)
        assert t.matches(phase=1, token_pct=0.0) is False
        assert t.matches(phase=2, token_pct=0.0) is True
        assert t.matches(phase=4, token_pct=0.0) is True
        assert t.matches(phase=5, token_pct=0.0) is False

    def test_token_pct_gte_matches(self):
        t = Trigger(token_pct_gte=0.7)
        assert t.matches(phase=0, token_pct=0.69) is False
        assert t.matches(phase=0, token_pct=0.7) is True
        assert t.matches(phase=0, token_pct=1.0) is True

    def test_combined_all_must_match(self):
        t = Trigger(phase_gte=2, token_pct_gte=0.5)
        assert t.matches(phase=1, token_pct=0.9) is False   # phase fails
        assert t.matches(phase=2, token_pct=0.4) is False   # token fails
        assert t.matches(phase=2, token_pct=0.5) is True    # both pass


# ---------------------------------------------------------------------------
# _PHASE_RE unit tests
# ---------------------------------------------------------------------------

class TestPhaseRegex:
    @pytest.mark.parametrize("line,expected", [
        ("PHASE-1: bootstrap done", 1),
        ("phase-3: strategies implemented", 3),
        ("PHASE-10: big number", 10),
        ("No phase here", None),
        ("feature: PHASE-2 in body", None),  # not at start
    ])
    def test_phase_re(self, line, expected):
        m = _PHASE_RE.match(line)
        if expected is None:
            assert m is None
        else:
            assert m is not None
            assert int(m.group(1)) == expected


# ---------------------------------------------------------------------------
# ModelWatchdog._read_current_phase()
# ---------------------------------------------------------------------------

class TestReadCurrentPhase:
    def test_no_git_dir_returns_zero(self, tmp_path):
        watchdog = ModelWatchdog(
            working_dir=tmp_path,
            rules=[],
            apply_fn=lambda m, r: None,
        )
        assert watchdog._read_current_phase() == 0

    def test_reads_phase_from_git_log(self, tmp_path):
        # Initialise a real git repo and make commits.
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        # Commit 1 — no phase marker.
        (tmp_path / "a.txt").write_text("a")
        subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)
        # Commit 2 — PHASE-2.
        (tmp_path / "b.txt").write_text("b")
        subprocess.run(["git", "add", "b.txt"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "PHASE-2: scaffold done"], cwd=tmp_path, check=True, capture_output=True)

        watchdog = ModelWatchdog(
            working_dir=tmp_path,
            rules=[],
            apply_fn=lambda m, r: None,
        )
        assert watchdog._read_current_phase() == 2

    def test_returns_highest_phase(self, tmp_path):
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)

        for phase in [1, 3, 2]:
            (tmp_path / f"f{phase}.txt").write_text(str(phase))
            subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", f"PHASE-{phase}: done"],
                cwd=tmp_path, check=True, capture_output=True,
            )

        watchdog = ModelWatchdog(
            working_dir=tmp_path,
            rules=[],
            apply_fn=lambda m, r: None,
        )
        assert watchdog._read_current_phase() == 3


# ---------------------------------------------------------------------------
# ModelWatchdog._fire() / rule deduplication
# ---------------------------------------------------------------------------

def _watchdog_with_phase(tmp_path, rules, apply_fn, phase=0, token_pct=0.0, get_token_pct=None):
    """Build a watchdog and patch _read_current_phase_and_sha to return *phase*."""
    watchdog = ModelWatchdog(
        working_dir=tmp_path,
        rules=rules,
        apply_fn=apply_fn,
        get_token_pct=get_token_pct,
    )
    # Patch both _read_current_phase (for callers that use it directly) and
    # _read_current_phase_and_sha (used by _tick after the SHA-dedup refactor).
    watchdog._read_current_phase = lambda: phase
    watchdog._read_current_phase_and_sha = lambda: (phase, "")
    return watchdog


class TestModelWatchdogFiring:
    def test_apply_fn_called_on_trigger(self, tmp_path):
        calls = []
        rule = _rule(phase_gte=2, model_id="claude-haiku-4-5-20251001", message="switch to haiku")
        watchdog = _watchdog_with_phase(
            tmp_path, rules=[rule],
            apply_fn=lambda m, r: calls.append((m, r)),
            phase=2,
        )
        watchdog._tick()
        assert len(calls) == 1
        assert calls[0][0] == "claude-haiku-4-5-20251001"

    def test_rule_fires_at_most_once(self, tmp_path):
        calls = []
        rule = _rule(phase_gte=1)
        watchdog = _watchdog_with_phase(
            tmp_path, rules=[rule],
            apply_fn=lambda m, r: calls.append(m),
            phase=3,
        )
        watchdog._tick()
        watchdog._tick()
        watchdog._tick()
        assert len(calls) == 1  # fired exactly once

    def test_multiple_rules_phase_advances(self, tmp_path):
        """Two rules fire as phase advances: rule0 at phase<=2, rule1 at phase>=3."""
        calls = []
        rules = [
            _rule(phase_gte=1, phase_lte=2, model_id="haiku"),
            _rule(phase_gte=3, model_id="sonnet"),
        ]
        # Phase=1 — only rule0 matches.
        watchdog = _watchdog_with_phase(tmp_path, rules=rules,
                                        apply_fn=lambda m, r: calls.append(m), phase=1)
        watchdog._tick()
        assert calls == ["haiku"]

        # Advance phase to 4 — rule1 now matches.
        watchdog._read_current_phase = lambda: 4
        watchdog._read_current_phase_and_sha = lambda: (4, "")
        watchdog._tick()
        assert calls == ["haiku", "sonnet"]

        # No more firings.
        watchdog._tick()
        assert len(calls) == 2

    def test_unfired_rule_not_triggered_when_trigger_fails(self, tmp_path):
        calls = []
        rule = _rule(phase_gte=5)
        watchdog = _watchdog_with_phase(
            tmp_path, rules=[rule],
            apply_fn=lambda m, r: calls.append(m),
            phase=3,  # below threshold
        )
        watchdog._tick()
        assert calls == []

    def test_token_pct_condition(self, tmp_path):
        calls = []
        rule = _rule(token_pct_gte=0.8)
        token_pct_value = [0.5]
        watchdog = _watchdog_with_phase(
            tmp_path, rules=[rule],
            apply_fn=lambda m, r: calls.append(m),
            phase=0,
            get_token_pct=lambda: token_pct_value[0],
        )
        watchdog._tick()
        assert calls == []  # 0.5 < 0.8, doesn't fire

        token_pct_value[0] = 0.85
        watchdog._tick()
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Start/stop lifecycle
# ---------------------------------------------------------------------------

class TestWatchdogLifecycle:
    def test_start_stop(self, tmp_path):
        watchdog = ModelWatchdog(
            working_dir=tmp_path,
            rules=[],
            apply_fn=lambda m, r: None,
            poll_interval=60.0,  # long interval so it never actually polls
        )
        watchdog.start()
        assert watchdog._thread is not None
        assert watchdog._thread.is_alive()

        watchdog.stop()
        assert not watchdog._thread.is_alive()

    def test_stop_without_start_is_safe(self, tmp_path):
        watchdog = ModelWatchdog(
            working_dir=tmp_path,
            rules=[],
            apply_fn=lambda m, r: None,
        )
        watchdog.stop()  # should not raise

    def test_current_phase_property(self, tmp_path):
        watchdog = ModelWatchdog(
            working_dir=tmp_path,
            rules=[],
            apply_fn=lambda m, r: None,
        )
        assert watchdog.current_phase == 0


# ---------------------------------------------------------------------------
# apply_fn exception handling
# ---------------------------------------------------------------------------

class TestApplyFnExceptions:
    def test_apply_fn_exception_does_not_crash_watchdog(self, tmp_path):
        def _bad_apply(model_id, reason):
            raise RuntimeError("oops")

        rule = _rule(phase_gte=1)
        watchdog = _watchdog_with_phase(
            tmp_path, rules=[rule], apply_fn=_bad_apply, phase=2
        )
        # Should not raise even though apply_fn raises.
        watchdog._tick()
        # Rule is still marked as fired so it doesn't retry endlessly.
        assert 0 in watchdog._fired
