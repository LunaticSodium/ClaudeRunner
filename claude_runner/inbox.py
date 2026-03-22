"""
claude_runner/inbox.py

Buffer-mediated message injection for claude-runner (Feature A1).

Allows external messages (delivered via ntfy cmd channel) to be accumulated
in a file and injected into a running Claude Code session at natural pause
points, without interrupting active tool use.

Buffer file: ~/.claude-runner/inbox/pending.md  (append-only during accumulation)

Full lifecycle (two-flag system):
  1. External source calls append_message(text)
     → appends timestamped entry, sets has_pending_messages = True
  2. Runner calls drain(process) at a natural pause
     → injects "read pending.md" prompt into Claude Code stdin
     → waits for acknowledgement (any output)
     → sets has_pending_messages = False
     → sets processing_pending_message = True (begin response capture)
  3. Runner's _on_output_line feeds each line to capture_line()
     → while processing_pending_message is True, lines are buffered
     → on end marker or max lines, _flush_response() fires
     → invokes the registered callback (auto-forward to ntfy out)
     → sets processing_pending_message = False
  4. Next append_message() runs _trim_if_needed()
     → if has_pending_messages is False AND file exceeds _MAX_BYTES,
        deletes consumed entries — prevents unbounded growth

Module-level API
----------------
append_message(text)           — append text with timestamp header; sets flag
drain(process, timeout_s)      — inject, wait ack, clear buffer, start capture
has_pending_messages            — True = unread content waiting
processing_pending_message      — True = LLM is responding, output captured
is_pending()                   — read has_pending_messages
is_processing()                — read processing_pending_message
capture_line(line)             — feed LLM output line into response buffer
set_response_callback(fn)      — register auto-forward callback
trim_consumed()                — trim old entries if LLM has consumed them
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
# True  = new content waiting for the LLM to read
# False = LLM has consumed the content (drain completed), or no content
has_pending_messages: bool = False

# Processing flag — True between drain() and response capture completion.
# While True, the runner's _on_output_line should capture output into
# the response buffer for auto-forwarding to ntfy out channel.
processing_pending_message: bool = False

# Response buffer — accumulated LLM output while processing_pending_message.
_response_buffer: list[str] = []

# Callback invoked with the captured response text when processing completes.
# Set by the runner to forward the response to ntfy.
_on_response_ready: callable | None = None  # type: ignore[type-arg]

# Max lines to capture before auto-flushing the response.
_MAX_RESPONSE_LINES: int = 50

# Lines that signal the LLM has finished responding to the pending message
# and resumed normal work (tool calls, phase markers, etc.)
_RESPONSE_END_MARKERS: list[str] = [
    "##RUNNER:",          # runner protocol marker
    "```tool_call",       # starting a tool call
    "[ACTION]",           # runner action marker
    "PHASE-",             # phase commit
]

# Hard limit: trim old entries when pending.md exceeds this size.
_MAX_BYTES: int = 32_768  # 32 KB

# Entry separator — every entry starts with this pattern.
# Used by trim logic to split entries.
_ENTRY_SEPARATOR = "\n---\n"

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

    Before appending, trims consumed entries if the file is over the size
    limit and the LLM has already read the previous content.

    Sets :data:`has_pending_messages` to ``True``.
    Does NOT interrupt Claude Code.

    Parameters
    ----------
    text:
        The message body to accumulate.
    """
    global has_pending_messages

    # Trim old consumed entries before adding new content.
    _trim_if_needed()

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = f"{_ENTRY_SEPARATOR}**Received {ts}**\n\n"
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

    After successful drain:
    - has_pending_messages is set to False
    - pending.md is truncated (the LLM has consumed the content)

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

    # Truncate pending.md — the LLM has consumed the content.
    try:
        _PENDING_FILE.write_text("", encoding="utf-8")
        logger.info("inbox.drain: pending.md truncated.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("inbox.drain: failed to truncate pending.md: %s", exc)

    has_pending_messages = False

    # Start capturing the LLM's response for auto-forwarding.
    _start_response_capture()


def is_pending() -> bool:
    """Return True if there are pending messages waiting to be injected."""
    return has_pending_messages


def trim_consumed() -> None:
    """Trim old entries from pending.md if the LLM has consumed them.

    Safe to call at any time.  Only trims when:
    1. has_pending_messages is False (LLM has read the content), AND
    2. The file still has content (leftover from before drain)

    This is the manual entry point for external scripts that manage
    the pending.md lifecycle independently of the runner process.
    """
    global has_pending_messages

    if has_pending_messages:
        # Content hasn't been consumed yet — don't trim.
        return

    try:
        if not _PENDING_FILE.exists():
            return
        size = _PENDING_FILE.stat().st_size
        if size == 0:
            return
        # LLM has consumed but file still has content — truncate.
        _PENDING_FILE.write_text("", encoding="utf-8")
        logger.info("inbox.trim_consumed: truncated %d bytes of consumed content.", size)
    except Exception as exc:  # noqa: BLE001
        logger.warning("inbox.trim_consumed failed: %s", exc)


def set_response_callback(callback) -> None:
    """Register a callback invoked with the captured response text.

    The runner wires this at init time to forward responses to ntfy.
    Signature: callback(response_text: str) -> None

    Parameters
    ----------
    callback:
        Called with the full captured response when the LLM finishes
        responding to the pending message.
    """
    global _on_response_ready
    _on_response_ready = callback


def capture_line(line: str) -> None:
    """Feed an output line from the LLM into the response capture buffer.

    Called by the runner's _on_output_line() on every clean output line.
    Only captures when processing_pending_message is True.

    Auto-flushes the response when:
    - An end marker is detected (LLM resumed normal work), or
    - The buffer exceeds _MAX_RESPONSE_LINES
    """
    global processing_pending_message

    if not processing_pending_message:
        return

    if not line.strip():
        return

    # Check if this line signals the LLM moved on to normal work.
    for marker in _RESPONSE_END_MARKERS:
        if marker in line:
            logger.info(
                "inbox.capture_line: end marker %r detected — flushing response.", marker,
            )
            _flush_response()
            return

    _response_buffer.append(line)

    # Auto-flush on max lines.
    if len(_response_buffer) >= _MAX_RESPONSE_LINES:
        logger.info("inbox.capture_line: max lines reached — flushing response.")
        _flush_response()


def is_processing() -> bool:
    """Return True if the LLM is currently responding to a pending message."""
    return processing_pending_message


def reset() -> None:
    """
    Reset module state.  Used by tests to get a clean slate between runs.
    """
    global has_pending_messages, processing_pending_message
    has_pending_messages = False
    processing_pending_message = False
    _response_buffer.clear()
    try:
        if _PENDING_FILE.exists():
            _PENDING_FILE.write_text("", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _start_response_capture() -> None:
    """Begin capturing the LLM's response to the pending message."""
    global processing_pending_message
    _response_buffer.clear()
    processing_pending_message = True
    logger.info("inbox: response capture started — LLM output will be buffered.")


def _flush_response() -> None:
    """Flush the captured response buffer and invoke the callback."""
    global processing_pending_message

    processing_pending_message = False
    response_text = "\n".join(_response_buffer).strip()
    _response_buffer.clear()

    if not response_text:
        logger.debug("inbox._flush_response: empty response — nothing to forward.")
        return

    logger.info(
        "inbox._flush_response: captured %d chars — invoking callback.", len(response_text),
    )

    if _on_response_ready is not None:
        try:
            _on_response_ready(response_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("inbox._flush_response: callback failed: %s", exc)
    else:
        logger.debug("inbox._flush_response: no callback registered — response discarded.")


def _trim_if_needed() -> None:
    """Trim oldest entries if file exceeds _MAX_BYTES and content was consumed.

    Called automatically before every append_message().

    If has_pending_messages is False (LLM consumed prior content), the whole
    file is truncated — old messages are no longer needed.

    If has_pending_messages is True (content not yet consumed), trim the
    oldest entries to keep the file under _MAX_BYTES, preserving the newest
    messages that the LLM hasn't read yet.
    """
    global has_pending_messages

    try:
        if not _PENDING_FILE.exists():
            return
        size = _PENDING_FILE.stat().st_size
        if size <= _MAX_BYTES:
            return
    except Exception:  # noqa: BLE001
        return

    # Case 1: LLM already consumed — safe to wipe everything.
    if not has_pending_messages:
        try:
            _PENDING_FILE.write_text("", encoding="utf-8")
            logger.info(
                "inbox._trim_if_needed: cleared %d bytes of consumed content.", size,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("inbox._trim_if_needed: truncate failed: %s", exc)
        return

    # Case 2: LLM hasn't consumed yet — keep newest entries, drop oldest.
    try:
        content = _PENDING_FILE.read_text(encoding="utf-8")
        # Split on entry separator, keep the newest half.
        parts = content.split(_ENTRY_SEPARATOR)
        if len(parts) <= 2:
            # Only one or two entries — can't trim further.
            return

        # Keep the second half of entries (newest).
        keep_from = len(parts) // 2
        trimmed = _ENTRY_SEPARATOR + _ENTRY_SEPARATOR.join(parts[keep_from:])
        _PENDING_FILE.write_text(trimmed, encoding="utf-8")
        dropped = len(parts) - (len(parts) - keep_from)
        logger.info(
            "inbox._trim_if_needed: dropped %d oldest entries, kept %d (was %d bytes).",
            dropped, len(parts) - keep_from, size,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("inbox._trim_if_needed: trim failed: %s", exc)


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
