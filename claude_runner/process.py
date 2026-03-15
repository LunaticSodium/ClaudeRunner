"""
process.py — Claude Code subprocess management via PTY (Windows).

Primary backend:  pywinpty (ConPTY) — requires Windows 10 build 1809+
Fallback backend: wexpect     — Windows pexpect port for older systems

Responsibilities
----------------
- Spawn Claude Code with the given prompt / working directory.
- Stream PTY output in real time (raw bytes → decoded lines).
- Strip ANSI escape codes for log storage; keep raw text for TUI consumers.
- Inject text into PTY stdin (e.g. "continue\\n" to resume after a prompt).
- Detect process termination (normal exit, crash, timeout).
- Emit every output line to a caller-supplied callback:
      on_line(raw_line: str, clean_line: str)
- Emit the final exit code via:
      on_exit(exit_code: int)
- Normalise CRLF → LF on all output before delivering to callbacks.
"""

from __future__ import annotations

import asyncio
import re
import os
import sys
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ANSI escape-code stripping
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(
    r"""
    \x1b          # ESC
    (?:
      \[ [0-9;]* [mGKHFJT]    # CSI sequences: colour, cursor movement, erase
    | \[? \d*     [A-Za-z]     # other CSI / private sequences
    | [()][AB]                 # character-set designations
    | [NO]                     # SS2 / SS3
    | \].*?(?:\x07|\x1b\\)     # OSC sequences (window title, etc.)
    )
    """,
    re.VERBOSE,
)


def strip_ansi(text: str) -> str:
    """Return *text* with all ANSI/VT escape sequences removed."""
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProcessError(Exception):
    """Raised for unrecoverable errors in ClaudeProcess lifecycle."""


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

_BACKEND: Optional[str] = None  # "winpty" | "wexpect" | None (detected lazily)


def _detect_backend() -> str:
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    try:
        import winpty  # noqa: F401  (pywinpty)
        _BACKEND = "winpty"
        log.debug("PTY backend: pywinpty (ConPTY)")
        return _BACKEND
    except ImportError:
        pass
    try:
        import wexpect  # noqa: F401
        _BACKEND = "wexpect"
        log.debug("PTY backend: wexpect")
        return _BACKEND
    except ImportError:
        pass
    raise ProcessError(
        "No PTY backend available. "
        "Install pywinpty (recommended: pip install pywinpty) "
        "or wexpect (pip install wexpect) and try again."
    )


# ---------------------------------------------------------------------------
# ClaudeProcess
# ---------------------------------------------------------------------------


class ClaudeProcess:
    """
    Manages a Claude Code subprocess via a Windows pseudo-terminal (PTY).

    Parameters
    ----------
    command:     Argument list, e.g.
                 ["claude", "--dangerously-skip-permissions", "-p", prompt]
    working_dir: Working directory for the subprocess.
    env:         Environment mapping (must include ANTHROPIC_API_KEY etc.).
    on_line:     Callback invoked for every output line:
                     on_line(raw_line: str, clean_line: str)
                 *raw_line* retains ANSI codes; *clean_line* has them stripped.
                 Both have CRLF normalised to LF.
    on_exit:     Callback invoked once when the process terminates:
                     on_exit(exit_code: int)
                 exit_code is -1 when the process was killed/crashed without a
                 normal exit status.
    cols / rows: Initial PTY dimensions (defaults to 220 × 50).
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        command: list[str],
        working_dir: Path,
        env: dict[str, str],
        on_line: Callable[[str, str], None],
        on_exit: Callable[[int], None],
        *,
        cols: int = 220,
        rows: int = 50,
    ) -> None:
        if not command:
            raise ProcessError("command must be a non-empty list")

        self._command = command
        self._working_dir = Path(working_dir)
        self._env = dict(env)
        self._on_line = on_line
        self._on_exit = on_exit
        self._cols = cols
        self._rows = rows

        self._backend: Optional[str] = None
        self._pty = None          # winpty.PtyProcess  or  wexpect child
        self._pid: int = -1
        self._exit_code: Optional[int] = None

        self._reader_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Line buffer for partial lines arriving across read() chunks
        self._line_buf = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Spawn the subprocess inside a PTY and begin streaming output.

        Raises ProcessError if the subprocess cannot be launched.
        """
        with self._lock:
            if self._pty is not None:
                raise ProcessError("ClaudeProcess.start() called more than once")

            self._backend = _detect_backend()

            try:
                if self._backend == "winpty":
                    self._start_winpty()
                else:
                    self._start_wexpect()
            except ProcessError:
                raise
            except Exception as exc:
                raise ProcessError(f"Failed to spawn subprocess: {exc}") from exc

            self._stop_event.clear()
            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                name="claude-pty-reader",
                daemon=True,
            )
            self._reader_thread.start()
            log.info("ClaudeProcess started (pid=%s, backend=%s)", self._pid, self._backend)

    def send(self, text: str) -> None:
        """
        Write *text* to the PTY stdin.

        A CRLF line-ending is appended automatically so that a bare string
        such as ``"continue"`` works as a complete terminal input line.
        """
        if not self.is_alive():
            log.warning("send() called but process is not alive — ignoring")
            return
        if not text.endswith(("\r\n", "\r", "\n")):
            text = text + "\r\n"
        try:
            if self._backend == "winpty":
                self._pty.write(text)
            else:
                self._pty.send(text)
        except Exception as exc:
            log.warning("send() failed: %s", exc)

    def stop(self, timeout: float = 5.0) -> None:
        """
        Gracefully terminate the subprocess, then wait up to *timeout* seconds
        for the reader thread to finish before force-killing.
        """
        self._stop_event.set()

        if self._pty is None:
            return

        try:
            if self._backend == "winpty":
                self._pty.close()
            else:
                self._pty.terminate(force=True)
        except Exception as exc:
            log.debug("stop(): ignoring error while closing PTY: %s", exc)

        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=timeout)
            if self._reader_thread.is_alive():
                log.warning("Reader thread did not stop within %.1fs", timeout)

        log.info("ClaudeProcess stopped (exit_code=%s)", self._exit_code)

    def is_alive(self) -> bool:
        """Return True if the subprocess is still running."""
        if self._pty is None:
            return False
        try:
            if self._backend == "winpty":
                return self._pty.isalive()
            else:
                return self._pty.isalive()
        except Exception:
            return False

    async def wait(self) -> int:
        """
        Async-compatible wait: resolves when the subprocess exits.

        Blocks in a thread-pool executor until the reader thread finishes,
        then returns the exit code (-1 if not determinable).
        """
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._wait_sync)
        return self._exit_code if self._exit_code is not None else -1

    def _wait_sync(self) -> None:
        """Block until the PTY reader thread has finished (process exited)."""
        if self._reader_thread is not None:
            self._reader_thread.join()

    @property
    def pid(self) -> int:
        """OS process ID, or -1 if the process has not been started."""
        return self._pid

    @property
    def exit_code(self) -> Optional[int]:
        """
        Exit code of the subprocess, or None if it is still running.
        -1 indicates an abnormal termination without a retrievable exit status.
        """
        return self._exit_code

    # ------------------------------------------------------------------
    # Backend-specific startup
    # ------------------------------------------------------------------

    def _start_winpty(self) -> None:
        """Spawn via pywinpty (ConPTY)."""
        import winpty

        # Build a single command string; winpty expects a string, not a list
        cmd_str = _list_to_cmdline(self._command)

        self._pty = winpty.PtyProcess.spawn(
            cmd_str,
            cwd=str(self._working_dir),
            env=self._env,
            dimensions=(self._rows, self._cols),
        )
        # pywinpty does not expose .pid directly on all versions
        try:
            self._pid = self._pty.pid
        except AttributeError:
            self._pid = -1

    def _start_wexpect(self) -> None:
        """Spawn via wexpect (fallback for older Windows)."""
        import wexpect

        # wexpect.spawn takes a single command string
        cmd_str = _list_to_cmdline(self._command)

        self._pty = wexpect.spawn(
            cmd_str,
            cwd=str(self._working_dir),
            env=self._env,
            codec_errors="replace",
            logfile=None,
        )
        try:
            self._pid = self._pty.pid
        except AttributeError:
            self._pid = -1

    # ------------------------------------------------------------------
    # Output reading loop (runs in background thread)
    # ------------------------------------------------------------------

    _READ_SIZE = 4096          # bytes per read() call
    _READ_TIMEOUT_SEC = 0.1    # non-blocking read poll interval

    def _reader_loop(self) -> None:
        """
        Background thread: read raw PTY output, split into lines, invoke callbacks.
        """
        try:
            while not self._stop_event.is_set():
                chunk = self._read_chunk()
                if chunk is None:
                    # Process has exited
                    break
                if chunk:
                    self._process_chunk(chunk)
        except Exception as exc:
            log.exception("Unexpected error in PTY reader loop: %s", exc)
        finally:
            # Flush any remaining partial line in the buffer
            if self._line_buf:
                self._deliver_line(self._line_buf)
                self._line_buf = ""
            # Retrieve and emit exit code
            exit_code = self._collect_exit_code()
            self._exit_code = exit_code
            if self._on_exit is not None:
                try:
                    self._on_exit(exit_code)
                except Exception as cb_exc:
                    log.warning("on_exit callback raised: %s", cb_exc)

    def _read_chunk(self) -> Optional[str]:
        """
        Read a chunk of output from the PTY.

        Returns:
            str   — decoded text (may be empty string for timeout / no data)
            None  — the child process has exited and there is no more data
        """
        try:
            if self._backend == "winpty":
                return self._read_chunk_winpty()
            else:
                return self._read_chunk_wexpect()
        except EOFError:
            return None
        except Exception as exc:
            if self._stop_event.is_set():
                return None
            log.debug("_read_chunk error (treating as EOF): %s", exc)
            return None

    def _read_chunk_winpty(self) -> Optional[str]:
        """Read from a pywinpty PtyProcess."""
        import winpty

        if not self._pty.isalive():
            # Drain any buffered data first
            try:
                data = self._pty.read(self._READ_SIZE)
                if data:
                    return data if isinstance(data, str) else data.decode("utf-8", errors="replace")
            except Exception:
                pass
            return None

        try:
            data = self._pty.read(self._READ_SIZE)
        except (EOFError, OSError):
            return None

        if data is None:
            time.sleep(self._READ_TIMEOUT_SEC)
            return ""

        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return data

    def _read_chunk_wexpect(self) -> Optional[str]:
        """Read from a wexpect child."""
        import wexpect

        if not self._pty.isalive():
            try:
                self._pty.expect(wexpect.EOF, timeout=1)
            except Exception:
                pass
            buf = getattr(self._pty, "before", "") or ""
            return buf if buf else None

        try:
            idx = self._pty.expect([r".+", wexpect.EOF, wexpect.TIMEOUT], timeout=self._READ_TIMEOUT_SEC)
        except Exception:
            return None

        if idx == 1:  # EOF
            buf = getattr(self._pty, "before", "") or ""
            return buf if buf else None
        if idx == 2:  # TIMEOUT — no data right now
            return ""

        matched = getattr(self._pty, "after", "") or ""
        before = getattr(self._pty, "before", "") or ""
        return before + matched

    # ------------------------------------------------------------------
    # Line splitting & delivery
    # ------------------------------------------------------------------

    def _process_chunk(self, chunk: str) -> None:
        """
        Append *chunk* to the internal line buffer, split on newlines, and
        deliver each complete line to the callbacks.
        """
        # Normalise CRLF → LF before buffering
        chunk = chunk.replace("\r\n", "\n").replace("\r", "\n")
        self._line_buf += chunk

        while "\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split("\n", 1)
            self._deliver_line(line)

    def _deliver_line(self, line: str) -> None:
        """Strip ANSI from *line* and invoke the on_line callback."""
        clean = strip_ansi(line)
        try:
            self._on_line(line, clean)
        except Exception as cb_exc:
            log.warning("on_line callback raised: %s", cb_exc)

    # ------------------------------------------------------------------
    # Exit code collection
    # ------------------------------------------------------------------

    def _collect_exit_code(self) -> int:
        """
        Retrieve the subprocess exit code from the PTY handle.
        Returns -1 if the code cannot be determined.
        """
        if self._pty is None:
            return -1
        try:
            if self._backend == "winpty":
                return self._pty.exitstatus if self._pty.exitstatus is not None else -1
            else:
                status = getattr(self._pty, "exitstatus", None)
                if status is None:
                    status = getattr(self._pty, "status", None)
                return int(status) if status is not None else -1
        except Exception as exc:
            log.debug("Could not retrieve exit code: %s", exc)
            return -1


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _list_to_cmdline(args: list[str]) -> str:
    """
    Convert an argument list to a Windows command-line string.

    Uses subprocess.list2cmdline which follows the MSVC quoting rules that
    the Windows CRT CommandLineToArgvW parser expects.
    """
    import subprocess
    return subprocess.list2cmdline(args)
