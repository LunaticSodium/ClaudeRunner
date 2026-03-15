"""
Session state persistence for claude-runner.

Saves and restores ``TaskState`` across crashes, rate-limit interruptions, and
planned restarts.  State is written atomically (write-to-temp + rename) to
prevent a corrupt state file from blocking recovery.

State file location: ``<state_dir>/<task_name>.json``

The ``PersistenceManager`` is used by the runner loop to:
  - Checkpoint state every 30 seconds and on every significant event.
  - Detect a prior incomplete run at startup and offer resume / fresh-start.
  - Append BUG-level fault entries on behalf of ``NotificationManager``.
  - Remove the state file cleanly on successful task completion.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TaskState dataclass
# ---------------------------------------------------------------------------

@dataclass
class TaskState:
    """
    Persisted state for a running or completed task.

    Written to ``<state_dir>/<task_name>.json``.
    Checkpointed every 30 seconds and on every significant event.

    Attributes
    ----------
    task_name:
        Identifier matching the project book's task name.  Also used as the
        JSON filename stem.
    project_book_path:
        Absolute path to the source ``.yaml`` project book file.
    start_time:
        ISO 8601 UTC timestamp of when the task was first launched.
    current_phase:
        One of ``"running"``, ``"waiting"``, ``"resuming"``, ``"complete"``,
        ``"failed"``.
    rate_limit_wait_count:
        Number of rate-limit cycles encountered so far.
    last_output_timestamp:
        ISO 8601 UTC timestamp of the most recent line of output received from
        Claude Code.  ``None`` if no output has been received yet.
    last_reset_time:
        ISO 8601 UTC timestamp of the most recent rate-limit reset.  ``None``
        if no reset has occurred.
    token_estimate:
        Running estimate of tokens consumed in the current context window.
        Updated by ``context_manager``.
    checkpoint_count:
        Number of context checkpoint prompts injected so far.
    progress_log_path:
        Absolute path to the in-workspace progress log
        (``/workspace/.claude-runner/progress.log`` inside Docker, or the
        equivalent host path).  ``None`` if not yet created.
    fault_log:
        List of BUG-level fault messages accumulated during this task run.
        Populated by ``NotificationManager`` via ``PersistenceManager.append_fault``.
    last_summary:
        The most recent checkpoint summary text produced by Claude Code
        (used by the ``summarize`` resume strategy).  ``None`` if no
        checkpoint summary has been captured yet.
    """

    task_name: str
    project_book_path: str
    start_time: str                      # ISO 8601
    current_phase: str                   # running | waiting | resuming | complete | failed
    rate_limit_wait_count: int = 0
    last_output_timestamp: str | None = None
    last_reset_time: str | None = None   # ISO 8601 of last rate-limit reset
    token_estimate: int = 0
    checkpoint_count: int = 0
    progress_log_path: str | None = None
    fault_log: list[str] = field(default_factory=list)
    last_summary: str | None = None      # For the summarize resume strategy

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskState":
        """
        Construct a ``TaskState`` from a plain dict (e.g. loaded from JSON).

        Unknown keys in *data* are silently ignored, which allows forward
        compatibility when new fields are added to the dataclass.
        """
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


# ---------------------------------------------------------------------------
# PersistenceManager
# ---------------------------------------------------------------------------

class PersistenceManager:
    """
    Manages ``TaskState`` JSON checkpoint files.

    Parameters
    ----------
    state_dir:
        Directory where state files are stored.
        Default convention: ``~/.claude-runner/state/``.
        Created automatically on first ``save()`` if it does not exist.
    task_name:
        Identifier for the task.  The state file is named
        ``<task_name>.json`` inside *state_dir*.
    """

    def __init__(self, state_dir: Path, task_name: str) -> None:
        self._state_dir = Path(state_dir)
        self._task_name = task_name
        self._state_path = self._state_dir / f"{task_name}.json"

    # ------------------------------------------------------------------
    # Core CRUD operations
    # ------------------------------------------------------------------

    def save(self, state: TaskState) -> None:
        """
        Write *state* to disk atomically.

        The write strategy is:
          1. Serialise state to JSON in memory.
          2. Write to a ``.tmp`` file in the same directory.
          3. Atomically rename the ``.tmp`` file to the target path.

        This ensures that a crash mid-write never produces a truncated or
        partially-written state file.

        Parameters
        ----------
        state:
            The ``TaskState`` instance to persist.

        Raises
        ------
        OSError
            If the directory cannot be created or the file cannot be written.
        """
        self._state_dir.mkdir(parents=True, exist_ok=True)

        payload = json.dumps(state.to_dict(), indent=2, ensure_ascii=False)

        # Write to a sibling temp file then rename atomically.
        fd, tmp_path_str = tempfile.mkstemp(
            dir=self._state_dir, prefix=f".{self._task_name}_", suffix=".tmp"
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            # On Windows, os.replace handles cross-device rename and is atomic
            # at the filesystem level within the same volume.
            os.replace(tmp_path, self._state_path)
        except Exception:
            # Clean up the temp file if something went wrong before the rename.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        logger.debug("State checkpointed to %s", self._state_path)

    def load(self) -> TaskState | None:
        """
        Load and return the persisted ``TaskState`` for this task.

        Returns ``None`` if no state file exists (fresh run).

        Raises
        ------
        ValueError
            If the state file exists but contains invalid JSON or is missing
            required fields.
        """
        if not self._state_path.exists():
            return None

        try:
            raw = self._state_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise OSError(
                f"Could not read state file {self._state_path}: {exc}"
            ) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"State file {self._state_path} contains invalid JSON: {exc}"
            ) from exc

        try:
            state = TaskState.from_dict(data)
        except TypeError as exc:
            raise ValueError(
                f"State file {self._state_path} is missing required fields: {exc}"
            ) from exc

        logger.debug("State loaded from %s (phase=%s)", self._state_path, state.current_phase)
        return state

    def delete(self) -> None:
        """
        Delete the state file on clean task completion.

        A missing state file is treated as a no-op (idempotent).
        """
        try:
            self._state_path.unlink(missing_ok=True)
            logger.debug("State file deleted: %s", self._state_path)
        except OSError as exc:
            logger.warning(
                "Could not delete state file %s: %s", self._state_path, exc
            )

    # ------------------------------------------------------------------
    # Fault log
    # ------------------------------------------------------------------

    def append_fault(self, message: str) -> None:
        """
        Load the current state, append *message* to ``fault_log``, and save.

        This is the callback passed to ``NotificationManager`` as ``on_fault``.
        It is called whenever the email guard fires (BUG-level event).

        If no state file exists yet (e.g. fault occurs before first checkpoint),
        a warning is logged and the fault is not persisted to disk — callers
        should ensure state is saved at least once before faults are possible.

        Parameters
        ----------
        message:
            BUG-level fault description string.
        """
        state = self.load()
        if state is None:
            logger.warning(
                "append_fault called but no state file exists for task %r. "
                "Fault message: %s",
                self._task_name,
                message,
            )
            return

        state.fault_log.append(message)
        logger.error("Fault recorded in state file: %s", message)
        self.save(state)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def exists(self) -> bool:
        """Return ``True`` if a state file exists for this task."""
        return self._state_path.exists()

    def get_state_path(self) -> Path:
        """Return the absolute ``Path`` of the state file."""
        return self._state_path
