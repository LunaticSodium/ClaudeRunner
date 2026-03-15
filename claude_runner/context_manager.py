"""
context_manager.py — Token estimation and checkpoint injection for claude-runner.

Operates as an independent context-length management layer between the orchestrator
and the Claude Code process.  It does NOT interfere with Claude Code's own internal
context compression; instead it fires *before* that compression threshold to ensure
an externally-readable progress.log is always fresh.

Key design decisions
--------------------
- Token estimation is character-based only (CHARS_PER_TOKEN = 4).  No tokenizer
  dependency; the approximation is intentionally coarse — the goal is early warning.
- Checkpoint threshold is 150 000 tokens by default, well below the estimated
  180-200 k threshold at which Claude Code's own compression fires.
- After a checkpoint response is received the counter is reset; the checkpoint
  prompt + response are then counted toward the *next* cycle.
- reset_on_rate_limit  → caller must invoke inject_checkpoint() before each rate-
  limit wait, regardless of token count.  The ContextManager enforces nothing here;
  the flag is a configuration hint consumed by runner.py.
- inject_log_on_resume → build_resume_prompt() always prepends progress.log when
  this flag is True, whatever the strategy.

Thread safety: this class is NOT thread-safe.  It is designed to be driven from a
single async task in runner.py.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHARS_PER_TOKEN: int = 4  # approximation for English / code

# Default path relative to the working directory inside the sandbox.
# Matches the spec path: /workspace/.claude-runner/progress.log
_DEFAULT_PROGRESS_LOG_SUBPATH = ".claude-runner/progress.log"

# Runner protocol injected at the top of every initial prompt, before any
# project-level context_anchors.  These markers let the orchestration layer
# detect task completion and fatal errors from the output stream rather than
# relying solely on the subprocess exit code.
RUNNER_PROTOCOL: str = """\
RUNNER PROTOCOL (mandatory):
When your task is complete, output exactly on its own line:
  ##RUNNER:COMPLETE##
When you encounter a fatal error you cannot recover from, output:
  ##RUNNER:ERROR:<one line description>##
These markers are read by the orchestration layer. Do not omit them.\
"""

CHECKPOINT_PROMPT: str = """
Please pause and write a structured progress update to the file:
  .claude-runner/progress.log

Use this format for each entry:
  [TIMESTAMP] [PHASE] Description of current state
  [TIMESTAMP] [DONE]  Description of completed step
  [TIMESTAMP] [BLOCK] Description of a blocker or open question
  [TIMESTAMP] [DECISION] Rationale for a choice made

After writing the progress log, briefly confirm:
1. What you have completed
2. What you will do next
3. Any blockers or open questions

Then continue with the task.
"""

# Template prepended to progress-log contents when injecting on resume.
_PROGRESS_LOG_HEADER = (
    "The following is a structured progress log from your earlier work on this task. "
    "Use it to restore context before continuing.\n\n"
    "--- progress.log ---\n"
    "{log_contents}\n"
    "--- end of progress.log ---\n\n"
)

# Resume strategy constants — kept here so runner.py can import them.
STRATEGY_CONTINUE = "continue"
STRATEGY_RESTATE = "restate"
STRATEGY_SUMMARIZE = "summarize"

_VALID_STRATEGIES = {STRATEGY_CONTINUE, STRATEGY_RESTATE, STRATEGY_SUMMARIZE}


# ---------------------------------------------------------------------------
# ContextManager
# ---------------------------------------------------------------------------


class ContextManager:
    """
    Independent context-length management layer.

    Operates at the communication level between the orchestrator and Claude Code.
    Does NOT interfere with Claude Code's own context compression.

    Purpose: ensure progress.log is always fresh, and that the orchestration
    layer has reliable external state even if Claude Code's context is reorganised.

    Parameters
    ----------
    threshold_tokens:
        Estimated token count at which a checkpoint is injected.
        Default 150 000 — below Claude Code's own compression (~180-200 k).
    reset_on_rate_limit:
        Configuration hint (consumed by runner.py).  When True, runner.py
        must call inject_checkpoint() before each rate-limit wait, regardless
        of the current token count.
    inject_log_on_resume:
        When True, build_resume_prompt() always prepends the current contents
        of progress.log to the returned string.
    progress_log_path:
        Absolute path to the progress.log file.  If None, ContextManager
        attempts to derive it from the sandbox working directory (this requires
        the caller to set it explicitly for meaningful operation).
    on_inject_checkpoint:
        Callable invoked with the checkpoint prompt text when inject_checkpoint()
        is called.  Typically this sends the text to the Claude Code process.
        If None, a warning is logged and the injection is a no-op (useful for
        testing or dry runs).
    """

    def __init__(
        self,
        threshold_tokens: int = 150_000,
        reset_on_rate_limit: bool = True,
        inject_log_on_resume: bool = True,
        progress_log_path: Optional[Path] = None,
        on_inject_checkpoint: Optional[Callable[[str], None]] = None,
        context_anchors: Optional[str] = None,
    ) -> None:
        if threshold_tokens <= 0:
            raise ValueError(
                f"threshold_tokens must be a positive integer, got {threshold_tokens!r}"
            )

        self._threshold_tokens: int = threshold_tokens
        self.reset_on_rate_limit: bool = reset_on_rate_limit
        self.inject_log_on_resume: bool = inject_log_on_resume
        self.progress_log_path: Optional[Path] = progress_log_path
        self._on_inject_checkpoint: Optional[Callable[[str], None]] = on_inject_checkpoint
        # context_anchors: stripped plain-text instructions prepended verbatim to
        # every outbound prompt.  None means the feature is disabled.
        self._context_anchors: Optional[str] = context_anchors.strip() if context_anchors else None

        # Mutable state
        self._token_estimate: int = 0
        self._checkpoint_count: int = 0
        self._original_prompt: str = ""

        # Checkpoint response exclusion: while True, count_output() is a no-op
        # so Claude's checkpoint-response tokens don't inflate the estimate.
        self._in_checkpoint: bool = False
        # Becomes True when a blank line or "Then continue" appears in output
        # while _in_checkpoint is set; the next non-empty line clears both.
        self._checkpoint_saw_signal: bool = False

        # Lock protects _token_estimate so that count_input / count_output can
        # be called safely from background I/O threads if needed.
        self._lock = threading.Lock()

        logger.debug(
            "ContextManager initialised: threshold=%d tokens, reset_on_rate_limit=%s, "
            "inject_log_on_resume=%s, progress_log=%s, context_anchors=%s",
            self._threshold_tokens,
            self.reset_on_rate_limit,
            self.inject_log_on_resume,
            self.progress_log_path,
            "active" if self._context_anchors else "none",
        )

    # ------------------------------------------------------------------
    # Token accounting
    # ------------------------------------------------------------------

    def count_input(self, text: str) -> None:
        """
        Add input text (a prompt sent to Claude Code) to the running token estimate.

        This should be called every time text is written to the Claude Code process
        input stream.

        Parameters
        ----------
        text:
            The raw text being sent to Claude Code.
        """
        if not text:
            return
        tokens = _chars_to_tokens(len(text))
        with self._lock:
            self._token_estimate += tokens
        logger.debug("count_input: +%d tokens (%d chars) → total %d", tokens, len(text), self._token_estimate)

    def count_output(self, text: str) -> None:
        """
        Add output text (a line received from Claude Code) to the running token estimate.

        This should be called for every line or chunk received from the Claude Code
        output stream.

        Parameters
        ----------
        text:
            The raw text received from Claude Code (may include ANSI escape codes;
            character count is still meaningful for estimation purposes).
        """
        if not text:
            return
        # While Claude is writing its checkpoint response, skip token counting
        # so the response itself does not push us immediately over threshold again.
        if self._in_checkpoint:
            return
        tokens = _chars_to_tokens(len(text))
        with self._lock:
            self._token_estimate += tokens
        logger.debug("count_output: +%d tokens (%d chars) → total %d", tokens, len(text), self._token_estimate)

    # ------------------------------------------------------------------
    # Threshold check
    # ------------------------------------------------------------------

    def check_threshold(self) -> bool:
        """
        Return True if the running token estimate has crossed the checkpoint threshold.

        The caller (runner.py) should check this periodically (e.g. after every output
        line) and call inject_checkpoint() when True is returned.

        Returns
        -------
        bool
            True  → threshold crossed; a checkpoint should be injected.
            False → still within the safe window.
        """
        with self._lock:
            over = self._token_estimate >= self._threshold_tokens
        if over:
            logger.info(
                "Context threshold crossed: %d estimated tokens >= %d threshold",
                self._token_estimate,
                self._threshold_tokens,
            )
        return over

    # ------------------------------------------------------------------
    # Checkpoint injection
    # ------------------------------------------------------------------

    def inject_checkpoint(self) -> None:
        """
        Send the checkpoint prompt to Claude Code via the on_inject_checkpoint callback,
        then reset the token counter.

        This may be called:
          - Automatically by runner.py when check_threshold() returns True.
          - Explicitly by runner.py before a rate-limit wait when reset_on_rate_limit
            is True.

        After this call returns:
          - The token counter is reset to zero.
          - checkpoint_count is incremented.
          - The on_inject_checkpoint callback has been invoked (if provided).
        """
        timestamp = _utc_now_str()
        logger.info(
            "[%s] Injecting context checkpoint (cycle %d, estimated tokens at time of injection: %d)",
            timestamp,
            self._checkpoint_count + 1,
            self._token_estimate,
        )

        if self._on_inject_checkpoint is not None:
            try:
                self._on_inject_checkpoint(self._prepend_anchors(CHECKPOINT_PROMPT))
            except Exception:
                logger.exception(
                    "on_inject_checkpoint callback raised an exception — "
                    "checkpoint prompt may not have been delivered to Claude Code"
                )
                # Re-raise: the caller must decide whether to continue or abort.
                raise
        else:
            logger.warning(
                "inject_checkpoint called but no on_inject_checkpoint callback is registered. "
                "The checkpoint prompt was NOT sent to Claude Code.  "
                "Set on_inject_checkpoint when constructing ContextManager."
            )

        self.reset()
        self._checkpoint_count += 1
        # Suppress token counting for Claude's checkpoint response until we see
        # a signal that it has finished (blank line, "Then continue", or the
        # next substantive output line after either of those).
        self._in_checkpoint = True
        self._checkpoint_saw_signal = False
        logger.info("Token counter reset after checkpoint injection (checkpoint #%d).", self._checkpoint_count)

    # ------------------------------------------------------------------
    # Resume prompt construction
    # ------------------------------------------------------------------

    def build_resume_prompt(self, base_resume: str, strategy: str) -> str:
        """
        Build the complete resume prompt that will be sent to Claude Code after a
        rate-limit wait.

        Parameters
        ----------
        base_resume:
            The "base" resume text.  Interpretation depends on strategy:
              - "continue"  : ignored; the literal string "continue" is returned
                              (unless inject_log_on_resume prepends the log).
              - "restate"   : ignored; the stored original_prompt is used.
              - "summarize" : ignored; progress.log contents drive the prompt.
        strategy:
            One of "continue", "restate", or "summarize".

        Returns
        -------
        str
            The fully constructed resume prompt, ready to be written to the Claude
            Code input stream.

        Raises
        ------
        ValueError
            If strategy is not a recognised value.
        """
        if strategy not in _VALID_STRATEGIES:
            raise ValueError(
                f"Unknown resume strategy {strategy!r}. "
                f"Valid options: {sorted(_VALID_STRATEGIES)}"
            )

        # ------------------------------------------------------------------
        # Build the "core" of the resume prompt according to strategy.
        # ------------------------------------------------------------------
        if strategy == STRATEGY_CONTINUE:
            core = "continue"

        elif strategy == STRATEGY_RESTATE:
            if not self._original_prompt:
                logger.warning(
                    "resume strategy is 'restate' but original_prompt has not been set. "
                    "Falling back to 'continue'.  Call set_original_prompt() before running."
                )
                core = "continue"
            else:
                core = "Continue from where you left off:\n\n" + self._original_prompt

        else:  # STRATEGY_SUMMARIZE
            log_contents = self.read_progress_log()
            if log_contents:
                core = (
                    "Continue from where you left off.  "
                    "Use the progress log below as your authoritative source of context "
                    "for what has been completed and what remains:\n\n"
                    + _PROGRESS_LOG_HEADER.format(log_contents=log_contents)
                )
            else:
                logger.warning(
                    "resume strategy is 'summarize' but progress.log is empty or not found. "
                    "Falling back to 'continue'."
                )
                core = "continue"

        # ------------------------------------------------------------------
        # Optionally prepend progress.log (inject_log_on_resume).
        # This is additive on top of the core for non-summarize strategies.
        # For 'summarize' the log is already embedded in core.
        # ------------------------------------------------------------------
        if self.inject_log_on_resume and strategy != STRATEGY_SUMMARIZE:
            log_contents = self.read_progress_log()
            if log_contents:
                prefix = _PROGRESS_LOG_HEADER.format(log_contents=log_contents)
                core = prefix + core
            else:
                logger.debug(
                    "inject_log_on_resume is True but progress.log is empty/missing — "
                    "no log prepended."
                )

        # Prepend context_anchors last so they always appear at the very top,
        # regardless of strategy or inject_log_on_resume ordering.
        core = self._prepend_anchors(core)

        logger.info(
            "Built resume prompt: strategy=%r, inject_log=%s, anchors=%s, length=%d chars",
            strategy,
            self.inject_log_on_resume,
            "active" if self._context_anchors else "none",
            len(core),
        )
        return core

    # ------------------------------------------------------------------
    # Progress log I/O
    # ------------------------------------------------------------------

    def read_progress_log(self) -> str:
        """
        Read the contents of the progress.log file.

        Returns the file's text content, or an empty string if the file does not
        exist, is empty, or cannot be read.

        Returns
        -------
        str
            Progress log text, or "" if unavailable.
        """
        if self.progress_log_path is None:
            logger.debug("read_progress_log: progress_log_path not set — returning empty string.")
            return ""

        try:
            text = self.progress_log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            logger.debug("read_progress_log: %s not found — returning empty string.", self.progress_log_path)
            return ""
        except OSError as exc:
            logger.warning("read_progress_log: could not read %s: %s", self.progress_log_path, exc)
            return ""

        stripped = text.strip()
        if not stripped:
            logger.debug("read_progress_log: %s is empty.", self.progress_log_path)
            return ""

        return stripped

    # ------------------------------------------------------------------
    # Properties and state management
    # ------------------------------------------------------------------

    @property
    def estimated_tokens(self) -> int:
        """Current running estimate of consumed tokens (input + output combined)."""
        with self._lock:
            return self._token_estimate

    @property
    def checkpoint_count(self) -> int:
        """Total number of checkpoints injected in this session."""
        return self._checkpoint_count

    @property
    def threshold_tokens(self) -> int:
        """The checkpoint threshold in tokens."""
        return self._threshold_tokens

    def reset(self) -> None:
        """
        Reset the running token counter to zero.

        Called automatically by inject_checkpoint().  May also be called explicitly
        by runner.py if an external reset point is needed (e.g. after a full process
        restart).
        """
        with self._lock:
            self._token_estimate = 0
        logger.debug("Token counter reset to zero.")

    def set_original_prompt(self, prompt: str) -> None:
        """
        Store the original task prompt for use with the 'restate' resume strategy.

        Must be called before run() begins so that the prompt is available if a
        rate-limit cycle occurs.

        Parameters
        ----------
        prompt:
            The full initial prompt sent to Claude Code at task start.
        """
        self._original_prompt = prompt
        logger.debug(
            "Original prompt stored (%d chars).",
            len(prompt),
        )

    @property
    def context_anchors_active(self) -> bool:
        """True if context_anchors are configured and will be prepended to prompts."""
        return bool(self._context_anchors)

    def acknowledge_checkpoint_end(self) -> None:
        """
        Signal that Claude has finished writing its checkpoint response.

        After this call, ``count_output()`` resumes normal token counting and
        ``_in_checkpoint`` is False.  Called by runner.py when it detects the
        end of Claude's checkpoint reply (blank line followed by a non-empty
        line, or a "Then continue" line followed by a non-empty line).
        """
        self._in_checkpoint = False
        self._checkpoint_saw_signal = False
        logger.debug("Checkpoint response acknowledged — token counting resumed.")

    def notify_output_line(self, clean_line: str) -> None:
        """
        Feed one clean output line to the checkpoint-end detector.

        Should be called for every line while the task is running.  When
        ``_in_checkpoint`` is True this method watches for the end-of-response
        signal and calls :meth:`acknowledge_checkpoint_end` automatically.

        Signal heuristics
        -----------------
        - Empty line (``not clean_line``)  → set the "saw signal" flag.
        - Line containing ``"Then continue"`` → set the "saw signal" flag
          (matches the last sentence of ``CHECKPOINT_PROMPT``).
        - Non-empty line after the signal flag is set → acknowledge end.
        """
        if not self._in_checkpoint:
            return
        if not clean_line or "Then continue" in clean_line:
            self._checkpoint_saw_signal = True
        elif self._checkpoint_saw_signal and clean_line:
            self.acknowledge_checkpoint_end()

    @property
    def in_checkpoint(self) -> bool:
        """True while Claude is writing its checkpoint response."""
        return self._in_checkpoint

    def build_initial_prompt(self, task_prompt: str) -> str:
        """
        Decorate *task_prompt* with the mandatory runner protocol and any
        project-level context anchors.

        Ordering (top → bottom in the final prompt):
          1. RUNNER_PROTOCOL  — always injected; tells Claude to emit
             ``##RUNNER:COMPLETE##`` / ``##RUNNER:ERROR:…##`` markers.
          2. context_anchors  — project-level standing instructions (if set).
          3. task_prompt      — the actual task text from the project book.

        The RUNNER_PROTOCOL is placed *before* context_anchors so that it
        cannot be shadowed or omitted by a project-level anchor block.

        Parameters
        ----------
        task_prompt:
            The raw task prompt from the project book (no decorations).

        Returns
        -------
        str
            Fully decorated initial prompt, ready for the ``_PROGRESS_LOG_INSTRUCTION``
            prefix that runner.py prepends before sending to Claude Code.
        """
        parts: list[str] = [RUNNER_PROTOCOL]
        if self._context_anchors:
            parts.append(self._context_anchors)
        parts.append(task_prompt)
        return "\n\n".join(parts)

    def _prepend_anchors(self, text: str) -> str:
        """
        Prepend context_anchors to *text* if anchors are configured.

        The anchors are separated from the rest of the text by a blank line.
        If no anchors are set, returns *text* unchanged.
        """
        if not self._context_anchors:
            return text
        return self._context_anchors + "\n\n" + text

    def set_on_inject_checkpoint(self, callback: Callable[[str], None]) -> None:
        """
        Update (or set for the first time) the checkpoint injection callback.

        Useful when the process handle is not available at ContextManager construction
        time but becomes available after the Claude Code process is launched.

        Parameters
        ----------
        callback:
            Callable that accepts a single str (the checkpoint prompt) and writes
            it to the Claude Code process input.
        """
        self._on_inject_checkpoint = callback
        logger.debug("on_inject_checkpoint callback registered.")

    # ------------------------------------------------------------------
    # Diagnostic helpers
    # ------------------------------------------------------------------

    def usage_fraction(self) -> float:
        """
        Return estimated token usage as a fraction of the threshold (0.0–1.0+).

        Values above 1.0 indicate the threshold has been crossed.  Intended for
        use by the TUI to draw a context-window usage indicator.
        """
        with self._lock:
            return self._token_estimate / self._threshold_tokens

    def __repr__(self) -> str:
        return (
            f"ContextManager("
            f"estimated_tokens={self.estimated_tokens}, "
            f"threshold={self._threshold_tokens}, "
            f"checkpoints={self._checkpoint_count}, "
            f"reset_on_rate_limit={self.reset_on_rate_limit}, "
            f"inject_log_on_resume={self.inject_log_on_resume}"
            f")"
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _chars_to_tokens(char_count: int) -> int:
    """Convert a character count to an approximate token count."""
    # Integer ceiling division: avoids importing math.
    return (char_count + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _utc_now_str() -> str:
    """Return the current UTC time as an ISO 8601 string (second precision)."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
