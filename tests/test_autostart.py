"""
tests/test_autostart.py

Unit tests for claude_runner.autostart — Windows Task Scheduler integration.

All subprocess.run calls are mocked; no real schtasks invocations occur.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, call
import subprocess

import pytest

from claude_runner.autostart import register, unregister, is_registered, TASK_NAME


def _make_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


class TestRegister:
    def test_calls_schtasks_create(self):
        with patch("subprocess.run", return_value=_make_result(0)) as mock_run:
            register("C:\\runner\\claude-runner.exe")
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert "schtasks" in cmd
        assert "/create" in cmd
        assert "/tn" in cmd
        assert TASK_NAME in cmd
        assert "/tr" in cmd
        assert "C:\\runner\\claude-runner.exe" in cmd
        assert "/sc" in cmd
        assert "onlogon" in cmd
        assert "/rl" in cmd
        assert "limited" in cmd
        assert "/f" in cmd

    def test_raises_on_nonzero_exit(self):
        with patch("subprocess.run", return_value=_make_result(1, stderr="Access denied")):
            with pytest.raises(RuntimeError, match="schtasks /create failed"):
                register("C:\\runner\\claude-runner.exe")

    def test_success_does_not_raise(self):
        with patch("subprocess.run", return_value=_make_result(0)):
            register("C:\\runner\\claude-runner.exe")  # must not raise

    def test_no_shell_true(self):
        """Security: never use shell=True."""
        with patch("subprocess.run", return_value=_make_result(0)) as mock_run:
            register("C:\\runner\\claude-runner.exe")
        _, kwargs = mock_run.call_args
        assert kwargs.get("shell") is not True


class TestUnregister:
    def test_calls_schtasks_delete(self):
        with patch("subprocess.run", return_value=_make_result(0)) as mock_run:
            unregister()
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert "schtasks" in cmd
        assert "/delete" in cmd
        assert TASK_NAME in cmd
        assert "/f" in cmd

    def test_silent_when_task_not_found_returncode_1(self):
        """Returncode 1 with 'cannot find' in stderr — should not raise."""
        with patch(
            "subprocess.run",
            return_value=_make_result(1, stderr="ERROR: The system cannot find the file specified."),
        ):
            unregister()  # must not raise

    def test_silent_when_not_found_text(self):
        """Any 'not found' message — should not raise."""
        with patch(
            "subprocess.run",
            return_value=_make_result(1, stderr="ERROR: Task not found."),
        ):
            unregister()  # must not raise

    def test_no_raise_on_success(self):
        with patch("subprocess.run", return_value=_make_result(0)):
            unregister()  # must not raise

    def test_no_shell_true(self):
        with patch("subprocess.run", return_value=_make_result(0)) as mock_run:
            unregister()
        _, kwargs = mock_run.call_args
        assert kwargs.get("shell") is not True


class TestIsRegistered:
    def test_returns_true_when_schtasks_exits_zero(self):
        with patch("subprocess.run", return_value=_make_result(0)):
            assert is_registered() is True

    def test_returns_false_when_schtasks_exits_nonzero(self):
        with patch("subprocess.run", return_value=_make_result(1, stderr="not found")):
            assert is_registered() is False

    def test_returns_false_on_exception(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("schtasks not found")):
            assert is_registered() is False

    def test_calls_schtasks_query(self):
        with patch("subprocess.run", return_value=_make_result(0)) as mock_run:
            is_registered()
        args, _ = mock_run.call_args
        cmd = args[0]
        assert "/query" in cmd
        assert TASK_NAME in cmd

    def test_no_shell_true(self):
        with patch("subprocess.run", return_value=_make_result(0)) as mock_run:
            is_registered()
        _, kwargs = mock_run.call_args
        assert kwargs.get("shell") is not True
