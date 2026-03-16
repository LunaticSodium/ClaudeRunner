"""
claude_runner/inbox.py

Buffer-mediated message injection for claude-runner (Feature A1).

Allows external messages (delivered via ntfy cmd channel) to be accumulated
in a file and injected into a running Claude Code session at natural pause
points, without interrupting active tool use.

Buffer file: ~/.claude-runner/inbox/pending.md  (append-only during accumulation)

Module-level API
----------------
append_message(text)      — append text with a timestamp header; sets flag
drain(process, timeout_s) — inject pending messages into *process* stdin;
                            wait for acknowledgement; clear buffer and flag
has_pending_messages      — bool (read-only via is_pending())
"""
from __future__ import annotations

import logging
import pathlib
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DEFAULT_HOME = pathlib.Path.home() / ".claude-runner"
_PENDING_FILE = _DEFAULT_HOME / "inbox" / "pending.md"

# Module-level pending flag.
has_pending_messages: bool = False

# Prompt injected into Claude Code when draining the inbox.
_INJECT_PROMPT = (
    "Please read ~/.claude-runner/inbox/pending.md and process its contents, "
    "then continue your current task."
)

# Default acknowledgement wait (seconds).
_DEFAULT_ACK_TIMEOUT_S: float = 60.0


def append_message(text: str) -> None:
    """
    Append *text* to the pending inbox file with a timestamp header.

    Sets :data:`has_pending_messages` to ``True``.
    Does NOT interrupt Claude Code.

    Parameters
    ----------
    text:
        The message body to accumulate.
    """
    global has_pending_messages

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = f"\n---\n**Received {ts}**\n\n"
    entry = header + text.strip() + "\n"

    try:
        _PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _PENDING_FILE.open("a", encoding="utf-8") as fh:
            fh.write(entry)
        logger.info("inbox.append_message: wrote %d chars to %s", len(entry), _PENDING_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("inbox.append_message: failed to write to %s: %s", _PENDING_FILE, exc)
        return

    has_pending_messages = True


def drain(process, timeout_s: float = _DEFAULT_ACK_TIMEOUT_S) -> None:
    """
    Inject pending messages into *process* and wait for acknowledgement.

    Called at natural pause points (after rate-limit resume, after context
    checkpoint, and on the silence-timeout probe path).

    If :data:`has_pending_messages` is ``False``, this is a no-op.

    Parameters
    ----------
    process:
        The running Claude Code process object.  Must expose a
        ``send(text)`` or ``write(text)`` method that writes to stdin.
    timeout_s:
        Maximum seconds to wait for acknowledgement (any output from
        the process).  Defaults to 60 s.
    """
    global has_pending_messages

    if not has_pending_messages:
        return

    logger.info("inbox.drain: pending messages found — injecting prompt.")

    # Determine how to send text to the process.
    send_fn = getattr(process, "send", None) or getattr(process, "write", None)
    if send_fn is None:
        logger.warning(
            "inbox.drain: process has no send/write method — skipping injection."
        )
        return

    try:
        send_fn(_INJECT_PROMPT + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("inbox.drain: failed to inject prompt: %s", exc)
        return

    # Wait for acknowledgement: any output within timeout_s.
    acked = _wait_for_output(process, timeout_s)
    if acked:
        logger.info("inbox.drain: Claude acknowledged inbox injection.")
    else:
        logger.warning(
            "inbox.drain: no acknowledgement within %.0f s — continuing anyway.", timeout_s
        )

    # Truncate pending.md to zero bytes.
    try:
        _PENDING_FILE.write_text("", encoding="utf-8")
        logger.info("inbox.drain: pending.md truncated.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("inbox.drain: failed to truncate pending.md: %s", exc)

    has_pending_messages = False


def is_pending() -> bool:
    """Return True if there are pending messages waiting to be injected."""
    return has_pending_messages


def reset() -> None:
    """
    Reset module state.  Used by tests to get a clean slate between runs.
    """
    global has_pending_messages
    has_pending_messages = False
    try:
        if _PENDING_FILE.exists():
            _PENDING_FILE.write_text("", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _wait_for_output(process, timeout_s: float) -> bool:
    """
    Poll *process* for any new output within *timeout_s* seconds.

    Returns True if output was received, False on timeout.

    Uses duck-typing: checks for ``output_available()``, ``has_output()``,
    or ``_last_output_time`` attribute on the process object.  Falls back
    to a simple sleep-based heuristic.
    """
    deadline = time.monotonic() + timeout_s
    # Duck-type: try output_available() or has_output() poll methods.
    poll_fn = getattr(process, "output_available", None) or getattr(process, "has_output", None)
    if poll_fn is not None:
        while time.monotonic() < deadline:
            if poll_fn():
                return True
            time.sleep(0.25)
        return False

    # Fallback: check _last_output_time attribute (set by TaskRunner).
    last_t_attr = "_last_output_time"
    if hasattr(process, last_t_attr):
        initial = getattr(process, last_t_attr)
        while time.monotonic() < deadline:
            current = getattr(process, last_t_attr)
            if current != initial:
                return True
            time.sleep(0.25)
        return False

    # No polling available — just sleep a bit and assume acknowledged.
    sleep_s = min(2.0, timeout_s)
    time.sleep(sleep_s)
    return True
