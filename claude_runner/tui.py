"""
tui.py — Rich-based terminal UI for claude-runner.

Purely informational: no user input is required or accepted.

Layout:
    ┌─────────────────────────────────────────────────────┐
    │  claude-runner  │  task: <name>                      │
    │  project: <path>                                     │
    ├──────────────┬──────────────────────────────────────┤
    │  STATUS      │  CONTEXT                              │
    │  State: ...  │  Tokens: ~12,450 / 150k               │
    │  Elapsed: .. │  Checkpoints: 0                       │
    │  Timeout: .. │  Rate limit waits: 0                  │
    ├──────────────┴──────────────────────────────────────┤
    │  CLAUDE OUTPUT (last 20 lines)                       │
    │  ...                                                 │
    ├─────────────────────────────────────────────────────┤
    │  NOTIFICATIONS                                       │
    │  [12:34:01] Task started                             │
    ├─────────────────────────────────────────────────────┤
    │  RESOURCES                                           │
    │  Docker: running  │  Disk: 142 MB used               │
    └─────────────────────────────────────────────────────┘

When in rate-limit-wait mode, the CLAUDE OUTPUT panel is replaced by a
prominent countdown panel.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timedelta
from typing import Deque, Optional

from rich.columns import Columns
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.style import Style
from rich.table import Table
from rich.text import Text

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

OUTPUT_BUFFER_SIZE = 20       # Lines retained in the output buffer
NOTIFICATION_BUFFER_SIZE = 50 # Notification entries retained

_STATE_STYLES: dict[str, str] = {
    "running":  "bold green",
    "waiting":  "bold yellow",
    "resuming": "bold cyan",
    "done":     "bold blue",
    "complete": "bold blue",
    "failed":   "bold red",
    "error":    "bold red",
    "starting": "bold white",
}


# ──────────────────────────────────────────────────────────────────────────────
# Helper: human-readable duration
# ──────────────────────────────────────────────────────────────────────────────

def _fmt_duration(seconds: float) -> str:
    """Return a human-readable duration string, e.g. '1h 23m 45s'."""
    if seconds < 0:
        return "—"
    td = timedelta(seconds=int(seconds))
    h, remainder = divmod(td.seconds + td.days * 86400, 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _fmt_tokens(estimated: int, threshold: int) -> str:
    """Format a token count as '~12,450 / 150k'."""
    def _abbrev(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n // 1_000}k"
        return str(n)

    pct = estimated / threshold if threshold else 0.0
    color = "red" if pct >= 0.90 else "yellow" if pct >= 0.70 else "green"
    return f"[{color}]~{estimated:,}[/] / {_abbrev(threshold)}"


# ──────────────────────────────────────────────────────────────────────────────
# TUIManager
# ──────────────────────────────────────────────────────────────────────────────

class TUIManager:
    """
    Rich-based live terminal UI for claude-runner.

    All public methods are thread-safe via a simple state-mutation approach —
    Rich's Live refreshes on a timer, reading the latest state each cycle.

    Usage::

        tui = TUIManager(task_name="My task", project_book_path="task.yaml",
                         timeout_hours=4.0)
        tui.start()
        try:
            tui.update_state("running")
            tui.add_output_line("Hello from Claude")
            tui.add_notification("Task started")
            ...
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
        self._task_name = task_name
        self._project_book_path = project_book_path
        self._timeout_hours = timeout_hours
        self._refresh_per_second = refresh_per_second

        # Mutable state updated by public methods
        self._state: str = "starting"
        self._start_time: float = time.monotonic()
        self._output_lines: Deque[str] = deque(maxlen=OUTPUT_BUFFER_SIZE)
        self._notifications: Deque[str] = deque(maxlen=NOTIFICATION_BUFFER_SIZE)

        # Context window
        self._token_estimated: int = 0
        self._token_threshold: int = 150_000
        self._checkpoints: int = 0
        self._rate_limit_waits: int = 0

        # Rate limit countdown
        self._rate_limit_remaining: float = 0.0
        self._rate_limit_end: Optional[float] = None  # monotonic

        # context_anchors indicator
        self._context_anchors_active: bool = False

        # Resources
        self._docker_status: str = "unknown"
        self._disk_usage_mb: float = 0.0

        # Rich internals
        self._console = Console()
        self._live: Optional[Live] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the Rich Live display."""
        self._start_time = time.monotonic()
        layout = self._build_layout()
        self._live = Live(
            layout,
            console=self._console,
            refresh_per_second=self._refresh_per_second,
            screen=False,
        )
        self._live.start(refresh=True)

    def stop(self) -> None:
        """Stop the Live display cleanly."""
        if self._live is not None:
            self._live.stop()
            self._live = None

    # ── State mutations ────────────────────────────────────────────────────────

    def update_state(self, state: str) -> None:
        """Update current state (running / waiting / resuming / done / failed)."""
        self._state = state.lower()
        self._refresh()

    def add_output_line(self, clean_line: str) -> None:
        """Add a line to the Claude output panel (keeps last 20 lines)."""
        self._output_lines.append(clean_line)
        self._refresh()

    def add_notification(self, message: str) -> None:
        """Add a notification log entry with timestamp."""
        ts = datetime.now().strftime("%H:%M:%S")
        self._notifications.append(f"[dim][{ts}][/dim] {message}")
        self._refresh()

    def update_tokens(
        self, estimated: int, threshold: int, checkpoints: int
    ) -> None:
        """Update the context window indicator."""
        self._token_estimated = estimated
        self._token_threshold = threshold
        self._checkpoints = checkpoints
        self._refresh()

    def update_rate_limit_countdown(self, remaining_seconds: float) -> None:
        """
        Show rate limit countdown.

        Pass 0 or a negative value to hide the countdown panel and restore the
        normal Claude output panel.
        """
        if remaining_seconds > 0:
            self._rate_limit_remaining = remaining_seconds
            self._rate_limit_end = time.monotonic() + remaining_seconds
        else:
            self._rate_limit_remaining = 0.0
            self._rate_limit_end = None
        self._refresh()

    def update_resources(self, docker_status: str, disk_usage_mb: float) -> None:
        """Update resource indicators."""
        self._docker_status = docker_status
        self._disk_usage_mb = disk_usage_mb
        self._refresh()

    def update_rate_limit_waits(self, count: int) -> None:
        """Update the rate limit wait counter shown in the context panel."""
        self._rate_limit_waits = count
        self._refresh()

    def set_context_anchors_active(self, active: bool) -> None:
        """Show or hide the [context anchors: active] indicator in the status panel."""
        self._context_anchors_active = active
        self._refresh()

    def print_message(self, message: str, style: str = "") -> None:
        """
        Print a message outside the live display.

        Useful for startup / shutdown banners that should persist in the scroll
        buffer after the Live display is stopped.
        """
        if self._live is not None:
            # Temporarily pause the live display so the print is not overwritten
            with self._live:
                self._console.print(message, style=style)
        else:
            self._console.print(message, style=style)

    # ── Internal rendering ─────────────────────────────────────────────────────

    def _refresh(self) -> None:
        """Rebuild the layout and push it to the Live display."""
        if self._live is not None:
            self._live.update(self._build_layout())

    def _build_layout(self) -> Panel:
        """
        Build the complete renderable for the current state.

        Returns a single Panel that wraps the entire UI so it renders inside a
        clean outer border with the tool name in the title.
        """
        elapsed = time.monotonic() - self._start_time

        # Recalculate live countdown from end timestamp (avoids drift from
        # only updating on explicit calls)
        if self._rate_limit_end is not None:
            remaining = self._rate_limit_end - time.monotonic()
            if remaining < 0:
                remaining = 0.0
                self._rate_limit_end = None
        else:
            remaining = 0.0

        in_rate_limit = remaining > 0

        rows: list = [
            self._render_header(),
            self._render_status_context_row(elapsed),
            self._render_output_or_countdown(in_rate_limit, remaining),
            self._render_notifications(),
            self._render_resources(),
        ]

        return Panel(
            Group(*rows),
            title="[bold cyan]claude-runner[/bold cyan]",
            border_style="bright_black",
            padding=(0, 1),
        )

    # ── Section renderers ──────────────────────────────────────────────────────

    def _render_header(self) -> Panel:
        """Header bar: task name and project book path."""
        # Truncate long paths so the header stays on one line
        max_path_len = 60
        path = self._project_book_path
        if len(path) > max_path_len:
            path = "…" + path[-(max_path_len - 1):]

        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold white")
        t.add_column(style="dim")
        t.add_row(
            f"task: [bold cyan]{self._task_name}[/bold cyan]",
            f"project: [italic]{path}[/italic]",
        )
        return Panel(t, style="on grey7", padding=(0, 1), expand=True)

    def _render_status_context_row(self, elapsed: float) -> Columns:
        """Two-column row: STATUS panel on the left, CONTEXT panel on the right."""
        return Columns(
            [
                self._render_status_panel(elapsed),
                self._render_context_panel(),
            ],
            equal=True,
            expand=True,
        )

    def _render_status_panel(self, elapsed: float) -> Panel:
        """Status panel: state, elapsed, estimated remaining."""
        state_style = _STATE_STYLES.get(self._state, "white")
        state_label = self._state.upper()

        # Spinner for active states
        if self._state in ("running", "resuming", "starting"):
            spinner = Spinner("dots", style=state_style)
            state_text: object = Text.assemble(
                ("  ", ""),  # placeholder for spinner position
                (f" {state_label}", state_style),
            )
            # We compose a simple grid: spinner | label
            grid = Table.grid()
            grid.add_column(width=2)
            grid.add_column()
            grid.add_row(spinner, Text(f" {state_label}", style=state_style))
            state_cell: object = grid
        else:
            state_cell = Text(state_label, style=state_style)

        timeout_total = self._timeout_hours * 3600
        if elapsed < timeout_total:
            remaining_est = timeout_total - elapsed
            eta_str = _fmt_duration(remaining_est)
        else:
            eta_str = "[red]exceeded[/red]"

        t = Table.grid(padding=(0, 1))
        t.add_column(style="dim", width=12)
        t.add_column()
        t.add_row("State", state_cell)
        t.add_row("Elapsed", Text(_fmt_duration(elapsed), style="white"))
        t.add_row("Est. left", Text(eta_str))
        t.add_row("Timeout", Text(f"{self._timeout_hours:.1f}h", style="dim"))
        if self._context_anchors_active:
            t.add_row("", Text("[context anchors: active]", style="dim cyan"))

        return Panel(t, title="[bold]STATUS[/bold]", border_style="bright_black")

    def _render_context_panel(self) -> Panel:
        """Context panel: token usage, checkpoints, rate limit waits."""
        token_str = _fmt_tokens(self._token_estimated, self._token_threshold)

        # Progress bar for token consumption
        pct = min(1.0, self._token_estimated / self._token_threshold) if self._token_threshold else 0.0
        bar_width = 20
        filled = int(pct * bar_width)
        bar_color = "red" if pct >= 0.90 else "yellow" if pct >= 0.70 else "green"
        bar = f"[{bar_color}]{'█' * filled}[/][dim]{'░' * (bar_width - filled)}[/]"

        t = Table.grid(padding=(0, 1))
        t.add_column(style="dim", width=18)
        t.add_column()
        t.add_row("Tokens", Text.from_markup(token_str))
        t.add_row("", Text.from_markup(bar))
        t.add_row("Checkpoints", Text(str(self._checkpoints), style="cyan"))
        t.add_row(
            "Rate limit waits",
            Text(str(self._rate_limit_waits), style="yellow" if self._rate_limit_waits else "dim"),
        )

        return Panel(t, title="[bold]CONTEXT[/bold]", border_style="bright_black")

    def _render_output_or_countdown(
        self, in_rate_limit: bool, remaining: float
    ) -> Panel:
        """
        Render either the Claude output panel or the rate limit countdown panel,
        depending on whether we are currently in a rate limit wait.
        """
        if in_rate_limit:
            return self._render_countdown_panel(remaining)
        return self._render_output_panel()

    def _render_output_panel(self) -> Panel:
        """CLAUDE OUTPUT panel: last N lines of Claude Code's output."""
        if not self._output_lines:
            content: object = Text("(no output yet)", style="dim italic")
        else:
            lines_text = Text()
            for i, line in enumerate(self._output_lines):
                if i:
                    lines_text.append("\n")
                # Dim older lines slightly
                age_pct = i / max(len(self._output_lines) - 1, 1)
                if age_pct < 0.3:
                    lines_text.append(line, style="dim")
                else:
                    lines_text.append(line, style="white")
            content = lines_text

        return Panel(
            content,
            title=f"[bold]CLAUDE OUTPUT[/bold] [dim](last {OUTPUT_BUFFER_SIZE} lines)[/dim]",
            border_style="bright_black",
            expand=True,
        )

    def _render_countdown_panel(self, remaining: float) -> Panel:
        """Rate limit countdown panel — replaces output panel during waits."""
        mins, secs = divmod(int(remaining), 60)
        hours, mins = divmod(mins, 60)

        if hours:
            countdown = f"{hours}:{mins:02d}:{secs:02d}"
        else:
            countdown = f"{mins:02d}:{secs:02d}"

        # Big, prominent countdown
        grid = Table.grid(expand=True)
        grid.add_column(justify="center")
        grid.add_row(Spinner("clock", style="yellow"))
        grid.add_row(Text("RATE LIMIT — waiting for reset", style="bold yellow", justify="center"))
        grid.add_row(Text(""))
        grid.add_row(Text(countdown, style="bold yellow", justify="center"))
        grid.add_row(Text(""))
        grid.add_row(Text("claude-runner will resume automatically", style="dim italic", justify="center"))

        return Panel(
            grid,
            title="[bold yellow]RATE LIMIT WAIT[/bold yellow]",
            border_style="yellow",
            expand=True,
        )

    def _render_notifications(self) -> Panel:
        """NOTIFICATIONS panel: timestamped log of events."""
        if not self._notifications:
            content: object = Text("(no notifications yet)", style="dim italic")
        else:
            lines_text = Text()
            for i, entry in enumerate(self._notifications):
                if i:
                    lines_text.append("\n")
                lines_text.append_text(Text.from_markup(entry))
            content = lines_text

        return Panel(
            content,
            title="[bold]NOTIFICATIONS[/bold]",
            border_style="bright_black",
            expand=True,
        )

    def _render_resources(self) -> Panel:
        """RESOURCES panel: Docker container status, disk usage."""
        docker_style = {
            "running": "bold green",
            "stopped": "bold red",
            "starting": "yellow",
            "unknown": "dim",
            "n/a": "dim",
            "native": "cyan",
        }.get(self._docker_status.lower(), "white")

        disk_str: str
        if self._disk_usage_mb < 1024:
            disk_str = f"{self._disk_usage_mb:.1f} MB"
        else:
            disk_str = f"{self._disk_usage_mb / 1024:.2f} GB"

        grid = Table.grid(padding=(0, 3))
        grid.add_column(style="dim")
        grid.add_column()
        grid.add_column(style="dim")
        grid.add_column()
        grid.add_row(
            "Docker",
            Text(self._docker_status, style=docker_style),
            "Disk (workdir)",
            Text(f"{disk_str} used", style="white"),
        )

        return Panel(
            grid,
            title="[bold]RESOURCES[/bold]",
            border_style="bright_black",
            expand=True,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Standalone smoke-test
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    tui = TUIManager(
        task_name="Example task",
        project_book_path="projects/example_simple.yaml",
        timeout_hours=2.0,
    )
    tui.start()

    import time as _time

    tui.update_state("running")
    tui.update_resources("running", 142.3)
    tui.add_notification("Task started")

    for i in range(5):
        _time.sleep(0.5)
        tui.add_output_line(f"[dim cyan]>[/dim cyan] Processing step {i + 1}...")
        tui.update_tokens(estimated=i * 3000, threshold=150_000, checkpoints=0)

    tui.add_notification("Rate limit hit — waiting 30 seconds")
    tui.update_state("waiting")
    tui.update_rate_limit_countdown(30)

    _time.sleep(5)
    tui.update_rate_limit_countdown(0)
    tui.update_state("resuming")
    tui.add_notification("Resuming after rate limit wait")

    _time.sleep(1)
    tui.update_state("running")
    tui.add_output_line("Continuing from where we left off...")

    _time.sleep(2)
    tui.update_state("done")
    tui.add_notification("Task complete")

    _time.sleep(2)
    tui.stop()
    print("TUI stopped.")
    sys.exit(0)
