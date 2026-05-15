"""
tui.py — Fixed-height ANSI overwrite renderer for claude-runner.

Rendering strategy
------------------
On start(), TUI_HEIGHT blank lines are printed to stdout to reserve vertical
space.  Every render call moves the cursor up TUI_HEIGHT lines and overwrites
all of them with the current frame.  No Rich Live, Layout, or Panel components
are used; Rich Console is kept only for non-live output (e.g. main.py banners).

Frame is always exactly TUI_HEIGHT = 28 lines:

  Line  1   ══ claude-runner ══  (centered header)
  Line  2   blank
  Line  3   task name + project path
  Line  4   blank
  Line  5   STATUS header + state indicator / spinner
  Line  6   Elapsed / Est.left / Timeout [/ context anchors]
  Line  7   CONTEXT header + token usage + progress bar
  Line  8   Checkpoints
  Line  9   Rate limit waits
  Line 10   blank
  Line 11   CLAUDE OUTPUT (last 12 lines) header
  Lines 12-23  Claude output  (OUTPUT_LINES = 12)
  Line 24   blank
  Line 25   NOTIFICATIONS header
  Lines 26-27  Last 2 notifications
  Line 28   RESOURCES line
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime

# ──────────────────────────────────────────────────────────────────────────────
# Layout constants
# ──────────────────────────────────────────────────────────────────────────────

TUI_HEIGHT        = 28   # total lines the TUI occupies
OUTPUT_LINES      = 12   # lines reserved for Claude output (lines 12-23)
NOTIFICATION_LINES = 3   # lines reserved for notifications: 1 header + 2 entries

OUTPUT_BUFFER_SIZE        = 200  # lines retained in output deque
NOTIFICATION_BUFFER_SIZE  = 50   # notifications retained

# ──────────────────────────────────────────────────────────────────────────────
# ANSI primitives
# ──────────────────────────────────────────────────────────────────────────────

_RST    = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_CYAN   = "\033[36m"
_WHITE  = "\033[37m"

_SPINNER_FRAMES = ["-", "\\", "|", "/"]
_SPINNER_STATES = frozenset({"running", "starting", "resuming"})

_ANSI_RE   = re.compile(r'\033\[[0-9;]*[A-Za-z]')
_RICH_TAG  = re.compile(r'\[/?[^\]]+\]')

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _fmt_duration(seconds: float) -> str:
    """Return human-readable duration, e.g. '1h 23m 45s'."""
    if seconds < 0:
        return "—"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 120


def _strip(text: str) -> str:
    """Strip ANSI escape sequences from *text*."""
    return _ANSI_RE.sub("", text)


def _fit(text: str, max_w: int) -> str:
    """
    Ensure *text* occupies at most *max_w* visible columns.

    If the visible length exceeds *max_w*, ANSI codes are dropped and the
    plain text is truncated with '...'.
    """
    if len(_strip(text)) <= max_w:
        return text
    plain = _strip(text)
    return plain[: max_w - 3] + "..."


# ──────────────────────────────────────────────────────────────────────────────
# TUIManager
# ──────────────────────────────────────────────────────────────────────────────


class TUIManager:
    """
    Fixed-height ANSI-overwrite terminal UI for claude-runner.

    Thread safety
    -------------
    Every public state-mutation method acquires ``_lock`` before modifying
    state and calling ``_render()``.  ``_render()`` must never be called
    from outside ``TuiManager``.

    Usage::

        tui = TUIManager(task_name="My task",
                         project_book_path="task.yaml",
                         timeout_hours=2.0)
        tui.start()
        try:
            tui.update_state("running")
            tui.add_output_line("Hello from Claude")
            tui.add_notification("Task started")
        finally:
            tui.stop()
    """

    def __init__(
        self,
        task_name: str,
        project_book_path: str,
        timeout_hours: float,
        refresh_per_second: float = 4.0,
    ) -> None:
        self._task_name          = task_name
        self._project_book_path  = project_book_path
        self._timeout_hours      = timeout_hours
        self._refresh_interval   = 1.0 / max(1.0, refresh_per_second)

        # Mutable display state
        self._state: str                  = "starting"
        self._start_time: float           = time.monotonic()
        self._output_lines: deque[str]    = deque(maxlen=OUTPUT_BUFFER_SIZE)
        self._notifications: deque[str]   = deque(maxlen=NOTIFICATION_BUFFER_SIZE)
        self._token_estimated: int        = 0
        self._token_threshold: int        = 150_000
        self._checkpoints: int            = 0
        self._rate_limit_waits: int       = 0
        self._rate_limit_end: float | None = None  # monotonic end timestamp
        self._context_anchors_active: bool = False
        self._docker_status: str          = "unknown"
        self._disk_usage_mb: float        = 0.0

        # Internals
        self._lock          = threading.Lock()
        self._spinner_idx   = 0
        self._active        = False
        self._stop_evt      = threading.Event()
        self._refresh_thread: threading.Thread | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Reserve TUI_HEIGHT lines and begin the refresh loop."""
        self._start_time = time.monotonic()
        self._active = True
        self._stop_evt.clear()
        # Print blank lines to occupy the vertical space we will overwrite.
        sys.stdout.write("\n" * TUI_HEIGHT)
        sys.stdout.flush()
        with self._lock:
            self._render()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            name="tui-refresh",
            daemon=True,
        )
        self._refresh_thread.start()

    def stop(self) -> None:
        """Stop the refresh loop and leave the final frame visible."""
        self._active = False
        self._stop_evt.set()
        if self._refresh_thread is not None:
            self._refresh_thread.join(timeout=1.0)
            self._refresh_thread = None
        # Render final state (active flag is False so _refresh_loop won't race).
        with self._lock:
            self._do_render()
        # One blank line so subsequent terminal output starts cleanly below.
        sys.stdout.write("\n")
        sys.stdout.flush()

    # ── Public state mutations ─────────────────────────────────────────────────

    def update_state(self, state: str) -> None:
        """Update current state label (running / waiting / resuming / done / failed)."""
        with self._lock:
            self._state = state.lower()
            self._render()

    def add_output_line(self, clean_line: str) -> None:
        """Append a line to the Claude output panel (keeps last OUTPUT_BUFFER_SIZE)."""
        with self._lock:
            self._output_lines.append(clean_line)
            self._render()

    def add_notification(self, message: str) -> None:
        """Append a timestamped notification entry."""
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._notifications.append(f"[{ts}] {message}")
            self._render()

    def update_tokens(self, estimated: int, threshold: int, checkpoints: int) -> None:
        """Update the context-window token indicator."""
        with self._lock:
            self._token_estimated = estimated
            self._token_threshold = threshold
            self._checkpoints     = checkpoints
            self._render()

    def update_rate_limit_countdown(self, remaining_seconds: float) -> None:
        """
        Show rate-limit countdown in the output panel.

        Pass ``0`` or a negative value to restore normal output display.
        """
        with self._lock:
            if remaining_seconds > 0:
                self._rate_limit_end = time.monotonic() + remaining_seconds
            else:
                self._rate_limit_end = None
            self._render()

    def update_resources(self, docker_status: str, disk_usage_mb: float) -> None:
        """Update resource indicators (Docker status, disk usage)."""
        with self._lock:
            self._docker_status  = docker_status
            self._disk_usage_mb  = disk_usage_mb
            self._render()

    def update_rate_limit_waits(self, count: int) -> None:
        """Update the rate-limit wait counter shown in the context panel."""
        with self._lock:
            self._rate_limit_waits = count
            self._render()

    def set_context_anchors_active(self, active: bool) -> None:
        """Show or hide the [context anchors: active] indicator."""
        with self._lock:
            self._context_anchors_active = active
            self._render()

    def print_message(self, message: str, style: str = "") -> None:
        """
        Log *message* as a notification.

        Rich markup and ANSI codes are stripped before display so the
        fixed-width renderer is not confused by embedded markup.
        """
        clean = _ANSI_RE.sub("", _RICH_TAG.sub("", message)).strip()
        if clean:
            self.add_notification(clean)

    # ── Internal rendering ─────────────────────────────────────────────────────

    def _refresh_loop(self) -> None:
        """Background daemon thread: re-render on a timer for spinner animation."""
        while not self._stop_evt.wait(timeout=self._refresh_interval):
            with self._lock:
                if self._active:
                    self._render()

    def _render(self) -> None:
        """
        Trigger a render if the TUI is active.  Must be called with _lock held.
        Delegates to _do_render() so stop() can render without the active guard.
        """
        if self._active:
            self._do_render()

    def _do_render(self) -> None:
        """
        Build the frame and overwrite the fixed block.
        Must be called with _lock held.
        """
        frame = self._build_frame()

        # Safety: enforce exactly TUI_HEIGHT lines.
        while len(frame) < TUI_HEIGHT:
            frame.append("")
        frame = frame[:TUI_HEIGHT]

        # Advance spinner after the frame snapshot so index is stable per render.
        if self._state in _SPINNER_STATES:
            self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER_FRAMES)

        out: list[str] = [f"\033[{TUI_HEIGHT}A"]
        for line in frame:
            out.append(f"\033[2K{line}\n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    # ── Frame builder ─────────────────────────────────────────────────────────

    def _build_frame(self) -> list[str]:
        """
        Build exactly TUI_HEIGHT lines for the current state.

        Must be called with _lock held.  Reads all state directly from self.
        """
        w     = _term_width()
        max_w = w - 2  # leave a small right margin

        elapsed = time.monotonic() - self._start_time

        # Rate-limit countdown
        if self._rate_limit_end is not None:
            rl_rem = max(0.0, self._rate_limit_end - time.monotonic())
        else:
            rl_rem = 0.0
        in_rl = rl_rem > 0

        L: list[str] = []

        # ── Line 1: header ────────────────────────────────────────────────────
        title = "  claude-runner  "
        fill  = max(0, w - len(title) - 2)
        lpad, rpad = fill // 2, fill - fill // 2
        L.append(f"{_BOLD}{_CYAN}{'═' * lpad}{title}{'═' * rpad}{_RST}")

        # ── Line 2: blank ─────────────────────────────────────────────────────
        L.append("")

        # ── Line 3: task + project path ───────────────────────────────────────
        L.append(_fit(
            f"  task: {_BOLD}{self._task_name}{_RST}   "
            f"project: {_DIM}{self._project_book_path}{_RST}",
            max_w,
        ))

        # ── Line 4: blank ─────────────────────────────────────────────────────
        L.append("")

        # ── Line 5: STATUS header + state ────────────────────────────────────
        spf = _SPINNER_FRAMES[self._spinner_idx]
        icon = {
            "complete": "✓", "failed": "✗", "error": "✗", "waiting": "⏸",
        }.get(self._state, spf if self._state in _SPINNER_STATES else "·")
        col = {
            "running":  _GREEN,  "resuming": _CYAN,  "starting": _WHITE,
            "complete": _GREEN,  "waiting":  _YELLOW, "failed":   _RED,
            "error":    _RED,
        }.get(self._state, _WHITE)
        L.append(_fit(
            f"  {_BOLD}STATUS{_RST}   "
            f"State: {col}{icon} {self._state.upper()}{_RST}",
            max_w,
        ))

        # ── Line 6: timing row ────────────────────────────────────────────────
        timeout_s = self._timeout_hours * 3600
        eta = (
            _fmt_duration(timeout_s - elapsed)
            if elapsed < timeout_s
            else f"{_RED}exceeded{_RST}"
        )
        anchors = (
            f"   {_CYAN}[context anchors: active]{_RST}"
            if self._context_anchors_active else ""
        )
        L.append(_fit(
            f"  {_DIM}Elapsed:{_RST} {_fmt_duration(elapsed)}   "
            f"{_DIM}Est.left:{_RST} {eta}   "
            f"{_DIM}Timeout:{_RST} {self._timeout_hours:.1f}h"
            f"{anchors}",
            max_w,
        ))

        # ── Line 7: CONTEXT header + tokens + progress bar ───────────────────
        pct    = min(1.0, self._token_estimated / self._token_threshold) if self._token_threshold else 0.0
        filled = int(pct * 20)
        bc     = _RED if pct >= 0.85 else _YELLOW if pct >= 0.60 else _GREEN
        bar    = f"{bc}{'█' * filled}{_DIM}{'░' * (20 - filled)}{_RST}"
        thr    = self._token_threshold
        thr_s  = (
            f"{thr // 1_000_000:.1f}M" if thr >= 1_000_000
            else f"{thr // 1_000}k"    if thr >= 1_000
            else str(thr)
        )
        L.append(_fit(
            f"  {_BOLD}CONTEXT{_RST}   "
            f"{_DIM}Tokens:{_RST} {bc}~{self._token_estimated:,}{_RST} / {thr_s}   "
            f"{bar}",
            max_w,
        ))

        # ── Line 8: Checkpoints ───────────────────────────────────────────────
        L.append(f"  {_DIM}Checkpoints:{_RST}       {_CYAN}{self._checkpoints}{_RST}")

        # ── Line 9: Rate limit waits ──────────────────────────────────────────
        rlc = _YELLOW if self._rate_limit_waits else _DIM
        L.append(f"  {_DIM}Rate limit waits:{_RST}  {rlc}{self._rate_limit_waits}{_RST}")

        # ── Line 10: blank ────────────────────────────────────────────────────
        L.append("")

        # ── Line 11: CLAUDE OUTPUT header ─────────────────────────────────────
        L.append(f"  {_BOLD}── CLAUDE OUTPUT (last {OUTPUT_LINES} lines){_RST}")

        # ── Lines 12–23: output content or countdown (OUTPUT_LINES = 12) ──────
        if in_rl:
            L.extend(self._countdown_block(rl_rem, OUTPUT_LINES, max_w))
        else:
            L.extend(self._output_block(OUTPUT_LINES, max_w))

        # ── Line 24: blank ────────────────────────────────────────────────────
        L.append("")

        # ── Line 25: NOTIFICATIONS header ─────────────────────────────────────
        L.append(f"  {_BOLD}── NOTIFICATIONS{_RST}")

        # ── Lines 26–27: last 2 notifications ─────────────────────────────────
        notif_entries = NOTIFICATION_LINES - 1  # = 2
        notifs = list(self._notifications)[-notif_entries:]
        for entry in notifs:
            L.append("  " + _fit(entry, max_w - 2))
        for _ in range(notif_entries - len(notifs)):
            L.append("")

        # ── Line 28: RESOURCES ────────────────────────────────────────────────
        dc = {
            "running": _GREEN, "stopped": _RED,
            "starting": _YELLOW, "native": _CYAN,
        }.get(self._docker_status.lower(), _DIM)
        disk_s = (
            f"{self._disk_usage_mb / 1024:.2f} GB"
            if self._disk_usage_mb >= 1024
            else f"{self._disk_usage_mb:.1f} MB"
        )
        L.append(_fit(
            f"  {_DIM}Docker:{_RST} {dc}{self._docker_status}{_RST}   "
            f"{_DIM}Disk (workdir):{_RST} {disk_s} used",
            max_w,
        ))

        return L  # caller enforces exactly TUI_HEIGHT

    # ── Panel helpers ─────────────────────────────────────────────────────────

    def _output_block(self, count: int, max_w: int) -> list[str]:
        """
        Return exactly *count* lines of Claude output.

        The most-recent output lines appear at the bottom; blank lines fill
        the top when the buffer is not yet full.
        """
        buf    = list(self._output_lines)[-count:]
        result = [f"  {_fit(line, max_w - 2)}" for line in buf]
        # Prepend blanks so newest output sits at the bottom of the panel.
        blanks = [""] * (count - len(result))
        return blanks + result

    def _countdown_block(self, remaining: float, count: int, max_w: int) -> list[str]:
        """Return exactly *count* lines showing the rate-limit countdown."""
        m, s = divmod(int(remaining), 60)
        h, m = divmod(m, 60)
        clock = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

        banner: list[str] = [
            "",
            f"  {_YELLOW}{_BOLD}  ⏸  RATE LIMIT — waiting for reset{_RST}",
            "",
            f"       {_BOLD}{_YELLOW}{clock}{_RST}",
            "",
            f"  {_DIM}  claude-runner will resume automatically{_RST}",
        ]
        while len(banner) < count:
            banner.append("")
        return banner[:count]


# ──────────────────────────────────────────────────────────────────────────────
# Standalone smoke-test
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import time as _time

    tui = TUIManager(
        task_name="Example task",
        project_book_path="projects/example_simple.yaml",
        timeout_hours=2.0,
    )
    tui.start()

    tui.update_state("running")
    tui.update_resources("native", 142.3)
    tui.add_notification("Task started")

    for i in range(6):
        _time.sleep(0.5)
        tui.add_output_line(f"> Processing step {i + 1}...")
        tui.update_tokens(estimated=i * 8_000, threshold=150_000, checkpoints=i // 3)

    tui.add_notification("Rate limit hit — waiting 30 seconds")
    tui.update_state("waiting")
    tui.update_rate_limit_countdown(30)
    tui.update_rate_limit_waits(1)

    _time.sleep(5)
    tui.update_rate_limit_countdown(0)
    tui.update_state("resuming")
    tui.add_notification("Resuming after rate limit wait")

    _time.sleep(1)
    tui.update_state("running")
    tui.add_output_line("Continuing from where we left off...")

    _time.sleep(2)
    tui.update_state("complete")
    tui.add_notification("Task complete")

    _time.sleep(2)
    tui.stop()
    print("TUI stopped.")
    sys.exit(0)
