"""
tests/test_trash_cli.py

Unit tests for `claude-runner logs --trash` CLI.
"""
from __future__ import annotations

import pathlib
import time

import pytest
from click.testing import CliRunner

from claude_runner.main import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_trash(trash_dir: pathlib.Path, filename: str, stage: str, reason: str, original: str = "raw") -> pathlib.Path:
    """Write a fake trash log file and return its path."""
    trash_dir.mkdir(parents=True, exist_ok=True)
    path = trash_dir / filename
    content = "\n".join([
        f"timestamp: 2026-03-16T00:00:00Z",
        f"stage: {stage}",
        f"reason: {reason}",
        "",
        "--- original message ---",
        original,
    ])
    path.write_text(content, encoding="utf-8")
    return path


def invoke_logs(args: list) -> object:
    """Invoke the CLI logs command."""
    runner = CliRunner()
    return runner.invoke(cli, ["logs"] + args)


# ---------------------------------------------------------------------------
# Tests: --trash with empty trash dir
# ---------------------------------------------------------------------------


class TestTrashEmpty:
    def test_empty_dir_prints_no_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_runner.main._DEFAULT_TRASH_DIR", tmp_path / "trash")
        result = invoke_logs(["--trash"])
        assert result.exit_code == 0
        assert "No trash entries" in result.output

    def test_nonexistent_dir_prints_no_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_runner.main._DEFAULT_TRASH_DIR", tmp_path / "nonexistent")
        result = invoke_logs(["--trash"])
        assert result.exit_code == 0
        assert "No trash entries" in result.output


# ---------------------------------------------------------------------------
# Tests: --trash listing
# ---------------------------------------------------------------------------


class TestTrashListing:
    def test_lists_files_newest_first(self, tmp_path, monkeypatch):
        trash_dir = tmp_path / "trash"
        monkeypatch.setattr("claude_runner.main._DEFAULT_TRASH_DIR", trash_dir)

        # Create two files with different mtimes
        f1 = _write_trash(trash_dir, "20260316T000001Z-PARSE.log", "PARSE", "bad input")
        time.sleep(0.05)
        f2 = _write_trash(trash_dir, "20260316T000002Z-CONVERT.log", "CONVERT", "yaml error")

        result = invoke_logs(["--trash"])
        assert result.exit_code == 0
        # CONVERT (newer) should appear before PARSE (older)
        idx_convert = result.output.find("CONVERT")
        idx_parse = result.output.find("PARSE")
        assert idx_convert < idx_parse, "Newer file should appear first"

    def test_shows_stage_in_listing(self, tmp_path, monkeypatch):
        trash_dir = tmp_path / "trash"
        monkeypatch.setattr("claude_runner.main._DEFAULT_TRASH_DIR", trash_dir)
        _write_trash(trash_dir, "20260316T000001Z-LAUNCH.log", "LAUNCH", "exec failed")

        result = invoke_logs(["--trash"])
        assert result.exit_code == 0
        assert "LAUNCH" in result.output

    def test_shows_reason_preview_in_listing(self, tmp_path, monkeypatch):
        trash_dir = tmp_path / "trash"
        monkeypatch.setattr("claude_runner.main._DEFAULT_TRASH_DIR", trash_dir)
        _write_trash(trash_dir, "20260316T000001Z-PARSE.log", "PARSE", "some parse error reason")

        result = invoke_logs(["--trash"])
        assert result.exit_code == 0
        assert "some parse error reason" in result.output


# ---------------------------------------------------------------------------
# Tests: --trash --last N
# ---------------------------------------------------------------------------


class TestTrashLastN:
    def test_shows_full_content_of_most_recent(self, tmp_path, monkeypatch):
        trash_dir = tmp_path / "trash"
        monkeypatch.setattr("claude_runner.main._DEFAULT_TRASH_DIR", trash_dir)
        _write_trash(trash_dir, "20260316T000001Z-PARSE.log", "PARSE", "unique-reason-xyz", original="original raw content")

        result = invoke_logs(["--trash", "--last", "1"])
        assert result.exit_code == 0
        assert "unique-reason-xyz" in result.output
        assert "original raw content" in result.output

    def test_shows_full_content_of_last_2(self, tmp_path, monkeypatch):
        trash_dir = tmp_path / "trash"
        monkeypatch.setattr("claude_runner.main._DEFAULT_TRASH_DIR", trash_dir)
        _write_trash(trash_dir, "20260316T000001Z-A.log", "PARSE", "reason-A")
        time.sleep(0.05)
        _write_trash(trash_dir, "20260316T000002Z-B.log", "CONVERT", "reason-B")
        time.sleep(0.05)
        _write_trash(trash_dir, "20260316T000003Z-C.log", "LAUNCH", "reason-C")

        result = invoke_logs(["--trash", "--last", "2"])
        assert result.exit_code == 0
        # Should show C and B (most recent 2), not A
        assert "reason-C" in result.output
        assert "reason-B" in result.output
        assert "reason-A" not in result.output

    def test_last_1_shows_only_most_recent(self, tmp_path, monkeypatch):
        trash_dir = tmp_path / "trash"
        monkeypatch.setattr("claude_runner.main._DEFAULT_TRASH_DIR", trash_dir)
        _write_trash(trash_dir, "20260316T000001Z-OLD.log", "PARSE", "old-reason")
        time.sleep(0.05)
        _write_trash(trash_dir, "20260316T000002Z-NEW.log", "PARSE", "new-reason")

        result = invoke_logs(["--trash", "--last", "1"])
        assert result.exit_code == 0
        assert "new-reason" in result.output
        assert "old-reason" not in result.output
