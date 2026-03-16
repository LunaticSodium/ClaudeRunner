"""
tests/test_git_inbox.py

Tests for Feature A2: Git repository as file transfer channel.
"""
from __future__ import annotations

import pathlib
import subprocess
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

from claude_runner.git_inbox import (
    fetch_branch,
    _get_github_token,
    _embed_token,
    _try_enqueue,
)
from claude_runner.pipeline import Pipeline
from claude_runner.ntfy_client import NtfyMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_YAML = """\
name: test-project
prompt: Do something useful.
"""

INVALID_YAML = "key: [unclosed bracket"

INVALID_PROJECT_YAML = """\
name: broken-project
description: missing prompt field
"""


def _make_daemon():
    daemon = MagicMock()
    daemon.enqueue = MagicMock()
    return daemon


def _make_pipeline():
    daemon = _make_daemon()
    ntfy = MagicMock()
    return Pipeline(daemon=daemon, ntfy_client=ntfy), daemon, ntfy


# ---------------------------------------------------------------------------
# _get_github_token
# ---------------------------------------------------------------------------


class TestGetGithubToken:
    def test_returns_none_when_keyring_missing(self):
        with patch.dict("sys.modules", {"keyring": None}):
            token = _get_github_token()
        assert token is None

    def test_returns_none_when_credential_not_set(self):
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = None
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            token = _get_github_token()
        assert token is None

    def test_returns_token_when_set(self):
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "ghp_test123"
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            token = _get_github_token()
        assert token == "ghp_test123"


# ---------------------------------------------------------------------------
# _embed_token
# ---------------------------------------------------------------------------


class TestEmbedToken:
    def test_embeds_token_into_https_url(self):
        url = _embed_token("https://github.com/user/repo.git", "mytoken")
        assert url == "https://mytoken@github.com/user/repo.git"

    def test_passthrough_for_non_https(self):
        url = _embed_token("git@github.com:user/repo.git", "mytoken")
        assert url == "git@github.com:user/repo.git"


# ---------------------------------------------------------------------------
# fetch_branch: skips gracefully when no GitHub token
# ---------------------------------------------------------------------------


class TestFetchBranchNoToken:
    def test_skips_when_no_token(self, caplog):
        """fetch_branch must skip gracefully when no GitHub token is found."""
        import logging
        daemon = _make_daemon()
        with patch("claude_runner.git_inbox._get_github_token", return_value=None):
            with caplog.at_level(logging.ERROR, logger="claude_runner.git_inbox"):
                fetch_branch("task/foo", daemon)
        daemon.enqueue.assert_not_called()
        assert any("No GitHub token" in r.message for r in caplog.records)

    def test_skips_when_no_repo_url(self, caplog):
        import logging
        daemon = _make_daemon()
        with patch("claude_runner.git_inbox._get_github_token", return_value="tok"):
            with patch("claude_runner.git_inbox._get_repo_url", return_value=None):
                with caplog.at_level(logging.ERROR, logger="claude_runner.git_inbox"):
                    fetch_branch("task/bar", daemon)
        daemon.enqueue.assert_not_called()
        assert any("No GitHub repo URL" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# fetch_branch: enqueues valid project books
# ---------------------------------------------------------------------------


class TestFetchBranchEnqueue:
    def test_enqueues_valid_project_books(self, tmp_path):
        """fetch_branch enqueues valid ProjectBook YAML files found in branch."""
        # Write a valid YAML file into the temp clone dir.
        yaml_file = tmp_path / "task.yaml"
        yaml_file.write_text(VALID_YAML, encoding="utf-8")

        daemon = _make_daemon()

        with patch("claude_runner.git_inbox._get_github_token", return_value="tok"):
            with patch("claude_runner.git_inbox._get_repo_url", return_value="https://github.com/x/y"):
                with patch("claude_runner.git_inbox._clone_branch") as mock_clone:
                    # Make _clone_branch write our yaml into the dest dir.
                    def _fake_clone(auth_url, branch_ref, dest):
                        (dest / "task.yaml").write_text(VALID_YAML, encoding="utf-8")
                    mock_clone.side_effect = _fake_clone
                    import tempfile
                    with patch("tempfile.mkdtemp", return_value=str(tmp_path / "clone")):
                        (tmp_path / "clone").mkdir()
                        fetch_branch("task/my-task", daemon)

        daemon.enqueue.assert_called_once()

    def test_does_not_crash_when_clone_fails(self, tmp_path, caplog):
        """fetch_branch logs an error but does not crash on clone failure."""
        import logging
        daemon = _make_daemon()

        with patch("claude_runner.git_inbox._get_github_token", return_value="tok"):
            with patch("claude_runner.git_inbox._get_repo_url", return_value="https://github.com/x/y"):
                with patch("claude_runner.git_inbox._clone_branch", side_effect=subprocess.CalledProcessError(1, "git")):
                    with caplog.at_level(logging.WARNING, logger="claude_runner.git_inbox"):
                        # Should not raise
                        try:
                            fetch_branch("task/test", daemon)
                        except Exception:
                            pytest.fail("fetch_branch raised an exception unexpectedly")


# ---------------------------------------------------------------------------
# _try_enqueue: handles invalid YAML gracefully
# ---------------------------------------------------------------------------


class TestTryEnqueue:
    def test_enqueues_valid_yaml(self, tmp_path):
        f = tmp_path / "valid.yaml"
        f.write_text(VALID_YAML, encoding="utf-8")
        daemon = _make_daemon()
        _try_enqueue(f, daemon)
        daemon.enqueue.assert_called_once()

    def test_logs_warning_for_invalid_yaml(self, tmp_path, caplog):
        """_try_enqueue logs a warning and continues for invalid YAML files."""
        import logging
        f = tmp_path / "bad.yaml"
        f.write_text(INVALID_YAML, encoding="utf-8")
        daemon = _make_daemon()
        with caplog.at_level(logging.WARNING, logger="claude_runner.git_inbox"):
            _try_enqueue(f, daemon)
        daemon.enqueue.assert_not_called()
        assert any("skipping" in r.message for r in caplog.records)

    def test_logs_warning_for_invalid_project_book(self, tmp_path, caplog):
        """_try_enqueue logs a warning for YAML that fails ProjectBook validation."""
        import logging
        f = tmp_path / "invalid_proj.yaml"
        f.write_text(INVALID_PROJECT_YAML, encoding="utf-8")
        daemon = _make_daemon()
        with caplog.at_level(logging.WARNING, logger="claude_runner.git_inbox"):
            _try_enqueue(f, daemon)
        daemon.enqueue.assert_not_called()
        assert any("skipping" in r.message for r in caplog.records)

    def test_does_not_raise_on_any_error(self, tmp_path):
        """_try_enqueue must not raise — errors are logged and skipped."""
        f = tmp_path / "nonexistent.yaml"
        daemon = _make_daemon()
        _try_enqueue(f, daemon)
        daemon.enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# Pipeline PARSE dispatches "fetch task/foo" to git_inbox handler
# ---------------------------------------------------------------------------


class TestPipelineFetchDispatch:
    def test_fetch_task_dispatches_to_git_inbox(self):
        """'fetch task/foo' message routes to _cmd_fetch, not trashed."""
        pipeline, daemon, ntfy = _make_pipeline()

        with patch("claude_runner.pipeline.Pipeline._cmd_fetch") as mock_fetch:
            msg = NtfyMessage(id="f1", message="fetch task/myproject", timestamp=0)
            pipeline.process(msg)
            mock_fetch.assert_called_once()
            # _cmd_fetch is called with (branch_ref: str, original_message)
            branch_ref_arg = mock_fetch.call_args[0][0]
            assert branch_ref_arg == "task/myproject"

    def test_fetch_inbox_dispatches_to_git_inbox(self):
        """'fetch inbox/2026-01-01T00:00:00' message routes to _cmd_fetch."""
        pipeline, daemon, ntfy = _make_pipeline()

        with patch("claude_runner.pipeline.Pipeline._cmd_fetch") as mock_fetch:
            msg = NtfyMessage(
                id="f2",
                message="fetch inbox/2026-01-01T12:00:00",
                timestamp=0,
            )
            pipeline.process(msg)
            mock_fetch.assert_called_once()

    def test_fetch_invalid_branch_trashes(self, tmp_path):
        """'fetch invalid-ref' (bad pattern) is trashed."""
        pipeline, daemon, ntfy = _make_pipeline()

        with patch("claude_runner.pipeline._TRASH_DIR", tmp_path / "trash"):
            msg = NtfyMessage(id="f3", message="fetch bad/ref/name", timestamp=0)
            pipeline.process(msg)
            trash_files = list((tmp_path / "trash").glob("*.log"))
            assert len(trash_files) >= 1

    def test_fetch_no_branch_trashes(self, tmp_path):
        """'fetch' with no branch argument is trashed."""
        pipeline, daemon, ntfy = _make_pipeline()

        with patch("claude_runner.pipeline._TRASH_DIR", tmp_path / "trash"):
            msg = NtfyMessage(id="f4", message="fetch", timestamp=0)
            pipeline.process(msg)
            trash_files = list((tmp_path / "trash").glob("*.log"))
            assert len(trash_files) >= 1
