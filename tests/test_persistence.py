"""
tests/test_persistence.py — Unit tests for claude_runner/persistence.py

Tests cover PersistenceManager (save/load/delete/append_fault) and
TaskState (to_dict/from_dict roundtrip).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_runner.persistence import PersistenceManager, TaskState


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------


def _make_state(**overrides) -> TaskState:
    """Return a minimal TaskState, with optional field overrides."""
    defaults = dict(
        task_name="test-task",
        project_book_path="/projects/test.yaml",
        start_time="2026-03-14T10:00:00Z",
        current_phase="running",
    )
    defaults.update(overrides)
    return TaskState(**defaults)


def _make_pm(tmp_path: Path, task_name: str = "test-task") -> PersistenceManager:
    state_dir = tmp_path / "state"
    return PersistenceManager(state_dir=state_dir, task_name=task_name)


# ---------------------------------------------------------------------------
# TaskState.to_dict() / from_dict() roundtrip
# ---------------------------------------------------------------------------


class TestTaskStateSerialisation:
    def test_to_dict_contains_all_fields(self):
        state = _make_state()
        d = state.to_dict()
        assert d["task_name"] == "test-task"
        assert d["project_book_path"] == "/projects/test.yaml"
        assert d["start_time"] == "2026-03-14T10:00:00Z"
        assert d["current_phase"] == "running"
        assert d["rate_limit_wait_count"] == 0
        assert d["fault_log"] == []
        assert d["token_estimate"] == 0
        assert d["checkpoint_count"] == 0

    def test_from_dict_roundtrip(self):
        state = _make_state(
            rate_limit_wait_count=3,
            token_estimate=42000,
            checkpoint_count=2,
            current_phase="waiting",
            fault_log=["[BUG] something bad happened"],
            last_summary="We completed steps 1 and 2.",
        )
        d = state.to_dict()
        restored = TaskState.from_dict(d)
        assert restored.task_name == state.task_name
        assert restored.rate_limit_wait_count == 3
        assert restored.token_estimate == 42000
        assert restored.checkpoint_count == 2
        assert restored.current_phase == "waiting"
        assert restored.fault_log == ["[BUG] something bad happened"]
        assert restored.last_summary == "We completed steps 1 and 2."

    def test_from_dict_ignores_unknown_keys(self):
        """from_dict silently ignores keys not in the dataclass (forward compat)."""
        d = {
            "task_name": "t",
            "project_book_path": "/p.yaml",
            "start_time": "2026-01-01T00:00:00Z",
            "current_phase": "running",
            "future_field_not_yet_defined": "some value",
        }
        state = TaskState.from_dict(d)
        assert state.task_name == "t"

    def test_from_dict_missing_required_raises(self):
        """from_dict raises TypeError when required fields are absent."""
        with pytest.raises(TypeError):
            TaskState.from_dict({"task_name": "t"})  # missing several required args

    def test_to_dict_is_json_serialisable(self):
        """The dict produced by to_dict() must be JSON serialisable."""
        state = _make_state(last_summary="Some summary", fault_log=["bug1"])
        d = state.to_dict()
        serialised = json.dumps(d)  # should not raise
        assert "test-task" in serialised


# ---------------------------------------------------------------------------
# PersistenceManager.save()
# ---------------------------------------------------------------------------


class TestPersistenceManagerSave:
    def test_save_creates_state_file(self, tmp_path):
        pm = _make_pm(tmp_path)
        state = _make_state()
        pm.save(state)
        assert pm.get_state_path().exists()

    def test_save_creates_state_dir_if_missing(self, tmp_path):
        """save() creates the state directory automatically."""
        state_dir = tmp_path / "new" / "nested" / "state"
        pm = PersistenceManager(state_dir=state_dir, task_name="t")
        pm.save(_make_state(task_name="t"))
        assert state_dir.exists()

    def test_save_writes_valid_json(self, tmp_path):
        pm = _make_pm(tmp_path)
        pm.save(_make_state())
        raw = pm.get_state_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        assert data["task_name"] == "test-task"

    def test_save_is_atomic_via_temp_file(self, tmp_path):
        """
        Verify atomic-write pattern: a .tmp file must not be left behind
        after a successful save().
        """
        pm = _make_pm(tmp_path)
        pm.save(_make_state())
        state_dir = tmp_path / "state"
        tmp_files = list(state_dir.glob("*.tmp"))
        assert tmp_files == [], f"Temp files found after save: {tmp_files}"

    def test_save_overwrites_previous_state(self, tmp_path):
        """A second save() overwrites the first without error."""
        pm = _make_pm(tmp_path)
        pm.save(_make_state(current_phase="running"))
        pm.save(_make_state(current_phase="complete"))
        state = pm.load()
        assert state.current_phase == "complete"


# ---------------------------------------------------------------------------
# PersistenceManager.load()
# ---------------------------------------------------------------------------


class TestPersistenceManagerLoad:
    def test_load_returns_none_for_missing_file(self, tmp_path):
        pm = _make_pm(tmp_path)
        assert pm.load() is None

    def test_load_restores_state_after_save(self, tmp_path):
        pm = _make_pm(tmp_path)
        original = _make_state(
            rate_limit_wait_count=5,
            current_phase="waiting",
            token_estimate=75000,
            fault_log=["[BUG] guard triggered"],
        )
        pm.save(original)
        restored = pm.load()
        assert restored is not None
        assert restored.task_name == original.task_name
        assert restored.rate_limit_wait_count == 5
        assert restored.current_phase == "waiting"
        assert restored.token_estimate == 75000
        assert restored.fault_log == ["[BUG] guard triggered"]

    def test_load_raises_on_invalid_json(self, tmp_path):
        """load() raises ValueError if the state file contains invalid JSON."""
        pm = _make_pm(tmp_path)
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        pm.get_state_path().write_text("{ not valid json !!!", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid JSON"):
            pm.load()

    def test_load_raises_on_missing_required_fields(self, tmp_path):
        """load() raises ValueError if JSON is valid but missing required fields."""
        pm = _make_pm(tmp_path)
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        pm.get_state_path().write_text(
            json.dumps({"task_name": "test-task"}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="missing required fields"):
            pm.load()

    def test_exists_returns_false_before_save(self, tmp_path):
        pm = _make_pm(tmp_path)
        assert pm.exists() is False

    def test_exists_returns_true_after_save(self, tmp_path):
        pm = _make_pm(tmp_path)
        pm.save(_make_state())
        assert pm.exists() is True


# ---------------------------------------------------------------------------
# PersistenceManager.delete()
# ---------------------------------------------------------------------------


class TestPersistenceManagerDelete:
    def test_delete_removes_state_file(self, tmp_path):
        pm = _make_pm(tmp_path)
        pm.save(_make_state())
        assert pm.exists()
        pm.delete()
        assert not pm.exists()

    def test_delete_is_idempotent(self, tmp_path):
        """Deleting a non-existent state file is a no-op — no exception raised."""
        pm = _make_pm(tmp_path)
        pm.delete()  # should not raise
        pm.delete()  # second call also safe

    def test_delete_does_not_affect_other_task_files(self, tmp_path):
        """Deleting one task's state file leaves others intact."""
        pm_a = _make_pm(tmp_path, task_name="task-a")
        pm_b = _make_pm(tmp_path, task_name="task-b")
        state_dir = tmp_path / "state"

        pm_a.save(_make_state(task_name="task-a"))
        pm_b.save(_make_state(task_name="task-b"))
        pm_a.delete()

        assert not pm_a.exists()
        assert pm_b.exists()


# ---------------------------------------------------------------------------
# PersistenceManager.append_fault()
# ---------------------------------------------------------------------------


class TestAppendFault:
    def test_append_fault_adds_to_fault_log(self, tmp_path):
        pm = _make_pm(tmp_path)
        pm.save(_make_state())
        pm.append_fault("[BUG] email guard triggered")
        state = pm.load()
        assert "[BUG] email guard triggered" in state.fault_log

    def test_append_fault_persists_to_disk(self, tmp_path):
        """After append_fault, a fresh load should reflect the added fault."""
        pm = _make_pm(tmp_path)
        pm.save(_make_state())
        pm.append_fault("fault 1")
        pm.append_fault("fault 2")
        state = pm.load()
        assert len(state.fault_log) == 2
        assert "fault 1" in state.fault_log
        assert "fault 2" in state.fault_log

    def test_append_fault_no_state_file_logs_warning(self, tmp_path, caplog):
        """append_fault on a missing state file logs a warning and does not crash."""
        import logging
        pm = _make_pm(tmp_path)
        with caplog.at_level(logging.WARNING, logger="claude_runner.persistence"):
            pm.append_fault("some fault")  # no state file exists
        assert any("no state file" in r.message.lower() for r in caplog.records)

    def test_get_state_path_returns_correct_path(self, tmp_path):
        pm = _make_pm(tmp_path, task_name="my-task")
        expected = tmp_path / "state" / "my-task.json"
        assert pm.get_state_path() == expected
