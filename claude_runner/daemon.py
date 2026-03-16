"""
claude_runner/daemon.py

Persistent marathon daemon that polls the ntfy cmd channel and dispatches tasks.

Launched when `claude-runner` is invoked with no arguments in marathon mode,
or explicitly via `claude-runner marathon`.
"""
from __future__ import annotations

import logging
import os
import pathlib
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .config import Config as GlobalConfig

logger = logging.getLogger(__name__)

_DEFAULT_HOME = pathlib.Path.home() / ".claude-runner"
_PID_FILE = _DEFAULT_HOME / "daemon.pid"


class MarathonDaemon:
    """
    Persistent daemon that polls the ntfy cmd channel and dispatches tasks.

    Launched when ``claude-runner`` is invoked with no arguments in marathon
    mode, or explicitly via ``claude-runner marathon``.
    """

    def __init__(self, config: "GlobalConfig") -> None:
        self.config = config
        self.start_time: datetime = datetime.now(timezone.utc)
        self.active_task: Optional[str] = None  # task name string
        self._shutdown = threading.Event()
        self._ntfy_client: Optional[object] = None  # NtfyClient, set lazily

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main loop.  Blocks until stop() is called.

        Polls every config.marathon.poll_interval_minutes minutes.
        Writes PID file on start; removes on stop.
        """
        _write_pid_file()
        self._notify_out("Marathon daemon started.")
        logger.info("Marathon daemon started. PID=%d", os.getpid())
        try:
            while not self._shutdown.is_set():
                try:
                    self._poll_once()
                except Exception as exc:  # noqa: BLE001
                    logger.error("Poll error: %s", exc)
                self._shutdown.wait(
                    timeout=self.config.marathon.poll_interval_minutes * 60
                )
        finally:
            _remove_pid_file()
            self._notify_out("Marathon daemon stopped.")
            logger.info("Marathon daemon stopped.")

    def stop(self) -> None:
        """Signal the daemon to exit after the current poll completes."""
        self._shutdown.set()

    def status(self) -> dict:
        """Return daemon uptime, active task name, and shutdown state."""
        now = datetime.now(timezone.utc)
        uptime_seconds = (now - self.start_time).total_seconds()
        return {
            "pid": os.getpid(),
            "start_time": self.start_time.isoformat(),
            "uptime_seconds": uptime_seconds,
            "active_task": self.active_task,
            "shutdown_requested": self._shutdown.is_set(),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _poll_once(self) -> None:
        """Fetch new messages from cmd channel and dispatch them."""
        client = self._get_ntfy_client()
        if client is None:
            logger.debug("ntfy client not available — skipping poll.")
            return

        try:
            # Import Pipeline lazily to avoid circular imports.
            from .pipeline import Pipeline  # noqa: PLC0415

            # Load persisted last_message_id.
            state = _load_ntfy_state()
            since_id: Optional[str] = state.get("last_message_id")

            messages = client.poll("cmd", since_id)
            for msg in messages:
                logger.info("Processing inbound message id=%s", msg.id)
                pipeline = Pipeline(daemon=self, ntfy_client=client)
                pipeline.process(msg)
        except Exception as exc:  # noqa: BLE001
            logger.error("_poll_once error: %s", exc)

    def _notify_out(self, message: str) -> None:
        """Publish a message to the out ntfy channel."""
        client = self._get_ntfy_client()
        if client is None:
            logger.debug("ntfy client not available — cannot publish: %s", message)
            return
        try:
            client.publish("out", message, title="claude-runner daemon")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to publish to out channel: %s", exc)

    def _get_ntfy_client(self) -> Optional[object]:
        """Lazily initialise and return NtfyClient."""
        if self._ntfy_client is None:
            try:
                from .ntfy_client import NtfyClient  # noqa: PLC0415
                self._ntfy_client = NtfyClient()
            except Exception as exc:  # noqa: BLE001
                logger.warning("NtfyClient init failed: %s — daemon continues without ntfy.", exc)
        return self._ntfy_client


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------


def _write_pid_file() -> None:
    """Write current PID to ~/.claude-runner/daemon.pid."""
    try:
        _DEFAULT_HOME.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        logger.debug("PID file written: %s", _PID_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write PID file: %s", exc)


def _remove_pid_file() -> None:
    """Remove ~/.claude-runner/daemon.pid if present."""
    try:
        if _PID_FILE.exists():
            _PID_FILE.unlink()
            logger.debug("PID file removed: %s", _PID_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to remove PID file: %s", exc)


def read_daemon_pid() -> Optional[int]:
    """Read the daemon PID from the PID file.  Returns None if not found."""
    try:
        if _PID_FILE.exists():
            return int(_PID_FILE.read_text(encoding="utf-8").strip())
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read PID file: %s", exc)
    return None


def _load_ntfy_state() -> dict:
    """Load ~/.claude-runner/ntfy_state.json."""
    state_file = _DEFAULT_HOME / "ntfy_state.json"
    try:
        if state_file.exists():
            import json  # noqa: PLC0415
            return json.loads(state_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read ntfy_state.json: %s", exc)
    return {}
