"""
tests/test_milestone_and_git.py

Unit tests for Phase 3 features:
  - Milestone detection in runner._check_milestones()
  - Git workflow in runner._run_git_workflow()

These tests exercise the logic in isolation without spawning a real subprocess
or a real Claude Code process.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — build a minimal TaskRunner-like object for unit testing
# ---------------------------------------------------------------------------

def _make_runner(milestones=None, git_cfg=None, working_dir=None):
    """
    Return a TaskRunner instance wired with test doubles.

    We import TaskRunner here to keep the import inside the test so that
    missing optional deps don't break the whole test module.
    """
    from claude_runner.runner import TaskRunner

    # Minimal project book namespace
    ms_list = milestones or []
    exec_ns = SimpleNamespace(
        milestones=ms_list,
        timeout_hours=1.0,
        max_rate_limit_waits=3,
        resume_strategy="continue",
        context=SimpleNamespace(
            checkpoint_threshold_tokens=150_000,
            reset_on_rate_limit=True,
            inject_log_on_resume=True,
        ),
    )
    git_ns = git_cfg or SimpleNamespace(enabled=False, branch_prefix="claude-task/", auto_push=False)
    output_ns = SimpleNamespace(git=git_ns, log_dir=None)
    sandbox_ns = SimpleNamespace(working_dir=Path(working_dir) if working_dir else None)
    book = SimpleNamespace(
        name="test-task",
        prompt="Do the thing.",
        description="",
        context_anchors=None,
        sandbox=sandbox_ns,
        execution=exec_ns,
        output=output_ns,
        notify=SimpleNamespace(on=[], channels=[]),
    )

    config = SimpleNamespace(sandbox_backend="native", log_dir=Path("/tmp"), state_dir=Path("/tmp"))

    runner = TaskRunner.__new__(TaskRunner)
    runner._book = book
    runner._config = config
    runner._api_key = "test-key"
    runner._tui = None
    runner._tui_callback = None
    runner._resume = False
    runner._secrets_config = None
    runner._sandbox = MagicMock()
    runner._notifier = MagicMock()
    runner._persistence = MagicMock()
    runner._context_manager = MagicMock()
    runner._context_manager.checkpoint_count = 0
    runner._rate_detector = MagicMock()
    runner._rate_waiter = None
    runner._rate_limit_event = None
    runner._rate_limit_reset_time = None
    runner._start_time = None
    runner._rate_limit_cycles = 0
    runner._fault_log = []
    runner._process = None
    runner._output_lines = []
    runner._fs_snapshot_start = {}
    runner._milestones_fired = set()
    runner._checkpoint_task = None
    runner._book_path = None
    runner._project_id = "test-task"

    # Compile milestone patterns just as _initialise() would
    runner._milestone_patterns = []
    for ms in ms_list:
        pat_str = getattr(ms, "pattern", "")
        msg_str = getattr(ms, "message", pat_str)
        if pat_str:
            try:
                runner._milestone_patterns.append((re.compile(pat_str), msg_str))
            except re.error:
                pass

    return runner


def _milestone(pattern: str, message: str) -> SimpleNamespace:
    return SimpleNamespace(pattern=pattern, message=message)


# ===========================================================================
# Milestone detection tests
# ===========================================================================

class TestMilestoneDetection:

    def test_no_patterns_no_dispatch(self):
        runner = _make_runner()
        runner._check_milestones("All tests passing")
        runner._notifier.dispatch.assert_not_called()

    def test_exact_substring_match_fires(self):
        ms = _milestone("All tests passing", "Tests are green")
        runner = _make_runner(milestones=[ms])
        runner._check_milestones("All tests passing — 42 passed, 0 failed")
        runner._notifier.dispatch.assert_called_once_with("milestone", "Tests are green")

    def test_no_match_does_not_fire(self):
        ms = _milestone("All tests passing", "Tests are green")
        runner = _make_runner(milestones=[ms])
        runner._check_milestones("Some tests failing")
        runner._notifier.dispatch.assert_not_called()

    def test_fires_only_once_per_message(self):
        ms = _milestone("Committed:", "Claude made a commit")
        runner = _make_runner(milestones=[ms])
        runner._check_milestones("Committed: abc1234 'Add feature'")
        runner._check_milestones("Committed: def5678 'Fix bug'")
        # Second match should be suppressed
        assert runner._notifier.dispatch.call_count == 1

    def test_multiple_milestones_independent(self):
        ms1 = _milestone("All tests passing", "Tests green")
        ms2 = _milestone("Committed:", "Made a commit")
        runner = _make_runner(milestones=[ms1, ms2])
        runner._check_milestones("All tests passing")
        runner._check_milestones("Committed: abc123")
        assert runner._notifier.dispatch.call_count == 2
        calls = [c.args for c in runner._notifier.dispatch.call_args_list]
        assert ("milestone", "Tests green") in calls
        assert ("milestone", "Made a commit") in calls

    def test_regex_pattern_works(self):
        ms = _milestone(r"✓ \d+ tests? passed", "Tests done")
        runner = _make_runner(milestones=[ms])
        runner._check_milestones("✓ 42 tests passed")
        runner._notifier.dispatch.assert_called_once_with("milestone", "Tests done")

    def test_regex_pattern_no_match(self):
        ms = _milestone(r"✓ \d+ tests? passed", "Tests done")
        runner = _make_runner(milestones=[ms])
        runner._check_milestones("0 tests passed")
        runner._notifier.dispatch.assert_not_called()

    def test_case_sensitive_by_default(self):
        ms = _milestone("all tests passing", "Tests green")
        runner = _make_runner(milestones=[ms])
        runner._check_milestones("All Tests Passing")
        # Default re.compile is case-sensitive
        runner._notifier.dispatch.assert_not_called()

    def test_milestones_fired_set_updated(self):
        ms = _milestone("deploy complete", "Deployed")
        runner = _make_runner(milestones=[ms])
        assert "Deployed" not in runner._milestones_fired
        runner._check_milestones("deploy complete: v1.2.3")
        assert "Deployed" in runner._milestones_fired

    def test_milestone_dispatches_correct_event_type(self):
        ms = _milestone("DONE", "Task done")
        runner = _make_runner(milestones=[ms])
        runner._check_milestones("DONE")
        event_arg = runner._notifier.dispatch.call_args.args[0]
        assert event_arg == "milestone"

    def test_notifier_none_does_not_crash(self):
        ms = _milestone("DONE", "Done")
        runner = _make_runner(milestones=[ms])
        runner._notifier = None  # simulate uninitialised notifier
        # Should not raise
        runner._check_milestones("DONE")

    def test_invalid_regex_skipped_at_init(self):
        # _make_runner silently skips bad patterns
        ms = _milestone("[invalid(regex", "Bad")
        runner = _make_runner(milestones=[ms])
        assert len(runner._milestone_patterns) == 0

    def test_empty_line_does_not_crash(self):
        ms = _milestone("DONE", "Done")
        runner = _make_runner(milestones=[ms])
        runner._check_milestones("")  # should not raise


# ===========================================================================
# Git workflow tests
# ===========================================================================

class TestGitWorkflow:

    def _git_runner(self, tmp_path, auto_push=False, enabled=True):
        git_cfg = SimpleNamespace(
            enabled=enabled,
            branch_prefix="claude-task/",
            auto_push=auto_push,
        )
        runner = _make_runner(git_cfg=git_cfg, working_dir=str(tmp_path))
        return runner

    def test_skipped_when_git_disabled(self, tmp_path):
        runner = self._git_runner(tmp_path, enabled=False)
        result = runner._run_git_workflow()
        assert result == ""

    def test_skipped_when_not_git_repo(self, tmp_path):
        # tmp_path has no .git directory → git rev-parse fails
        runner = self._git_runner(tmp_path)
        result = runner._run_git_workflow()
        assert result == ""

    def test_creates_branch_and_commits(self, tmp_path):
        """Full happy-path test in a real git repo."""
        # Init a real git repo in tmp_path
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"],
                       cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                       cwd=tmp_path, check=True, capture_output=True)
        # Need an initial commit so branch creation works
        (tmp_path / "readme.txt").write_text("init")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"],
                       cwd=tmp_path, check=True, capture_output=True)

        runner = self._git_runner(tmp_path)
        # Add a file to commit
        (tmp_path / "output.py").write_text("x = 1")
        result = runner._run_git_workflow()

        assert "Branch:" in result
        assert "claude-task/" in result

        # Verify the branch now exists in the repo
        branches = subprocess.run(
            ["git", "branch"], cwd=tmp_path, capture_output=True, text=True
        )
        assert "claude-task/" in branches.stdout

    def test_branch_name_includes_task_slug(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"],
                       cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"],
                       cwd=tmp_path, check=True, capture_output=True)

        runner = self._git_runner(tmp_path)
        result = runner._run_git_workflow()
        # task name is "test-task" → slug "test-task"
        assert "test-task" in result

    def test_auto_push_false_no_push_call(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"],
                       cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"],
                       cwd=tmp_path, check=True, capture_output=True)

        runner = self._git_runner(tmp_path, auto_push=False)
        result = runner._run_git_workflow()
        # No "Pushed:" line since auto_push=False
        assert "Pushed:" not in result

    def test_fault_log_updated_on_commit_failure(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"],
                       cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"],
                       cwd=tmp_path, check=True, capture_output=True)

        runner = self._git_runner(tmp_path)
        # Simulate commit failure by patching subprocess.run for the commit step only
        original_run = subprocess.run

        def patched_run(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "commit"]:
                return SimpleNamespace(returncode=1, stderr="simulated failure", stdout="")
            return original_run(cmd, *args, **kwargs)

        with patch("claude_runner.runner.subprocess.run", side_effect=patched_run):
            runner._run_git_workflow()

        # fault_log should contain the git commit warning
        assert any("git" in entry for entry in runner._fault_log)
