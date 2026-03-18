"""
Tests for claude_runner.preflight module.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_runner.preflight import PreflightError, run_preflight


# ---------------------------------------------------------------------------
# Helpers — minimal ProjectBook-like objects
# ---------------------------------------------------------------------------


def _book(preflight_cfg=None, model_schedule=None, notify=None):
    """Return a minimal mock ProjectBook."""
    book = MagicMock()
    book.preflight = preflight_cfg
    book.model_schedule = model_schedule
    book.notify = notify
    return book


def _preflight_cfg(required_env=None, skip=False):
    cfg = MagicMock()
    cfg.required_env = required_env or []
    cfg.skip = skip
    return cfg


# ---------------------------------------------------------------------------
# Check 1: working_dir exists
# ---------------------------------------------------------------------------


class TestWorkingDirExists:
    def test_raises_when_dir_missing(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        with pytest.raises(PreflightError, match="working_dir does not exist"):
            run_preflight(_book(), missing)

    def test_passes_when_dir_exists(self, tmp_path):
        warnings = run_preflight(_book(), tmp_path)
        # May have a git warning but should not raise.
        assert isinstance(warnings, list)


# ---------------------------------------------------------------------------
# Check 2: git repo (warn only)
# ---------------------------------------------------------------------------


class TestGitRepoCheck:
    def test_warns_when_no_git(self, tmp_path):
        warnings = run_preflight(_book(), tmp_path)
        git_warnings = [w for w in warnings if "not a git repository" in w]
        assert len(git_warnings) == 1

    def test_no_git_warning_when_git_present(self, tmp_path):
        (tmp_path / ".git").mkdir()
        warnings = run_preflight(_book(), tmp_path)
        git_warnings = [w for w in warnings if "not a git repository" in w]
        assert len(git_warnings) == 0


# ---------------------------------------------------------------------------
# Check 3: skip flags
# ---------------------------------------------------------------------------


class TestSkipFlag:
    def test_skip_arg_returns_empty(self, tmp_path):
        missing = tmp_path / "no_such_dir"
        # Would raise PreflightError if not skipped.
        result = run_preflight(_book(), missing, skip=True)
        assert result == []

    def test_project_skip_flag_returns_empty(self, tmp_path):
        missing = tmp_path / "no_such_dir"
        book = _book(preflight_cfg=_preflight_cfg(skip=True))
        result = run_preflight(book, missing)
        assert result == []


# ---------------------------------------------------------------------------
# Check 4: required_env
# ---------------------------------------------------------------------------


class TestRequiredEnv:
    def test_raises_when_var_missing(self, tmp_path):
        var = "TEST_PREFLIGHT_VAR_XYZ_MISSING"
        # Ensure the var is not set.
        os.environ.pop(var, None)
        book = _book(preflight_cfg=_preflight_cfg(required_env=[var]))
        with pytest.raises(PreflightError, match=var):
            run_preflight(book, tmp_path)

    def test_passes_when_var_set(self, tmp_path):
        var = "TEST_PREFLIGHT_VAR_XYZ_PRESENT"
        os.environ[var] = "some_value"
        try:
            book = _book(preflight_cfg=_preflight_cfg(required_env=[var]))
            warnings = run_preflight(book, tmp_path)
            assert not any(var in w for w in warnings)
        finally:
            os.environ.pop(var, None)

    def test_multiple_vars_one_missing(self, tmp_path):
        present_var = "TEST_PREFLIGHT_PRESENT_XYZ"
        missing_var = "TEST_PREFLIGHT_MISSING_XYZ"
        os.environ[present_var] = "ok"
        os.environ.pop(missing_var, None)
        try:
            book = _book(preflight_cfg=_preflight_cfg(required_env=[present_var, missing_var]))
            with pytest.raises(PreflightError, match=missing_var):
                run_preflight(book, tmp_path)
        finally:
            os.environ.pop(present_var, None)


# ---------------------------------------------------------------------------
# Check 5: ntfy (network — mocked)
# ---------------------------------------------------------------------------


class TestNtfyCheck:
    def test_no_warning_when_no_notify_channels(self, tmp_path):
        notify = MagicMock()
        notify.channels = []
        book = _book(notify=notify)
        warnings = run_preflight(book, tmp_path)
        ntfy_warnings = [w for w in warnings if "ntfy" in w.lower()]
        assert len(ntfy_warnings) == 0

    def test_warns_when_ntfy_unreachable(self, tmp_path):
        """Simulate a reachable channel name but failed requests.get."""
        # Create a fake .git dir so we don't also get a git warning.
        (tmp_path / ".git").mkdir()
        notify = MagicMock()
        notify.channels = [MagicMock()]
        book = _book(notify=notify)

        with patch("claude_runner.preflight._find_ntfy_channel", return_value="test-channel"), \
             patch("requests.get", side_effect=ConnectionError("refused")):
            warnings = run_preflight(book, tmp_path)

        ntfy_warnings = [w for w in warnings if "ntfy" in w.lower() or "unreachable" in w.lower()]
        assert len(ntfy_warnings) == 1

    def test_no_ntfy_warning_when_reachable(self, tmp_path):
        """Simulate a reachable ntfy server."""
        (tmp_path / ".git").mkdir()
        notify = MagicMock()
        notify.channels = [MagicMock()]
        book = _book(notify=notify)

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("claude_runner.preflight._find_ntfy_channel", return_value="test-channel"), \
             patch("requests.get", return_value=mock_response):
            warnings = run_preflight(book, tmp_path)

        ntfy_warnings = [w for w in warnings if "ntfy" in w.lower()]
        assert len(ntfy_warnings) == 0


# ---------------------------------------------------------------------------
# Integration: all checks together
# ---------------------------------------------------------------------------


class TestRunPreflightIntegration:
    def test_returns_list_of_strings(self, tmp_path):
        result = run_preflight(_book(), tmp_path)
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, str)

    def test_no_preflight_config_no_required_env_check(self, tmp_path):
        """Without preflight config, required_env check is skipped."""
        book = _book(preflight_cfg=None)
        result = run_preflight(book, tmp_path)
        assert isinstance(result, list)
