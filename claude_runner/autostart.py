"""
claude_runner/autostart.py

Windows Task Scheduler integration for marathon mode autostart.

Registers/unregisters claude-runner as a startup task using schtasks.
All operations are idempotent and fail-safe (errors logged, not raised,
for query and unregister operations).
"""
from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

TASK_NAME = "claude-runner-marathon"


def register(exe_path: str) -> None:
    """Register claude-runner as a Windows startup task (current user, limited).

    Uses schtasks to create an OnLogon trigger for the current user.
    The task runs with limited (non-elevated) privileges.

    Parameters
    ----------
    exe_path:
        Full path to the claude-runner executable or Python script to launch.

    Raises
    ------
    RuntimeError
        If schtasks exits with a non-zero return code.
    """
    cmd = [
        "schtasks",
        "/create",
        "/tn", TASK_NAME,
        "/tr", exe_path,
        "/sc", "onlogon",
        "/rl", "limited",
        "/f",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"schtasks /create failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    logger.info("Registered startup task %r → %s", TASK_NAME, exe_path)


def unregister() -> None:
    """Remove the claude-runner startup task.

    Silent if the task does not exist. Logs a warning on unexpected errors.
    """
    cmd = [
        "schtasks",
        "/delete",
        "/tn", TASK_NAME,
        "/f",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        logger.info("Unregistered startup task %r.", TASK_NAME)
        return

    stderr_lower = result.stderr.lower()
    # schtasks exits 1 with "ERROR: The system cannot find the file specified."
    # or similar "not found" messages when the task doesn't exist — that's OK.
    if "cannot find" in stderr_lower or "not found" in stderr_lower or result.returncode == 1:
        logger.debug("Startup task %r was not registered (nothing to remove).", TASK_NAME)
        return

    logger.warning(
        "schtasks /delete exited %d: %s", result.returncode, result.stderr.strip()
    )


def is_registered() -> bool:
    """Return True if the marathon startup task is currently registered.

    Returns False on any error (schtasks not found, permission denied, etc.).
    """
    cmd = [
        "schtasks",
        "/query",
        "/tn", TASK_NAME,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception as exc:  # noqa: BLE001
        logger.debug("is_registered() probe failed: %s", exc)
        return False
