"""
watchdog.py — Self-awakening monitor for claude-runner.

Watches a claude-runner process (by PID file or process scan) and restarts
it with the same project book if it dies unexpectedly.  Sends an ntfy
notification on every restart so the user knows what happened.

Usage:
    python watchdog.py projects/addendum.yaml [--max-restarts 5]

The watchdog itself is a plain blocking loop — run it in a separate terminal
or detach it with pythonw watchdog.py ... on Windows.
"""

from __future__ import annotations

import argparse
import logging
import os
import pathlib
import subprocess
import sys
import time

import requests

# ── PATH augmentation ────────────────────────────────────────────────────────
# Node.js may not be on PATH in non-interactive shells (Git Bash, task scheduler).
# Inject known Windows install locations so 'node' and 'claude' are found.
_EXTRA_PATHS = [
    r"C:\Program Files\nodejs",
    r"C:\Program Files\nodejs\node_modules\.bin",
    pathlib.Path.home() / ".local" / "bin",  # claude.EXE location
    r"C:\Users\zl7u25\AppData\Local\Programs\Git\usr\bin",  # git bash
]
for _p in _EXTRA_PATHS:
    _ps = str(_p)
    if _ps not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _ps + os.pathsep + os.environ.get("PATH", "")

# Claude Code on Windows requires git bash for its shell operations.
# Without this, it exits with code 1 and a message about git-bash.
_GIT_BASH = r"C:\Users\zl7u25\AppData\Local\Programs\Git\usr\bin\bash.exe"
if pathlib.Path(_GIT_BASH).exists():
    os.environ.setdefault("CLAUDE_CODE_GIT_BASH_PATH", _GIT_BASH)

# ── Configuration ────────────────────────────────────────────────────────────

NTFY_URL        = "https://ntfy.sh/claude-runner-honacoo"
POLL_INTERVAL_S = 15        # seconds between liveness checks
RESTART_DELAY_S = 10        # seconds to wait before restarting after a crash
DEFAULT_MAX_RESTARTS = 10   # give up after this many unplanned restarts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("watchdog")

# ── ntfy helper ──────────────────────────────────────────────────────────────

def _notify(title: str, body: str, priority: str = "default") -> None:
    try:
        requests.post(
            NTFY_URL,
            data=body.encode(),
            headers={"Title": title, "Priority": priority},
            timeout=10,
        )
    except Exception as exc:
        log.warning("ntfy send failed: %s", exc)


# ── Runner launcher ──────────────────────────────────────────────────────────

def _launch(book_path: str) -> subprocess.Popen:
    """Start claude-runner run <book_path> as a child process."""
    cmd = [sys.executable, "-m", "claude_runner.main", "run", book_path]
    log.info("Launching: %s", " ".join(cmd))
    return subprocess.Popen(cmd, cwd=pathlib.Path(__file__).parent)


# ── Main watchdog loop ────────────────────────────────────────────────────────

def run(book_path: str, max_restarts: int) -> None:
    restarts = 0
    proc = _launch(book_path)
    _notify(
        "claude-runner watchdog started",
        f"Watching: {book_path}\nPID: {proc.pid}\nMax restarts: {max_restarts}",
    )

    try:
        while True:
            time.sleep(POLL_INTERVAL_S)
            ret = proc.poll()

            if ret is None:
                # Still running — heartbeat every ~5 minutes
                elapsed = time.monotonic()
                continue

            # Process exited
            if ret == 0:
                log.info("Runner exited cleanly (exit 0). Watchdog done.")
                _notify(
                    "claude-runner finished",
                    f"Task {book_path!r} completed normally. Watchdog exiting.",
                    priority="high",
                )
                break

            # Non-zero exit — unexpected crash
            restarts += 1
            log.warning(
                "Runner exited with code %d (restart %d/%d).",
                ret, restarts, max_restarts,
            )
            _notify(
                f"claude-runner CRASHED — restarting ({restarts}/{max_restarts})",
                f"Exit code: {ret}\nBook: {book_path}\nWaiting {RESTART_DELAY_S}s…",
                priority="high",
            )

            if restarts >= max_restarts:
                log.error("Max restarts reached (%d). Giving up.", max_restarts)
                _notify(
                    "claude-runner watchdog GIVING UP",
                    f"Reached {max_restarts} restarts for {book_path!r}. Manual intervention needed.",
                    priority="urgent",
                )
                break

            time.sleep(RESTART_DELAY_S)
            proc = _launch(book_path)
            _notify(
                f"claude-runner restarted (attempt {restarts})",
                f"PID: {proc.pid} | Book: {book_path}",
            )

    except KeyboardInterrupt:
        log.info("Watchdog interrupted by user.")
        if proc.poll() is None:
            log.info("Terminating runner process %d.", proc.pid)
            proc.terminate()
        _notify("claude-runner watchdog stopped", "Interrupted by user.")


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="claude-runner watchdog")
    parser.add_argument("book", help="Path to the project book YAML")
    parser.add_argument(
        "--max-restarts",
        type=int,
        default=DEFAULT_MAX_RESTARTS,
        help=f"Maximum unplanned restarts before giving up (default: {DEFAULT_MAX_RESTARTS})",
    )
    args = parser.parse_args()
    run(args.book, args.max_restarts)
