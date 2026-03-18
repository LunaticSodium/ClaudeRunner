"""
model_watchdog.py — Background thread that monitors git commit messages and
triggers model switches when phase-aware rules match.

Phase contract
--------------
Claude Code must prefix significant-milestone commit messages with
``PHASE-{N}: `` (e.g. ``PHASE-2: strategy classes done``) so that the watchdog
can detect the current phase from ``git log`` output.  The runner injects this
contract into ``CLAUDE.md`` before launching Claude Code (unless
``marathon_mode`` is True).

The watchdog fires at most once per PhaseRule per session: once a rule's action
is applied it is permanently suppressed for the remainder of the run.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# Matches "PHASE-N:" at the start of a git commit subject line (case-insensitive).
_PHASE_RE = re.compile(r"^PHASE-(\d+):\s*", re.IGNORECASE)


class ModelWatchdog:
    """
    Background thread that polls the working directory's git log to detect
    phase advances and evaluate model-switch triggers.

    Parameters
    ----------
    working_dir:
        Host-side working directory where ``git log`` is run to detect phases.
    rules:
        List of :class:`PhaseRule` objects from ``model_schedule.rules``.
        Each rule fires at most once per session.
    apply_fn:
        ``(model_id: str, reason: str) -> None``.  Called once per fired rule,
        from the watchdog thread.  Implementations must be thread-safe.
    poll_interval:
        How often (seconds) to poll git log.  Defaults to 15 s.
    get_token_pct:
        Optional callable returning the current context-window utilisation as a
        fraction in [0.0, 1.0].  Used to evaluate ``token_pct_gte`` /
        ``token_pct_lte`` trigger conditions.  When ``None``, token-pct
        conditions are treated as 0.0 (i.e. token-pct-gte > 0 never fires).
    """

    def __init__(
        self,
        working_dir: Path,
        rules: List,  # list[PhaseRule] — avoids circular import at call site
        apply_fn: Callable[[str, str], None],
        poll_interval: float = 15.0,
        get_token_pct: Optional[Callable[[], float]] = None,
    ) -> None:
        self._working_dir = working_dir
        self._rules = rules
        self._apply_fn = apply_fn
        self._poll_interval = poll_interval
        self._get_token_pct = get_token_pct

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Indices of rules that have already fired — prevents re-firing.
        self._fired: set[int] = set()

        # SHA-based dedup: set of "<rule_idx>:<commit_sha>" strings that have
        # already triggered a milestone.  Prevents re-firing when the same
        # commit is seen across multiple polling windows.
        self._fired_milestones: set[str] = set()

        # Last detected phase number (0 = none yet).
        self._current_phase: int = 0

        # Last detected triggering commit SHA (empty string = none yet).
        self._current_trigger_sha: str = ""

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background polling thread."""
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="model-watchdog",
        )
        self._thread.start()
        logger.info(
            "ModelWatchdog started (poll_interval=%.1fs, %d rule(s)).",
            self._poll_interval,
            len(self._rules),
        )

    def stop(self) -> None:
        """Signal the background thread to stop and join it (up to 5 s)."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        logger.info("ModelWatchdog stopped (phase at stop: %d).", self._current_phase)

    @property
    def current_phase(self) -> int:
        """Last detected phase number from git log.  0 = none detected yet."""
        return self._current_phase

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("ModelWatchdog._tick() raised unexpectedly — continuing.")
            self._stop_event.wait(timeout=self._poll_interval)

    def _tick(self) -> None:
        """One poll iteration: detect current phase and evaluate unfired rules."""
        phase, trigger_sha = self._read_current_phase_and_sha()
        self._current_phase = phase
        self._current_trigger_sha = trigger_sha
        token_pct = self._get_token_pct() if self._get_token_pct is not None else 0.0

        for idx, rule in enumerate(self._rules):
            if idx in self._fired:
                continue
            for trigger in rule.triggers:
                if trigger.matches(phase=phase, token_pct=token_pct):
                    self._fire(idx, rule, trigger_sha)
                    break  # only fire once per tick per rule

    def _fire(self, idx: int, rule, trigger_sha: str = "") -> None:
        """Mark a rule as fired and invoke apply_fn.

        SHA-based dedup: if we have a commit SHA, check whether this
        (rule_idx, sha) pair has already fired.  If so, skip silently.
        After firing, record the pair so subsequent polls are no-ops.
        """
        # SHA-based dedup (second guard on top of the index-based _fired set).
        if trigger_sha:
            dedup_key = f"{idx}:{trigger_sha}"
            if dedup_key in self._fired_milestones:
                logger.debug(
                    "[ModelWatchdog] Skipping rule %d — SHA %s already fired.",
                    idx,
                    trigger_sha,
                )
                return
        self._fired.add(idx)
        # Record the (rule, sha) pair so repeated polls of the same commit
        # do not re-fire (SHA-based dedup layer).
        if trigger_sha:
            self._fired_milestones.add(f"{idx}:{trigger_sha}")
        action = rule.action
        reason = action.message or f"PHASE-{self._current_phase}: rule {idx} triggered"
        logger.info(
            "[ModelWatchdog] Firing rule %d → model=%r  reason=%r",
            idx,
            action.model_id,
            reason,
        )
        try:
            self._apply_fn(action.model_id, reason)
        except Exception:  # noqa: BLE001
            logger.exception("ModelWatchdog apply_fn raised for rule %d", idx)

    def _read_current_phase(self) -> int:
        """Return the highest PHASE-N number found in the last 50 git commits.

        Returns the previous value of ``self._current_phase`` on error so that
        a transient git failure does not reset the detected phase.
        """
        phase, _ = self._read_current_phase_and_sha()
        return phase

    def _read_current_phase_and_sha(self) -> tuple[int, str]:
        """Return ``(phase, commit_sha)`` for the highest PHASE-N commit found.

        ``commit_sha`` is the abbreviated SHA of the commit that carries the
        highest phase marker; used for SHA-based milestone dedup.  Returns
        ``(0, "")`` when no git repo or no phase markers are found.

        On error returns ``(self._current_phase, self._current_trigger_sha)``
        to preserve the last known values.
        """
        git_dir = self._working_dir / ".git"
        if not git_dir.exists():
            return 0, ""  # no repo yet; phase stays 0

        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-50", "--format=%h %s"],
                cwd=self._working_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return self._current_phase, self._current_trigger_sha  # keep last known value on error

        if result.returncode != 0:
            return self._current_phase, self._current_trigger_sha

        highest = 0
        highest_sha = ""
        for line in result.stdout.splitlines():
            parts = line.strip().split(" ", 1)
            if len(parts) < 2:
                continue
            sha, subject = parts[0], parts[1]
            m = _PHASE_RE.match(subject.strip())
            if m:
                n = int(m.group(1))
                if n > highest:
                    highest = n
                    highest_sha = sha

        # Never go backwards: if git history is rewritten or the working dir is
        # replaced mid-session, preserve the highest phase we have ever seen.
        if highest > self._current_phase:
            return highest, highest_sha
        return self._current_phase, self._current_trigger_sha
