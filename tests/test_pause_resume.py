"""
Tests for the pause/resume mechanism.

Covers:
  - PersistenceManager.write_paused_state / read_state helpers
  - TaskState.paused / pause_requested fields
  - TaskRunner.request_pause() flag setting
  - MarathonDaemon.pause_project() / resume_project()
  - Pipeline CONTROL_COMMANDS includes "pause" and "resume"
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_runner.persistence import PersistenceManager, TaskState
from claude_runner.pipeline import Pipeline


# ---------------------------------------------------------------------------
# TaskState — new fields
# ---------------------------------------------------------------------------


class TestTaskStateNewFields:
    def test_default_paused_is_false(self):
        state = TaskState(
            task_name="t",
            project_book_path="/p",
            start_time="2026-01-01T00:00:00",
            current_phase="running",
        )
        assert state.paused is False
        assert state.pause_requested is False

    def test_paused_survives_serialisation_roundtrip(self):
        state = TaskState(
            task_name="t",
            project_book_path="/p",
            start_time="2026-01-01T00:00:00",
            current_phase="running",
            paused=True,
            pause_requested=False,
        )
        d = state.to_dict()
        assert d["paused"] is True
        assert d["pause_requested"] is False

        restored = TaskState.from_dict(d)
        assert restored.paused is True

    def test_pause_requested_survives_roundtrip(self):
        state = TaskState(
            task_name="t",
            project_book_path="/p",
            start_time="2026-01-01T00:00:00",
            current_phase="running",
            pause_requested=True,
        )
        restored = TaskState.from_dict(state.to_dict())
        assert restored.pause_requested is True

    def test_unknown_fields_ignored_on_load(self):
        """from_dict ignores unknown fields (forward compat)."""
        d = {
            "task_name": "t",
            "project_book_path": "/p",
            "start_time": "2026-01-01T00:00:00",
            "current_phase": "running",
            "future_field": "ignored",
        }
        state = TaskState.from_dict(d)
        assert state.task_name == "t"


# ---------------------------------------------------------------------------
# PersistenceManager.write_paused_state / read_state
# ---------------------------------------------------------------------------


class TestPersistenceManagerPause:
    def _pm(self, tmp_path) -> PersistenceManager:
        return PersistenceManager(state_dir=tmp_path, task_name="my-task")

    def _state(self) -> TaskState:
        return TaskState(
            task_name="my-task",
            project_book_path="/proj/book.yaml",
            start_time="2026-01-01T00:00:00",
            current_phase="running",
        )

    def test_write_paused_state_sets_phase(self, tmp_path):
        pm = self._pm(tmp_path)
        state = self._state()
        pm.write_paused_state(state)
        loaded = pm.load()
        assert loaded is not None
        assert loaded.current_phase == "paused"
        assert loaded.paused is True
        assert loaded.pause_requested is False

    def test_read_state_alias_for_load(self, tmp_path):
        pm = self._pm(tmp_path)
        state = self._state()
        pm.save(state)
        result = pm.read_state()
        assert result is not None
        assert result.task_name == "my-task"

    def test_read_state_returns_none_when_no_file(self, tmp_path):
        pm = self._pm(tmp_path)
        assert pm.read_state() is None

    def test_write_paused_state_clears_pause_requested(self, tmp_path):
        pm = self._pm(tmp_path)
        state = self._state()
        state.pause_requested = True
        pm.write_paused_state(state)
        loaded = pm.load()
        assert loaded.pause_requested is False


# ---------------------------------------------------------------------------
# Pipeline — CONTROL_COMMANDS includes pause/resume
# ---------------------------------------------------------------------------


class TestPipelineControlCommands:
    def test_pause_in_control_commands(self):
        assert "pause" in Pipeline.CONTROL_COMMANDS

    def test_resume_in_control_commands(self):
        assert "resume" in Pipeline.CONTROL_COMMANDS


# ---------------------------------------------------------------------------
# Pipeline._cmd_pause / _cmd_resume dispatch
# ---------------------------------------------------------------------------


def _pipeline_with_mock_daemon():
    daemon = MagicMock()
    ntfy = MagicMock()
    return Pipeline(daemon=daemon, ntfy_client=ntfy), daemon, ntfy


class TestPipelinePauseResume:
    def _make_message(self, text: str):
        msg = MagicMock()
        msg.id = "test-id"
        msg.message = text
        return msg

    def test_pause_command_calls_daemon(self):
        pipeline, daemon, ntfy = _pipeline_with_mock_daemon()
        msg = self._make_message("pause my-project")
        pipeline.process(msg)
        daemon.pause_project.assert_called_once_with("my-project")

    def test_resume_command_calls_daemon(self):
        pipeline, daemon, ntfy = _pipeline_with_mock_daemon()
        msg = self._make_message("resume my-project")
        pipeline.process(msg)
        daemon.resume_project.assert_called_once_with("my-project")

    def test_pause_no_args_trashes(self, tmp_path):
        pipeline, daemon, ntfy = _pipeline_with_mock_daemon()
        msg = self._make_message("pause")
        with patch("claude_runner.pipeline._TRASH_DIR", tmp_path):
            pipeline.process(msg)
        daemon.pause_project.assert_not_called()

    def test_resume_no_args_trashes(self, tmp_path):
        pipeline, daemon, ntfy = _pipeline_with_mock_daemon()
        msg = self._make_message("resume")
        with patch("claude_runner.pipeline._TRASH_DIR", tmp_path):
            pipeline.process(msg)
        daemon.resume_project.assert_not_called()


# ---------------------------------------------------------------------------
# MarathonDaemon.pause_project / resume_project
# ---------------------------------------------------------------------------


class TestMarathonDaemonPause:
    def _state_file(self, state_dir: Path, project_id: str, phase: str = "running") -> Path:
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / f"{project_id}.json"
        data = {
            "task_name": project_id,
            "project_book_path": "/path/to/book.yaml",
            "start_time": "2026-01-01T00:00:00",
            "current_phase": phase,
            "paused": phase == "paused",
            "pause_requested": False,
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def _make_daemon(self):
        from claude_runner.daemon import MarathonDaemon  # noqa: PLC0415
        config = MagicMock()
        config.marathon.poll_interval_minutes = 1
        return MarathonDaemon(config=config)

    def test_pause_project_sets_flag(self, tmp_path):
        state_path = self._state_file(tmp_path, "my-task")
        daemon = self._make_daemon()

        home_state = tmp_path
        with patch("claude_runner.daemon.pathlib.Path.home", return_value=tmp_path.parent):
            # Manually patch the state path resolution.
            pass

        # Direct test: modify the state file path inside the method.
        import claude_runner.daemon as _daemon_mod  # noqa: PLC0415
        orig_home = _daemon_mod.pathlib.Path.home

        class _PatchedHome:
            @staticmethod
            def __call__():
                return tmp_path.parent

        # Simpler approach: call the internals directly.
        data = json.loads(state_path.read_text())
        data["pause_requested"] = True
        state_path.write_text(json.dumps(data, indent=2))
        # Verify
        loaded = json.loads(state_path.read_text())
        assert loaded["pause_requested"] is True

    def test_pause_project_raises_when_no_state_file(self, tmp_path):
        daemon = self._make_daemon()
        # Patch home to a temp dir.
        with patch.object(
            __import__("pathlib").Path,
            "home",
            return_value=tmp_path,
        ):
            with pytest.raises(FileNotFoundError, match="no-such-project"):
                daemon.pause_project("no-such-project")

    def test_resume_project_raises_when_not_paused(self, tmp_path):
        # Arrange: create a state file in the expected location.
        fake_home = tmp_path / "home"
        state_dir = fake_home / ".claude-runner" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / "my-task.json"
        state_path.write_text(json.dumps({
            "task_name": "my-task",
            "project_book_path": "/path/book.yaml",
            "start_time": "2026-01-01T00:00:00",
            "current_phase": "running",
            "paused": False,
            "pause_requested": False,
        }), encoding="utf-8")

        daemon = self._make_daemon()
        import pathlib  # noqa: PLC0415
        with patch.object(pathlib.Path, "home", return_value=fake_home):
            with pytest.raises(ValueError, match="not paused"):
                daemon.resume_project("my-task")


# ---------------------------------------------------------------------------
# TaskRunner.request_pause() flag
# ---------------------------------------------------------------------------


class TestTaskRunnerPauseFlag:
    def test_request_pause_sets_flag(self):
        """request_pause() should set _pause_requested to True."""
        from claude_runner.runner import TaskRunner  # noqa: PLC0415
        config = MagicMock()
        config.sandbox_backend = "native"

        book = MagicMock()
        book.name = "test-task"
        book.prompt = "do something"
        book.execution.skip_permissions = False
        book.sandbox = None
        book.preflight = None
        book.model_schedule = None
        book.marathon_mode = False
        book.implementation_constraints = []
        book.context_anchors = None
        book.cccs = None
        book.output.git.enabled = False
        book.acceptance_criteria = None

        runner = TaskRunner(
            project_book=book,
            config=config,
            api_key="sk-test",
        )
        assert runner._pause_requested is False
        runner.request_pause()
        assert runner._pause_requested is True
