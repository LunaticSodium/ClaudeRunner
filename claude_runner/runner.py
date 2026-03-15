"""
runner.py — Core orchestration loop for claude-runner.

TaskRunner coordinates the full lifecycle of a Claude Code task:

  1.  Sandbox setup/teardown (DockerSandbox or NativeSandbox via sandbox factory)
  2.  Claude Code process launch
  3.  Real-time output monitoring (token counting, rate-limit detection, TUI updates)
  4.  Rate-limit wait/resume cycle with configurable ceiling
  5.  Context checkpoint injection via ContextManager
  6.  Notification dispatch (start / rate_limit / resume / complete / error)
  7.  State persistence (30-second heartbeat + event-triggered saves)
  8.  Output collection (git diff --stat or filesystem snapshot diff)
  9.  Full report write-out to log_dir on completion or error

Design principles (from spec section 9):
  - Log every action taken (sending prompts, dispatching notifications, …).
  - Fail loudly with actionable errors; never silently degrade.
  - max_rate_limit_waits is a hard ceiling — exceeding it raises RateLimitError.

Interaction with other modules
------------------------------
  sandbox/     — provides setup(), launch_claude(), teardown() interface
  process.py   — ConPTY subprocess; sandbox delegates to it internally
  rate_limit.py — RateLimitDetector (per-line callback), RateLimitWaiter
  context_manager.py — ContextManager
  notify.py    — NotificationManager
  persistence.py — PersistenceManager
  tui.py       — TUI callback type / live display
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from .context_manager import (
    CHECKPOINT_PROMPT,
    STRATEGY_CONTINUE,
    STRATEGY_RESTATE,
    STRATEGY_SUMMARIZE,
    ContextManager,
)

if TYPE_CHECKING:
    # Avoid circular imports at runtime; use string annotations where needed.
    from .config import Config
    from .notify import NotificationManager
    from .persistence import PersistenceManager
    from .project import ProjectBook
    from .rate_limit import RateLimitDetector, RateLimitWaiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How often (in seconds) the orchestration loop checkpoints state to disk,
# independently of event-triggered saves.
_STATE_CHECKPOINT_INTERVAL_S: float = 30.0

# How often (in seconds) the main loop polls the process for new output when
# using the polling fallback (non-PTY modes).
_POLL_INTERVAL_S: float = 0.05

# Default silence window before the watchdog sends a probe (seconds).
# Overridable per-task via execution.silence_timeout_minutes in the project book.
_SILENCE_TIMEOUT_S: float = 300.0  # 5 minutes

# Directory name created inside the working directory for claude-runner artefacts.
_RUNNER_DIR = ".claude-runner"

# Progress log filename (relative to _RUNNER_DIR inside working directory).
_PROGRESS_LOG_NAME = "progress.log"

# Progress log header written at task start.
_PROGRESS_LOG_HEADER_TEMPLATE = (
    "# claude-runner progress log\n"
    "# Task:    {task_name}\n"
    "# Started: {start_time}\n"
    "#\n"
    "# Format:\n"
    "#   [TIMESTAMP] [PHASE]    Description of current state\n"
    "#   [TIMESTAMP] [DONE]     Description of completed step\n"
    "#   [TIMESTAMP] [BLOCK]    Description of a blocker or open question\n"
    "#   [TIMESTAMP] [DECISION] Rationale for a choice made\n"
    "#\n"
)

# Prefix prepended to the initial user prompt asking Claude to maintain the log.
_PROGRESS_LOG_INSTRUCTION = textwrap.dedent(
    """\
    IMPORTANT INSTRUCTIONS FOR THIS SESSION
    ========================================
    You are operating autonomously inside a claude-runner session.  You have no
    interactive human supervisor.

    MANDATORY: maintain a structured progress log at the following path:

      .claude-runner/progress.log

    This file already exists (claude-runner created it).  Append to it using
    this format:

      [TIMESTAMP] [PHASE]    Description of current state
      [TIMESTAMP] [DONE]     Description of completed step
      [TIMESTAMP] [BLOCK]    Description of a blocker or open question
      [TIMESTAMP] [DECISION] Rationale for a choice made

    Write to the progress log:
      - At task start (summarise what you are about to do).
      - After each significant milestone (file written, test run, decision made).
      - Before any long-running operation (so the log is current if you are interrupted).
      - When you encounter a blocker.
      - At task completion (final summary of everything accomplished).

    The progress log is the authoritative external record of this session.
    It will be read by claude-runner to recover context if execution is interrupted.

    ========================================
    TASK
    ========================================
    """
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RateLimitError(RuntimeError):
    """Raised when max_rate_limit_waits is exceeded."""


class TaskTimeoutError(RuntimeError):
    """Raised when the overall task timeout is exceeded."""


class SandboxError(RuntimeError):
    """Raised on unrecoverable sandbox failures."""


# ---------------------------------------------------------------------------
# TaskResult
# ---------------------------------------------------------------------------


@dataclass
class TaskResult:
    """
    Immutable summary of a completed (or failed) task run.

    Returned by TaskRunner.run() in all terminal states.
    """

    task_name: str
    status: str  # "complete" | "failed" | "aborted"
    start_time: datetime
    end_time: datetime
    rate_limit_cycles: int
    checkpoint_count: int
    change_summary: str   # git diff --stat or filesystem snapshot diff
    progress_log: str     # contents of .claude-runner/progress.log at end of run
    fault_log: list[str] = field(default_factory=list)
    error_message: Optional[str] = None

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def duration(self) -> timedelta:
        return self.end_time - self.start_time

    @property
    def duration_str(self) -> str:
        total = int(self.duration.total_seconds())
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h {m:02d}m {s:02d}s"

    def __str__(self) -> str:  # pragma: no cover
        return (
            f"TaskResult(task={self.task_name!r}, status={self.status!r}, "
            f"duration={self.duration_str}, rate_limit_cycles={self.rate_limit_cycles}, "
            f"checkpoints={self.checkpoint_count})"
        )


# ---------------------------------------------------------------------------
# TaskRunner
# ---------------------------------------------------------------------------


class TaskRunner:
    """
    Orchestrates the full lifecycle of a Claude Code task.

    Parameters
    ----------
    project_book:
        Parsed and validated ProjectBook instance.
    config:
        Global claude-runner Config instance.
    api_key:
        Anthropic API key (resolved by main.py via the priority chain in spec §4.6).
    tui_callback:
        Optional callable invoked with (event_type: str, data: dict) for every
        observable event (new output line, state change, countdown tick, …).
        The TUI module registers this callback to update its live display.
        If None, TUI updates are silently skipped.
    sandbox:
        Optional pre-constructed sandbox object.  If provided, sandbox creation
        in _initialise() is skipped and this instance is used directly.
    tui:
        Optional TUI manager object.  Exposes add_output_line(), update_state()
        etc.  Takes precedence over tui_callback when both are provided.
    resume:
        If True, attempt to resume a previous session rather than starting fresh.
    """

    def __init__(
        self,
        project_book: "ProjectBook",
        config: "Config",
        api_key: str,
        tui_callback: Optional[Callable[[str, dict], None]] = None,
        sandbox=None,
        tui=None,
        resume: bool = False,
        project_book_path: Optional[str] = None,
    ) -> None:
        self._book = project_book
        self._config = config
        self._api_key = api_key
        self._tui_callback = tui_callback
        self._tui = tui
        self._resume = resume
        self._book_path: Optional[Path] = Path(project_book_path) if project_book_path else None
        # Unique filesystem identifier derived from the YAML filename stem.
        # Keying off the filename (not book.name) prevents collisions when two
        # project books share the same name: field but live in the same folder.
        self._project_id: str = (
            _safe_name(self._book_path.stem)
            if self._book_path is not None
            else _safe_name(self._book.name)
        )
        self._secrets_config = self._load_secrets_config()

        # Resolved lazily once run() begins.
        self._sandbox = sandbox  # may be pre-provided; if None, created in _initialise()
        self._notifier: Optional["NotificationManager"] = None
        self._persistence: Optional["PersistenceManager"] = None
        self._rate_detector: Optional["RateLimitDetector"] = None
        self._rate_waiter: Optional["RateLimitWaiter"] = None
        self._context_manager: Optional[ContextManager] = None

        # Runtime state
        self._start_time: Optional[datetime] = None
        self._rate_limit_cycles: int = 0
        self._fault_log: list[str] = []
        self._process = None           # the live ClaudeProcess (or equivalent)
        self._output_lines: list[str] = []  # all stripped output collected

        # asyncio.Event used to signal a detected rate-limit from the detector callback.
        self._rate_limit_event: Optional[asyncio.Event] = None
        self._rate_limit_reset_time: Optional[datetime] = None

        # asyncio.Events for runner protocol markers (##RUNNER:COMPLETE## / ##RUNNER:ERROR##).
        # Initialised in _initialise() once an event loop is available (NOT here).
        self._runner_complete_event: Optional[asyncio.Event] = None
        self._runner_error_event: Optional[asyncio.Event] = None
        self._runner_error_message: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Silence watchdog: tracks last output time and the background task.
        self._last_output_time: float = time.monotonic()
        self._silence_watchdog_task: Optional[asyncio.Task] = None

        # Filesystem snapshot taken at task start (fallback when git unavailable).
        self._fs_snapshot_start: dict[str, tuple[int, float]] = {}  # path → (size, mtime)

        # Compiled milestone patterns: list of (re.Pattern, message_str).
        # Built in _initialise(); kept separate from the book to avoid re-compiling.
        self._milestone_patterns: list[tuple[re.Pattern, str]] = []
        # Set of milestone messages already fired this session (prevents repeat fires).
        self._milestones_fired: set[str] = set()

        # Set by _state_checkpoint_loop to allow cancellation.
        self._checkpoint_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Secrets config loader
    # ------------------------------------------------------------------

    @staticmethod
    def _load_secrets_config():
        """
        Load ~/.claude-runner/secrets.yaml as a SimpleNamespace.

        Returns a SimpleNamespace on success, or None if the file does not exist
        or cannot be parsed.
        """
        import types  # noqa: PLC0415

        secrets_path = Path.home() / ".claude-runner" / "secrets.yaml"
        if not secrets_path.exists():
            return None
        try:
            import yaml  # noqa: PLC0415
            raw = secrets_path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw) or {}
            if not isinstance(data, dict):
                logger.warning("secrets.yaml did not parse as a dict — ignoring.")
                return None

            def _to_ns(d):
                if isinstance(d, dict):
                    return types.SimpleNamespace(**{k: _to_ns(v) for k, v in d.items()})
                return d

            return _to_ns(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load secrets.yaml: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Fault handler
    # ------------------------------------------------------------------

    def _on_fault(self, message: str) -> None:
        """Called by NotificationManager when the email guard fires."""
        self._fault_log.append(message)
        if self._persistence:
            self._persistence.append_fault(message)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> TaskResult:
        """
        Execute the task end-to-end.  Returns a TaskResult in all cases
        (complete, failed, aborted).  Re-raises only truly unexpected
        exceptions that indicate programming errors.

        Flow
        ----
        1.  Resolve helper objects (sandbox, notifier, persistence, …).
        2.  Create progress.log file in working directory.
        3.  Save state file with phase="running".
        4.  Dispatch "start" notification.
        5.  Launch Claude Code subprocess with initial prompt.
        6.  Enter main monitoring loop.
        7.  On rate-limit: checkpoint → wait → resume.
        8.  On completion: collect diff, write report, clean up, return.
        9.  On error/timeout: dispatch notification, persist failure state, return.
        """
        self._start_time = datetime.now(tz=timezone.utc)
        start_str = _fmt_time(self._start_time)
        logger.info("TaskRunner.run() starting: task=%r at %s", self._book.name, start_str)

        try:
            await self._initialise()
        except Exception as exc:
            msg = f"Initialisation failed: {exc}"
            logger.exception(msg)
            return self._make_result("failed", error_message=msg)

        try:
            result = await self._run_inner()
        except Exception as exc:
            # Catch-all: log, dispatch error notification, persist, return failed result.
            msg = f"Unexpected error in run loop: {exc}"
            logger.exception(msg)
            self._fault_log.append(f"[FATAL] {msg}")
            await self._dispatch("error", {"error": msg, "task": self._book.name})
            result = self._make_result("failed", error_message=msg)
        finally:
            await self._cleanup()

        return result

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    async def _initialise(self) -> None:
        """Set up all helper objects and the sandbox."""
        # Deferred imports to avoid circular dependencies at module load time.
        from .notify import NotificationManager  # noqa: PLC0415
        from .persistence import PersistenceManager, TaskState  # noqa: PLC0415
        from .rate_limit import RateLimitDetector  # noqa: PLC0415
        from .sandbox import create_sandbox  # noqa: PLC0415

        # --- Sandbox -------------------------------------------------------
        if self._sandbox is None:
            logger.info("[ACTION] Creating sandbox (backend=%r)", getattr(self._config, "sandbox_backend", "auto"))
            self._sandbox = create_sandbox(
                self._book, self._config, self._api_key, book_path=self._book_path
            )
        else:
            logger.info("[ACTION] Using pre-provided sandbox.")
        await _maybe_await(self._sandbox.setup)
        logger.info("[ACTION] Sandbox ready.")

        # --- Progress log --------------------------------------------------
        self._init_progress_log()

        # --- Filesystem snapshot (git fallback) ----------------------------
        self._fs_snapshot_start = self._take_fs_snapshot()

        # --- ContextManager ------------------------------------------------
        ctx_cfg = getattr(getattr(self._book, "execution", None), "context", None)
        threshold = getattr(ctx_cfg, "checkpoint_threshold_tokens", 150_000)
        reset_on_rl = getattr(ctx_cfg, "reset_on_rate_limit", True)
        inject_log = getattr(ctx_cfg, "inject_log_on_resume", True)

        context_anchors = getattr(self._book, "context_anchors", None) or None
        if context_anchors:
            logger.info("[ACTION] context_anchors are active for this task (content not logged).")

        progress_log_path = self._progress_log_path()
        self._context_manager = ContextManager(
            threshold_tokens=threshold,
            reset_on_rate_limit=reset_on_rl,
            inject_log_on_resume=inject_log,
            progress_log_path=progress_log_path,
            context_anchors=context_anchors,
            # on_inject_checkpoint wired in after process is launched.
        )
        logger.info(
            "ContextManager initialised: threshold=%d tokens, reset_on_rate_limit=%s, inject_log_on_resume=%s",
            threshold,
            reset_on_rl,
            inject_log,
        )

        # --- Milestone patterns --------------------------------------------
        raw_milestones = getattr(getattr(self._book, "execution", None), "milestones", []) or []
        for ms in raw_milestones:
            pat_str = getattr(ms, "pattern", "")
            msg_str = getattr(ms, "message", pat_str)
            if pat_str:
                try:
                    self._milestone_patterns.append((re.compile(pat_str), msg_str))
                    logger.debug("Milestone pattern compiled: %r → %r", pat_str, msg_str)
                except re.error as exc:
                    logger.warning("Invalid milestone pattern %r: %s — skipping.", pat_str, exc)
        if self._milestone_patterns:
            logger.info(
                "[ACTION] %d milestone pattern(s) active.", len(self._milestone_patterns)
            )

        # --- Rate limit helpers --------------------------------------------
        # The detector fires _on_rate_limit_detected via callback; no queue needed.
        self._rate_limit_event = asyncio.Event()
        self._rate_limit_reset_time = None

        # Runner protocol marker events — created here (inside the event loop) so
        # that call_soon_threadsafe() is safe to use from I/O threads.
        self._runner_complete_event = asyncio.Event()
        self._runner_error_event = asyncio.Event()

        self._loop = asyncio.get_event_loop()

        def _on_rate_limit_detected(reset_at: datetime) -> None:
            self._rate_limit_reset_time = reset_at
            self._loop.call_soon_threadsafe(self._rate_limit_event.set)

        self._rate_detector = RateLimitDetector(on_rate_limit=_on_rate_limit_detected)

        # --- Notifications -------------------------------------------------
        self._notifier = NotificationManager(
            notify_config=getattr(self._book, "notify", None),
            task_name=self._book.name,
            secrets_config=self._secrets_config,
            on_fault=self._on_fault,
        )

        # --- Persistence ---------------------------------------------------
        state_dir = Path.home() / ".claude-runner" / "state"
        self._persistence = PersistenceManager(state_dir=state_dir, task_name=self._project_id)
        self._persistence.save(self._make_state("running", rate_limit_wait_count=0))
        logger.info("[ACTION] State file created (phase=running).")

    # ------------------------------------------------------------------
    # Main run inner loop
    # ------------------------------------------------------------------

    async def _run_inner(self) -> TaskResult:
        """
        Inner orchestration loop.  Called after _initialise() succeeds.
        Returns a TaskResult on any terminal condition.
        """
        # Start background state-checkpoint heartbeat.
        self._checkpoint_task = asyncio.create_task(self._state_checkpoint_loop())

        # Start silence watchdog (detects hung process / missed rate limits).
        exec_cfg = getattr(self._book, "execution", None)
        _silence_min = getattr(exec_cfg, "silence_timeout_minutes", None)
        silence_timeout_s = float(_silence_min * 60) if _silence_min is not None else _SILENCE_TIMEOUT_S
        self._last_output_time = time.monotonic()
        self._silence_watchdog_task = asyncio.create_task(
            self._silence_watchdog(silence_timeout_s)
        )

        # --- Build initial prompt -----------------------------------------
        initial_prompt = self._build_initial_prompt()
        # Store only the decorated portion (RUNNER_PROTOCOL + anchors + task prompt,
        # without _PROGRESS_LOG_INSTRUCTION) so the 'restate' resume strategy
        # does not re-send the one-time log maintenance instruction.
        decorated = initial_prompt[len(_PROGRESS_LOG_INSTRUCTION):]
        self._context_manager.set_original_prompt(decorated)
        self._context_manager.count_input(initial_prompt)

        # --- Notify TUI about context_anchors status ----------------------
        if self._context_manager and self._context_manager.context_anchors_active:
            self._tui_update("context_anchors_active", {"active": True})

        # --- Dispatch start notification ----------------------------------
        logger.info("[ACTION] Dispatching 'start' notification.")
        await self._dispatch("start", {"task": self._book.name})

        # --- Launch Claude Code -------------------------------------------
        logger.info("[ACTION] Launching Claude Code subprocess.")
        self._process = await _maybe_await(
            self._sandbox.launch_claude,
            prompt=initial_prompt,
            on_line=self._on_output_line,
            on_exit=None,  # exit handled via process.wait() below
        )

        # Wire checkpoint callback now that the process handle is available.
        self._context_manager.set_on_inject_checkpoint(self._send_to_process)
        logger.info("Claude Code process launched; on_inject_checkpoint wired.")

        # --- Determine timeout -------------------------------------------
        exec_cfg = getattr(self._book, "execution", None)
        timeout_hours = getattr(exec_cfg, "timeout_hours", 24)
        max_rl_waits = getattr(exec_cfg, "max_rate_limit_waits", 20)
        resume_strategy = getattr(exec_cfg, "resume_strategy", STRATEGY_CONTINUE)
        deadline = self._start_time + timedelta(hours=timeout_hours)

        logger.info(
            "Task parameters: timeout=%dh, max_rate_limit_waits=%d, resume_strategy=%r",
            timeout_hours,
            max_rl_waits,
            resume_strategy,
        )

        # --- Main monitoring loop ----------------------------------------
        #
        # The process drives its own async output via on_line callbacks.
        # Here we wait for the process to exit, handling rate-limit events
        # in between.  Rate-limit events are signalled via asyncio.Event set
        # by the RateLimitDetector callback inside _initialise().
        #

        while True:
            # Check overall deadline.
            now = datetime.now(tz=timezone.utc)
            if now >= deadline:
                msg = (
                    f"Task timeout exceeded: {timeout_hours}h limit reached "
                    f"(started {_fmt_time(self._start_time)})."
                )
                logger.error(msg)
                self._fault_log.append(f"[TIMEOUT] {msg}")
                await self._dispatch("error", {"error": msg, "task": self._book.name})
                self._checkpoint_state()
                return self._make_result("failed", error_message=msg)

            # Wait for: process exit, rate-limit, or runner protocol markers,
            # whichever comes first.  We poll with a short timeout so
            # we can service the deadline check.
            remaining_s = (deadline - now).total_seconds()
            done_task = asyncio.ensure_future(self._process.wait())
            rl_task = asyncio.ensure_future(self._rate_limit_event.wait())
            rc_task = asyncio.ensure_future(self._runner_complete_event.wait())
            re_task = asyncio.ensure_future(self._runner_error_event.wait())

            try:
                finished, pending = await asyncio.wait(
                    {done_task, rl_task, rc_task, re_task},
                    timeout=min(remaining_s, _STATE_CHECKPOINT_INTERVAL_S),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except Exception:
                for t in (done_task, rl_task, rc_task, re_task):
                    t.cancel()
                raise

            # Cancel whichever futures are still pending.
            for t in (done_task, rl_task, rc_task, re_task):
                if not t.done():
                    t.cancel()

            # --- Runner protocol markers (highest priority) ----------------
            if rc_task in finished:
                logger.info("##RUNNER:COMPLETE## — treating as clean task completion.")
                return await self._handle_completion()

            if re_task in finished:
                msg = f"Claude reported fatal error: {self._runner_error_message or '(no description)'}"
                logger.error(msg)
                self._fault_log.append(f"[ERROR] {msg}")
                await self._dispatch("error", {"error": msg, "task": self._book.name})
                return self._make_result("failed", error_message=msg)

            if done_task in finished:
                # Process has exited.
                exit_code = done_task.result()
                if exit_code == 0:
                    logger.info("Claude Code exited cleanly (exit code 0).")
                    return await self._handle_completion()
                else:
                    msg = f"Claude Code exited with non-zero exit code {exit_code}."
                    logger.error(msg)
                    self._fault_log.append(f"[ERROR] {msg}")
                    await self._dispatch("error", {"error": msg, "task": self._book.name})
                    return self._make_result("failed", error_message=msg)

            elif rl_task in finished:
                # Rate-limit event detected (not a runner marker).
                reset_time: datetime = self._rate_limit_reset_time or datetime.now(tz=timezone.utc)
                # Clear the event so it can fire again in subsequent cycles.
                self._rate_limit_event.clear()
                logger.info(
                    "Rate limit detected.  Reset time: %s.  Cycle %d of %d.",
                    _fmt_time(reset_time),
                    self._rate_limit_cycles + 1,
                    max_rl_waits,
                )
                result = await self._handle_rate_limit(
                    reset_time=reset_time,
                    max_rl_waits=max_rl_waits,
                    resume_strategy=resume_strategy,
                )
                if result is not None:
                    # Max waits exceeded or unrecoverable.
                    return result
                # Resume was successful; loop back to monitoring.

            else:
                # Timeout on wait — loop back to recheck deadline and state.
                logger.debug("Monitor poll timeout; rechecking deadline.")
                self._checkpoint_state()

    # ------------------------------------------------------------------
    # Rate-limit handling
    # ------------------------------------------------------------------

    async def _handle_rate_limit(
        self,
        reset_time: datetime,
        max_rl_waits: int,
        resume_strategy: str,
    ) -> Optional[TaskResult]:
        """
        Handle a single rate-limit cycle.

        Returns None if the cycle completed successfully (caller should continue
        the monitoring loop), or a TaskResult if max_rl_waits was exceeded.
        """
        self._rate_limit_cycles += 1

        # --- Optional checkpoint before the wait -------------------------
        if self._context_manager.reset_on_rate_limit:
            logger.info("[ACTION] reset_on_rate_limit=True: injecting checkpoint before rate-limit wait.")
            try:
                self._context_manager.inject_checkpoint()
            except Exception as exc:
                # Log but don't abort — the checkpoint is a best-effort artefact.
                warn = f"Checkpoint injection failed before rate-limit wait: {exc}"
                logger.warning(warn)
                self._fault_log.append(f"[WARN] {warn}")

        # --- Dispatch rate_limit notification (desktop + webhook only) ---
        wait_until_str = _fmt_time(reset_time)
        logger.info("[ACTION] Dispatching 'rate_limit' notification (reset at %s).", wait_until_str)
        await self._dispatch(
            "rate_limit",
            {"task": self._book.name, "reset_time": wait_until_str, "cycle": self._rate_limit_cycles},
        )

        # --- Update state -------------------------------------------------
        self._persistence.save(self._make_state("waiting", rate_limit_wait_count=self._rate_limit_cycles))
        logger.info("[ACTION] State updated: phase=waiting, rate_limit_wait_count=%d.", self._rate_limit_cycles)

        # --- Check ceiling -----------------------------------------------
        if self._rate_limit_cycles > max_rl_waits:
            msg = (
                f"max_rate_limit_waits={max_rl_waits} exceeded "
                f"(this is cycle {self._rate_limit_cycles}).  Aborting task."
            )
            logger.error(msg)
            self._fault_log.append(f"[ABORT] {msg}")
            await self._dispatch("error", {"error": msg, "task": self._book.name})
            self._persistence.save(self._make_state("failed"))
            return self._make_result("failed", error_message=msg)

        # --- Wait with TUI countdown -------------------------------------
        from .rate_limit import RateLimitWaiter  # noqa: PLC0415
        logger.info("[ACTION] Waiting for rate limit to reset at %s.", wait_until_str)
        waiter = RateLimitWaiter(
            reset_at=reset_time,
            on_tick=self._on_countdown_tick,
            on_resume=lambda: None,  # resume is handled after wait returns
        )
        await waiter.wait()
        logger.info("Rate limit wait complete.  Resuming task.")

        # --- Dispatch resume notification --------------------------------
        logger.info("[ACTION] Dispatching 'resume' notification.")
        await self._dispatch(
            "resume",
            {"task": self._book.name, "cycle": self._rate_limit_cycles},
        )

        # --- Build and send resume prompt --------------------------------
        resume_prompt = self._context_manager.build_resume_prompt(
            base_resume="continue",
            strategy=resume_strategy,
        )
        logger.info(
            "[ACTION] Sending resume prompt to Claude Code (strategy=%r, length=%d chars).",
            resume_strategy,
            len(resume_prompt),
        )
        self._send_to_process(resume_prompt)
        self._context_manager.count_input(resume_prompt)

        # --- Update state to running -------------------------------------
        self._persistence.save(self._make_state("running"))
        logger.info("[ACTION] State updated: phase=running.")

        return None  # Success — caller continues the monitoring loop.

    # ------------------------------------------------------------------
    # Completion handler
    # ------------------------------------------------------------------

    async def _handle_completion(self) -> TaskResult:
        """Called when the Claude Code process exits with code 0."""
        end_time = datetime.now(tz=timezone.utc)
        logger.info("Task completed successfully at %s.", _fmt_time(end_time))

        # --- Collect change summary --------------------------------------
        change_summary = self._collect_output_diff()

        # --- Dispatch complete notification (all channels + diff) --------
        logger.info("[ACTION] Dispatching 'complete' notification.")
        await self._dispatch(
            "complete",
            {
                "task": self._book.name,
                "change_summary": change_summary,
                "duration": str(end_time - self._start_time),
                "rate_limit_cycles": self._rate_limit_cycles,
            },
        )

        # --- Git workflow (branch, commit, optional push/PR) -------------
        git_summary = self._run_git_workflow()
        if git_summary:
            logger.info("[ACTION] Git workflow complete:\n%s", git_summary)
            # Append git summary to change_summary for the report/email.
            change_summary = change_summary + "\n\n--- git workflow ---\n" + git_summary if change_summary else git_summary

        # --- Read final progress log -------------------------------------
        progress_log_text = self._context_manager.read_progress_log()

        # --- Copy progress.log to host log_dir ---------------------------
        self._save_progress_log_to_host(progress_log_text)

        # --- Build result ------------------------------------------------
        result = TaskResult(
            task_name=self._book.name,
            status="complete",
            start_time=self._start_time,
            end_time=end_time,
            rate_limit_cycles=self._rate_limit_cycles,
            checkpoint_count=self._context_manager.checkpoint_count,
            change_summary=change_summary,
            progress_log=progress_log_text,
            fault_log=list(self._fault_log),
            error_message=None,
        )

        # --- Write full report -------------------------------------------
        self._write_full_report(result)

        # --- Clean up state file (clean completion) ----------------------
        self._persistence.delete()
        logger.info("[ACTION] State file deleted (clean completion).")

        logger.info("TaskRunner.run() finished: %s", result)
        return result

    # ------------------------------------------------------------------
    # Output line callback
    # ------------------------------------------------------------------

    def _on_output_line(self, raw_line: str, clean_line: str = "") -> None:
        """
        Called for every line of output received from the Claude Code process.

        ClaudeProcess calls this as on_line(raw_line, clean_line) with two
        positional args; the default value makes it safe to call with one arg
        in tests.

        Responsibilities:
          - Normalise line endings.
          - Strip ANSI escape codes for storage.
          - Feed to RateLimitDetector.
          - Feed to ContextManager (token counting).
          - Notify TUI callback.
          - Periodically check context threshold and inject checkpoint.
        """
        # Normalise CRLF (Windows PTY artefact) to LF.
        line = raw_line.replace("\r\n", "\n").replace("\r", "\n")
        # Use the pre-stripped clean_line when provided by ClaudeProcess; re-strip otherwise.
        clean = (clean_line.replace("\r\n", "\n").replace("\r", "\n").strip()
                 if clean_line else _strip_ansi(line).strip())

        if clean:
            self._output_lines.append(clean)
            self._last_output_time = time.monotonic()

        # Feed rate-limit / runner-marker detector (works on the clean line).
        if self._rate_detector is not None:
            self._rate_detector.feed(clean)
            if self._rate_detector.matched_runner_complete:
                logger.info("[MARKER] ##RUNNER:COMPLETE## detected — signalling task done.")
                if self._runner_complete_event is not None and self._loop is not None:
                    self._loop.call_soon_threadsafe(self._runner_complete_event.set)
            elif self._rate_detector.matched_runner_error is not None:
                msg = self._rate_detector.matched_runner_error
                logger.info("[MARKER] ##RUNNER:ERROR## detected — %r", msg)
                self._runner_error_message = msg
                if self._runner_error_event is not None and self._loop is not None:
                    self._loop.call_soon_threadsafe(self._runner_error_event.set)

        # Token counting + checkpoint-end detection.
        if self._context_manager is not None:
            self._context_manager.count_output(clean)
            self._context_manager.notify_output_line(clean)

        # TUI update.
        self._tui_update("output_line", {"line": clean, "raw": line})

        # Milestone detection — runs on every clean output line.
        if clean and self._milestone_patterns:
            self._check_milestones(clean)

        # Context threshold check (fires the callback synchronously if over threshold).
        if self._context_manager is not None and self._context_manager.check_threshold():
            logger.info("[ACTION] Context threshold crossed — injecting checkpoint.")
            try:
                self._context_manager.inject_checkpoint()
                self._tui_update(
                    "checkpoint_injected",
                    {"checkpoint_count": self._context_manager.checkpoint_count},
                )
            except Exception as exc:
                warn = f"Context checkpoint injection failed: {exc}"
                logger.warning(warn)
                self._fault_log.append(f"[WARN] {warn}")

    # ------------------------------------------------------------------
    # Initial prompt builder
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Milestone detection
    # ------------------------------------------------------------------

    def _check_milestones(self, clean_line: str) -> None:
        """
        Test *clean_line* against all compiled milestone patterns.

        On first match for a given message string:
          - Logs the milestone at INFO level.
          - Dispatches a "milestone" notification (desktop + webhook only,
            never email — per spec section 2.4 and 4.5).
          - Adds a notification entry to the TUI.
          - Records the message in _milestones_fired so it does not re-fire.

        Subsequent lines that match the same milestone are silently ignored.
        """
        for pattern, message in self._milestone_patterns:
            if message in self._milestones_fired:
                continue
            if pattern.search(clean_line):
                self._milestones_fired.add(message)
                logger.info("[MILESTONE] %r matched in output. Message: %r", pattern.pattern, message)
                self._tui_update("notification", {"message": f"[milestone] {message}"})
                # Dispatch asynchronously-safe: notifier.dispatch is synchronous.
                try:
                    if self._notifier is not None:
                        self._notifier.dispatch("milestone", message)
                except Exception as exc:
                    logger.warning("Milestone notification dispatch failed: %s", exc)

    # ------------------------------------------------------------------
    # Git workflow
    # ------------------------------------------------------------------

    def _run_git_workflow(self) -> str:
        """
        Run the post-completion git workflow (branch creation, commit, optional push).

        Called from _handle_completion() when output.git.enabled is True.

        Steps:
          1. Verify working directory is a git repo (skip silently if not).
          2. Create a new branch: <branch_prefix><slug>-<YYYYMMDD_HHMMSS>
          3. Stage all changes: git add -A
          4. Commit with a structured message.
          5. Push to origin if auto_push is True.
          6. Attempt to open a PR via `gh pr create` if `gh` CLI is available
             and auto_push is True.

        Returns a human-readable summary string (for the report), or an empty
        string if git is not available or the workflow was skipped.
        """
        git_cfg = getattr(getattr(self._book, "output", None), "git", None)
        if git_cfg is None or not getattr(git_cfg, "enabled", False):
            return ""

        working_dir = self._working_dir()
        if working_dir is None:
            logger.warning("Git workflow skipped: working directory unknown.")
            return ""

        # --- Check this is a git repo ------------------------------------
        check = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=working_dir, capture_output=True, text=True, timeout=10,
        )
        if check.returncode != 0:
            logger.info("Git workflow skipped: %s is not a git repository.", working_dir)
            return ""

        branch_prefix = getattr(git_cfg, "branch_prefix", "claude-task/")
        auto_push = getattr(git_cfg, "auto_push", False)
        slug = self._project_id
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        branch_name = f"{branch_prefix}{slug}-{timestamp}"

        summary_lines: list[str] = []

        # --- Create branch -----------------------------------------------
        branch_result = subprocess.run(
            ["git", "checkout", "-b", branch_name],
            cwd=working_dir, capture_output=True, text=True, timeout=30,
        )
        if branch_result.returncode != 0:
            warn = f"git checkout -b failed: {branch_result.stderr.strip()}"
            logger.warning("[ACTION] %s", warn)
            self._fault_log.append(f"[WARN] git: {warn}")
            return ""
        logger.info("[ACTION] git: created branch %r.", branch_name)
        summary_lines.append(f"Branch: {branch_name}")

        # --- Stage all changes -------------------------------------------
        add_result = subprocess.run(
            ["git", "add", "-A"],
            cwd=working_dir, capture_output=True, text=True, timeout=30,
        )
        if add_result.returncode != 0:
            warn = f"git add -A failed: {add_result.stderr.strip()}"
            logger.warning("[ACTION] %s", warn)
            self._fault_log.append(f"[WARN] git: {warn}")

        # --- Commit ------------------------------------------------------
        commit_msg = (
            f"claude-runner: {self._book.name}\n\n"
            f"Automated commit by claude-runner.\n"
            f"Task: {self._book.name}\n"
            f"Rate-limit cycles: {self._rate_limit_cycles}\n"
            f"Context checkpoints: {self._context_manager.checkpoint_count if self._context_manager else 0}\n"
        )
        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_msg, "--allow-empty"],
            cwd=working_dir, capture_output=True, text=True, timeout=30,
        )
        if commit_result.returncode != 0:
            warn = f"git commit failed: {commit_result.stderr.strip()}"
            logger.warning("[ACTION] %s", warn)
            self._fault_log.append(f"[WARN] git: {warn}")
        else:
            logger.info("[ACTION] git: committed changes on branch %r.", branch_name)
            summary_lines.append(f"Commit: {commit_result.stdout.strip()[:80]}")

        # --- Push --------------------------------------------------------
        if auto_push:
            push_result = subprocess.run(
                ["git", "push", "-u", "origin", branch_name],
                cwd=working_dir, capture_output=True, text=True, timeout=60,
            )
            if push_result.returncode != 0:
                warn = f"git push failed: {push_result.stderr.strip()}"
                logger.warning("[ACTION] %s", warn)
                self._fault_log.append(f"[WARN] git: {warn}")
            else:
                logger.info("[ACTION] git: pushed branch %r to origin.", branch_name)
                summary_lines.append(f"Pushed: origin/{branch_name}")

                # --- Attempt PR via gh CLI --------------------------------
                gh_path = shutil.which("gh")
                if gh_path:
                    pr_title = f"claude-runner: {self._book.name}"
                    pr_body = (
                        f"Automated pull request created by claude-runner.\n\n"
                        f"**Task:** {self._book.name}\n"
                        f"**Rate-limit cycles:** {self._rate_limit_cycles}\n"
                    )
                    pr_result = subprocess.run(
                        [gh_path, "pr", "create",
                         "--title", pr_title,
                         "--body", pr_body,
                         "--head", branch_name],
                        cwd=working_dir, capture_output=True, text=True, timeout=60,
                    )
                    if pr_result.returncode == 0:
                        pr_url = pr_result.stdout.strip()
                        logger.info("[ACTION] PR created: %s", pr_url)
                        summary_lines.append(f"PR: {pr_url}")
                    else:
                        logger.warning(
                            "gh pr create failed (non-fatal): %s", pr_result.stderr.strip()
                        )
                else:
                    logger.debug("gh CLI not found — skipping PR creation.")

        return "\n".join(summary_lines)

    # ------------------------------------------------------------------
    # Initial prompt builder
    # ------------------------------------------------------------------

    def _build_initial_prompt(self) -> str:
        """
        Build the full prompt sent to Claude Code at task start.

        Prepends the mandatory progress-log instructions defined in spec §2.6
        before the user-supplied task prompt from the project book.
        """
        task_prompt = getattr(self._book, "prompt", "").strip()
        if not task_prompt:
            raise ValueError(
                f"ProjectBook {self._book.name!r} has an empty prompt.  "
                "A task prompt is required."
            )

        # Apply RUNNER_PROTOCOL + context_anchors via ContextManager so the
        # ordering is always: runner protocol → anchors → progress log → task.
        decorated = (
            self._context_manager.build_initial_prompt(task_prompt)
            if self._context_manager
            else task_prompt
        )
        full_prompt = _PROGRESS_LOG_INSTRUCTION + decorated
        logger.debug(
            "Initial prompt built: instruction=%d chars, task=%d chars, total=%d chars.",
            len(_PROGRESS_LOG_INSTRUCTION),
            len(task_prompt),
            len(full_prompt),
        )
        return full_prompt

    # ------------------------------------------------------------------
    # Output diff collection
    # ------------------------------------------------------------------

    def _collect_output_diff(self) -> str:
        """
        Collect a change summary for the completion email and report.

        Tries `git diff --stat` first; falls back to a filesystem snapshot diff
        if git is not available or the working directory is not a git repository.

        Returns a formatted string in the style of `git diff --stat`.
        """
        working_dir = self._working_dir()

        # --- Try git diff --stat -----------------------------------------
        try:
            result = subprocess.run(
                ["git", "diff", "--stat", "HEAD"],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                diff_text = result.stdout.strip()
                # Also capture untracked files.
                untracked = subprocess.run(
                    ["git", "ls-files", "--others", "--exclude-standard"],
                    cwd=working_dir,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if untracked.returncode == 0 and untracked.stdout.strip():
                    new_files = untracked.stdout.strip().splitlines()
                    untracked_lines = "\n".join(
                        f"  {f} (new, untracked)" for f in new_files
                    )
                    diff_text = diff_text + "\nUntracked new files:\n" + untracked_lines
                logger.info("Change summary collected via git diff --stat.")
                return diff_text
        except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
            logger.info("git diff not available (%s); using filesystem snapshot diff.", exc)

        # --- Filesystem snapshot fallback --------------------------------
        if not self._fs_snapshot_start:
            return "(No change summary available: snapshot was not taken at task start.)"

        current_snapshot = self._take_fs_snapshot()
        return _fs_diff(self._fs_snapshot_start, current_snapshot)

    # ------------------------------------------------------------------
    # Progress log helpers
    # ------------------------------------------------------------------

    def _init_progress_log(self) -> None:
        """
        Create the progress log file in the working directory.

        Creates .claude-runner/ directory and writes a header into progress.log.
        Called once during _initialise() before Claude Code is launched.
        """
        log_path = self._progress_log_path()
        if log_path is None:
            logger.warning("Cannot initialise progress log: working directory unknown.")
            return

        log_path.parent.mkdir(parents=True, exist_ok=True)
        header = _PROGRESS_LOG_HEADER_TEMPLATE.format(
            task_name=self._book.name,
            start_time=_fmt_time(datetime.now(tz=timezone.utc)),
        )
        try:
            log_path.write_text(header, encoding="utf-8")
            logger.info("[ACTION] Progress log initialised at %s.", log_path)
        except OSError as exc:
            warn = f"Could not create progress log at {log_path}: {exc}"
            logger.warning(warn)
            self._fault_log.append(f"[WARN] {warn}")

    def _save_progress_log_to_host(self, contents: str) -> None:
        """
        Copy progress.log contents to the host log directory.

        Written alongside the full report so both survive sandbox teardown.
        """
        log_dir = self._host_log_dir()
        if log_dir is None:
            return
        dest = log_dir / f"{self._project_id}_progress.log"
        try:
            dest.write_text(contents or "(empty)", encoding="utf-8")
            logger.info("[ACTION] progress.log saved to host: %s.", dest)
        except OSError as exc:
            warn = f"Could not save progress.log to host: {exc}"
            logger.warning(warn)
            self._fault_log.append(f"[WARN] {warn}")

    # ------------------------------------------------------------------
    # Full report writer
    # ------------------------------------------------------------------

    def _write_full_report(self, result: TaskResult) -> None:
        """
        Write a complete text report to the host log directory.

        Includes: timing, token stats, rate-limit cycles, checkpoint count,
        fault_log, change_summary, and progress.log contents.
        """
        log_dir = self._host_log_dir()
        if log_dir is None:
            logger.warning("log_dir not configured — skipping full report write.")
            return

        log_dir.mkdir(parents=True, exist_ok=True)
        report_name = (
            f"{self._project_id}_"
            f"{result.start_time.strftime('%Y%m%d_%H%M%S')}_report.txt"
        )
        report_path = log_dir / report_name

        lines = [
            "=" * 72,
            f"claude-runner — Task Report",
            "=" * 72,
            f"Task:              {result.task_name}",
            f"Status:            {result.status.upper()}",
            f"Started:           {_fmt_time(result.start_time)}",
            f"Ended:             {_fmt_time(result.end_time)}",
            f"Duration:          {result.duration_str}",
            f"Rate limit cycles: {result.rate_limit_cycles}",
            f"Context checkpts:  {result.checkpoint_count}",
            f"Est. tokens used:  {self._context_manager.estimated_tokens if self._context_manager else 'N/A'}",
            "",
            "-" * 72,
            "CHANGE SUMMARY",
            "-" * 72,
            result.change_summary or "(none)",
            "",
        ]

        if result.error_message:
            lines += [
                "-" * 72,
                "ERROR",
                "-" * 72,
                result.error_message,
                "",
            ]

        if result.fault_log:
            lines += [
                "-" * 72,
                "FAULT LOG",
                "-" * 72,
            ]
            lines.extend(result.fault_log)
            lines.append("")

        lines += [
            "-" * 72,
            "PROGRESS LOG",
            "-" * 72,
            result.progress_log or "(empty)",
            "",
            "=" * 72,
            "END OF REPORT",
            "=" * 72,
        ]

        try:
            report_path.write_text("\n".join(lines), encoding="utf-8")
            logger.info("[ACTION] Full report written to %s.", report_path)
        except OSError as exc:
            warn = f"Could not write full report to {report_path}: {exc}"
            logger.warning(warn)
            self._fault_log.append(f"[WARN] {warn}")

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _checkpoint_state(self) -> None:
        """Save current orchestration state to the persistence manager."""
        if self._persistence is None:
            return
        try:
            self._persistence.save(
                self._make_state(
                    "running",
                    rate_limit_wait_count=self._rate_limit_cycles,
                    token_estimate=(
                        self._context_manager.estimated_tokens
                        if self._context_manager else 0
                    ),
                )
            )
        except Exception as exc:
            warn = f"State checkpoint failed: {exc}"
            logger.warning(warn)
            self._fault_log.append(f"[WARN] {warn}")

    async def _state_checkpoint_loop(self) -> None:
        """Background coroutine that saves state every _STATE_CHECKPOINT_INTERVAL_S seconds."""
        while True:
            await asyncio.sleep(_STATE_CHECKPOINT_INTERVAL_S)
            logger.debug("Heartbeat checkpoint: saving state.")
            self._checkpoint_state()

    async def _silence_watchdog(self, silence_timeout_s: float) -> None:
        """
        Background coroutine that detects prolonged output silence.

        On each wake cycle (every *silence_timeout_s* seconds):
          1. If no output was received during the sleep → log a warning and
             send a ``continue`` probe to the process.
          2. Sleep another *silence_timeout_s*.
          3. If still no output after the probe → set _runner_error_event and exit.

        Resets automatically whenever _last_output_time is updated (i.e. whenever
        a non-empty output line arrives).
        """
        while True:
            await asyncio.sleep(silence_timeout_s)

            elapsed = time.monotonic() - self._last_output_time
            if elapsed < silence_timeout_s:
                # New output arrived during our sleep — nothing to do.
                continue

            logger.warning(
                "No output for %.0fs — possible undetected rate limit.  Sending probe.",
                elapsed,
            )
            try:
                self._send_to_process("continue\n")
            except Exception as exc:
                logger.warning("Silence probe send failed: %s", exc)

            # Wait one more window; if output resumes, the next cycle is a no-op.
            await asyncio.sleep(silence_timeout_s)

            elapsed2 = time.monotonic() - self._last_output_time
            if elapsed2 >= silence_timeout_s:
                msg = (
                    f"Silence timeout: no output for {elapsed2:.0f}s after probe — "
                    "possible hung process or undetected rate limit."
                )
                logger.error(msg)
                self._runner_error_message = msg
                self._runner_error_event.set()
                return

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _cleanup(self) -> None:
        """Tear down resources unconditionally."""
        for task_attr in ("_checkpoint_task", "_silence_watchdog_task"):
            task = getattr(self, task_attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._sandbox is not None:
            try:
                logger.info("[ACTION] Tearing down sandbox.")
                await _maybe_await(self._sandbox.teardown)
            except Exception as exc:
                logger.warning("Sandbox teardown raised an exception: %s", exc)

    # ------------------------------------------------------------------
    # Notification dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, event: str, data: dict) -> None:
        """
        Send a notification event via the NotificationManager.

        Converts the data dict to a human-readable message string and extracts
        the optional change_summary key for email body use.

        Logs the action regardless of whether the notifier is configured.
        Catches and logs (but does not re-raise) notification errors so that a
        broken notification channel never kills the orchestration loop.
        """
        logger.info("[ACTION] Dispatching notification event=%r data=%s", event, data)
        if self._notifier is None:
            return
        try:
            change_summary: str = data.pop("change_summary", "") if isinstance(data, dict) else ""
            # Build a human-readable message from the remaining data fields.
            message_parts = [f"{k}={v}" for k, v in (data.items() if isinstance(data, dict) else [])]
            message = f"[claude-runner] {event}: " + ", ".join(message_parts) if message_parts else f"[claude-runner] {event}"
            self._notifier.dispatch(event, message, change_summary)
        except Exception as exc:
            warn = f"Notification dispatch failed for event={event!r}: {exc}"
            logger.warning(warn)
            self._fault_log.append(f"[WARN] {warn}")

    # ------------------------------------------------------------------
    # TUI helpers
    # ------------------------------------------------------------------

    def _tui_update(self, event: str, data: dict) -> None:
        """
        Send a TUI update event.

        Routes to:
        - self._tui (TUIManager) via direct method calls, if available.
        - self._tui_callback(event, data), if provided (alternative hook).
        TUI errors never crash the orchestration loop.
        """
        tui = self._tui
        if tui is not None:
            try:
                if event == "output_line":
                    tui.add_output_line(data.get("line", ""))
                elif event == "state_change":
                    tui.update_state(data.get("state", "running"))
                elif event == "notification":
                    tui.add_notification(data.get("message", ""))
                elif event == "countdown_tick":
                    tui.update_rate_limit_countdown(data.get("remaining_seconds", 0))
                elif event == "checkpoint_injected":
                    tui.update_tokens(
                        self._context_manager.estimated_tokens if self._context_manager else 0,
                        self._context_manager.threshold_tokens if self._context_manager else 150_000,
                        data.get("checkpoint_count", 0),
                    )
                elif event == "context_anchors_active":
                    tui.set_context_anchors_active(data.get("active", False))
                elif event == "rate_limit_waits":
                    tui.update_rate_limit_waits(data.get("count", 0))
            except Exception as exc:
                logger.debug("TUI direct update raised: %s", exc)

        if self._tui_callback is not None:
            try:
                self._tui_callback(event, data)
            except Exception as exc:
                # TUI errors must never crash the orchestration loop.
                logger.debug("TUI callback raised: %s", exc)

    def _on_countdown_tick(self, remaining_s: float) -> None:
        """Forwarded from RateLimitWaiter; updates the TUI countdown display."""
        reset_time = self._rate_limit_reset_time
        self._tui_update(
            "countdown_tick",
            {
                "remaining_seconds": remaining_s,
                "reset_time": _fmt_time(reset_time) if reset_time else "unknown",
                "task": self._book.name,
            },
        )

    # ------------------------------------------------------------------
    # Process I/O
    # ------------------------------------------------------------------

    def _send_to_process(self, text: str) -> None:
        """
        Write text to the Claude Code process stdin.

        Appends a newline if the text does not already end with one, mirroring
        the behaviour of a user pressing Enter.

        Logs the action (without logging the full text if it is a large prompt,
        to keep logs readable).
        """
        if self._process is None:
            raise RuntimeError("Cannot send text: Claude Code process is not running.")
        payload = text if text.endswith("\n") else text + "\n"
        preview = payload[:80].replace("\n", "\\n")
        logger.info("[ACTION] Sending input to Claude Code (%d chars): %r…", len(payload), preview)
        self._process.send(payload)

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    def _working_dir(self) -> Path:
        """Return the task's working directory as a Path."""
        from .sandbox import resolve_working_dir  # noqa: PLC0415
        return resolve_working_dir(self._book, book_path=self._book_path)

    def _progress_log_path(self) -> Path:
        """Return the absolute path to progress.log inside the working directory."""
        return self._working_dir() / _RUNNER_DIR / _PROGRESS_LOG_NAME

    def _host_log_dir(self) -> Optional[Path]:
        """Return the host-side log directory, creating it if necessary."""
        output_cfg = getattr(self._book, "output", None)
        log_dir_str = getattr(output_cfg, "log_dir", None)
        if not log_dir_str:
            # Fallback to global config default.
            log_dir_str = getattr(self._config, "log_dir", None)
        if not log_dir_str:
            return None
        log_dir = Path(log_dir_str).expanduser()
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def _take_fs_snapshot(self) -> dict[str, tuple[int, float]]:
        """
        Walk the working directory and record (size_bytes, mtime) for each file.

        Excludes the .claude-runner/ subdirectory (runner artefacts, not task output).
        Returns an empty dict if the directory cannot be walked.
        """
        wd = self._working_dir()
        snapshot: dict[str, tuple[int, float]] = {}
        try:
            for entry in wd.rglob("*"):
                if not entry.is_file():
                    continue
                # Exclude runner artefacts.
                if _RUNNER_DIR in entry.parts:
                    continue
                try:
                    stat = entry.stat()
                    snapshot[str(entry.relative_to(wd))] = (stat.st_size, stat.st_mtime)
                except OSError:
                    pass
        except OSError as exc:
            logger.warning("Could not snapshot working directory %s: %s", wd, exc)
        return snapshot

    # ------------------------------------------------------------------
    # State factory
    # ------------------------------------------------------------------

    def _make_state(self, phase: str, **kwargs) -> "TaskState":  # type: ignore[name-defined]
        """
        Build a TaskState for the given phase, incorporating current runtime state.

        Extra keyword arguments are forwarded to the TaskState constructor and
        override the defaults computed here (e.g. rate_limit_wait_count,
        token_estimate).
        """
        from .persistence import TaskState  # noqa: PLC0415

        now = datetime.now(timezone.utc)
        start_iso = (
            self._start_time.isoformat()
            if self._start_time
            else now.isoformat()
        )
        progress_path = self._progress_log_path()
        defaults = dict(
            task_name=self._project_id,
            project_book_path=str(self._book_path) if self._book_path is not None else self._book.name,
            start_time=start_iso,
            current_phase=phase,
            rate_limit_wait_count=self._rate_limit_cycles,
            token_estimate=(
                self._context_manager.estimated_tokens
                if self._context_manager is not None
                else 0
            ),
            checkpoint_count=(
                self._context_manager.checkpoint_count
                if self._context_manager is not None
                else 0
            ),
            progress_log_path=str(progress_path) if progress_path else None,
            fault_log=list(self._fault_log),
        )
        defaults.update(kwargs)
        return TaskState(**defaults)

    # ------------------------------------------------------------------
    # Result factory
    # ------------------------------------------------------------------

    def _make_result(
        self,
        status: str,
        error_message: Optional[str] = None,
    ) -> TaskResult:
        """Build a TaskResult from current runner state."""
        end_time = datetime.now(tz=timezone.utc)
        progress_log = (
            self._context_manager.read_progress_log()
            if self._context_manager is not None
            else ""
        )
        change_summary = ""
        try:
            change_summary = self._collect_output_diff()
        except Exception as exc:
            logger.debug("Could not collect diff for error result: %s", exc)

        return TaskResult(
            task_name=self._book.name,
            status=status,
            start_time=self._start_time or end_time,
            end_time=end_time,
            rate_limit_cycles=self._rate_limit_cycles,
            checkpoint_count=(
                self._context_manager.checkpoint_count
                if self._context_manager is not None
                else 0
            ),
            change_summary=change_summary,
            progress_log=progress_log,
            fault_log=list(self._fault_log),
            error_message=error_message,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from a string."""
    return _ANSI_ESCAPE_RE.sub("", text)


def _fmt_time(dt: datetime) -> str:
    """Format a datetime as a human-readable UTC string."""
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _safe_name(name: str) -> str:
    """Convert a task name to a filesystem-safe slug."""
    return re.sub(r"[^\w\-]", "_", name).strip("_")[:64]


def _fs_diff(
    before: dict[str, tuple[int, float]],
    after: dict[str, tuple[int, float]],
) -> str:
    """
    Produce a git-diff-stat style summary from two filesystem snapshots.

    Each snapshot maps relative_path → (size_bytes, mtime).
    """
    lines: list[str] = []
    new_files: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []

    all_paths = set(before) | set(after)
    for path in sorted(all_paths):
        if path not in before:
            new_files.append(path)
        elif path not in after:
            deleted.append(path)
        else:
            b_size, b_mtime = before[path]
            a_size, a_mtime = after[path]
            if a_size != b_size or abs(a_mtime - b_mtime) > 0.001:
                modified.append((path, b_size, a_size))  # type: ignore[arg-type]

    if not (new_files or modified or deleted):
        return "(No filesystem changes detected.)"

    for path in new_files:
        size = after[path][0]
        lines.append(f"  {path:<55}  (new, {_fmt_size(size)})")
    for path, b_size, a_size in modified:  # type: ignore[misc]
        delta = a_size - b_size
        sign = "+" if delta >= 0 else ""
        lines.append(f"  {path:<55}  modified ({sign}{_fmt_size(delta)})")
    for path in deleted:
        lines.append(f"  {path:<55}  (deleted)")

    summary_parts = []
    if new_files:
        summary_parts.append(f"{len(new_files)} new")
    if modified:
        summary_parts.append(f"{len(modified)} modified")
    if deleted:
        summary_parts.append(f"{len(deleted)} deleted")
    lines.append("")
    lines.append("  " + ", ".join(summary_parts))

    return "\n".join(lines)


def _fmt_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable string."""
    if abs(size_bytes) < 1024:
        return f"{size_bytes} B"
    if abs(size_bytes) < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


async def _maybe_await(fn, *args, **kwargs):
    """
    Call fn(*args, **kwargs).  If the result is a coroutine, await it.

    Allows TaskRunner to call sandbox methods that may be sync or async,
    without requiring the sandbox interface to commit to one model.
    """
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return await result
    return result


# ---------------------------------------------------------------------------
# Alias so that main.py can instantiate ClaudeRunner(project_book=..., ...)
# ---------------------------------------------------------------------------

ClaudeRunner = TaskRunner
