"""
rate_limit.py — Rate limit detection and wait/resume logic for claude-runner.

Claude Code emits a structured message when the Anthropic API usage limit is
reached.  Typical formats seen in the wild:

    Claude AI usage limit reached|<unix_timestamp>
    Claude AI usage limit reached — resets at <unix_timestamp>
    Rate limit exceeded. Resets at 1712345678.

This module provides:

RateLimitDetector
    Scans every clean (ANSI-stripped) output line from the subprocess.
    On a positive match it parses the reset timestamp and fires the optional
    ``on_rate_limit`` callback.

RateLimitWaiter
    Given a reset ``datetime``, sleeps (asynchronously) until the quota
    window expires.  Fires ``on_tick(remaining_seconds)`` every 30 s so a
    TUI layer can display a live countdown, and fires ``on_resume()`` when
    the wait completes.

RateLimitError
    Raised by higher-level orchestration code when the configured maximum
    number of consecutive rate-limit waits has been exceeded.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Runner protocol markers — these are injected into every initial prompt by
# context_manager.RUNNER_PROTOCOL.  They are checked *first* so the detector
# can distinguish a deliberate task-complete / task-error signal from a
# rate-limit event.  The caller must inspect ``matched_runner_complete`` or
# ``matched_runner_error`` to determine which type of match fired.
#
# Each pattern must either:
#   - Contain a capture group ``(\d{10,})`` that yields a Unix timestamp, OR
#   - Contain no capture group, indicating a "soft" signal where no timestamp
#     can be extracted.

RUNNER_COMPLETE_PATTERN: str = r"##RUNNER:COMPLETE##"
RUNNER_ERROR_PATTERN: str = r"##RUNNER:ERROR:(.+)##"

RATE_LIMIT_PATTERNS: list[str] = [
    # Primary: runner protocol markers (checked before any rate-limit pattern)
    RUNNER_COMPLETE_PATTERN,
    RUNNER_ERROR_PATTERN,
    # Fallback: structured rate-limit format "Claude AI usage limit reached|<ts>"
    r"Claude AI usage limit reached\|(\d{10,})",
    # Prose variants emitted by different Claude Code versions
    r"usage limit reached[^\d]*(\d{10,})",
    r"rate.?limit[^\d]*(\d{10,})",
    # Interactive menu prompts (no timestamp embedded)
    r"/rate-limit-options",
    r"Rate limit options",
]

_COMPILED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in RATE_LIMIT_PATTERNS
]

# Pre-compiled runner marker patterns for fast isinstance checks in the detector.
_RUNNER_COMPLETE_RE = re.compile(RUNNER_COMPLETE_PATTERN)
_RUNNER_ERROR_RE = re.compile(RUNNER_ERROR_PATTERN)

# Minimum plausible Unix timestamp: 2020-01-01T00:00:00Z
_MIN_TIMESTAMP = 1_577_836_800
# Maximum plausible timestamp: ~year 2100
_MAX_TIMESTAMP = 4_102_444_800


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RateLimitError(Exception):
    """
    Raised when the maximum number of consecutive rate-limit waits has been
    exceeded and the orchestrator should abort the current task.

    Attributes
    ----------
    waits_exhausted : int
        How many waits were attempted before giving up.
    reset_at : datetime | None
        The last detected reset time, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        waits_exhausted: int = 0,
        reset_at: Optional[datetime] = None,
    ) -> None:
        super().__init__(message)
        self.waits_exhausted = waits_exhausted
        self.reset_at = reset_at

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"RateLimitError({str(self)!r}, "
            f"waits_exhausted={self.waits_exhausted}, "
            f"reset_at={self.reset_at!r})"
        )


# ---------------------------------------------------------------------------
# RateLimitDetector
# ---------------------------------------------------------------------------


class RateLimitDetector:
    """
    Scans Claude Code output lines for rate limit messages.

    Usage
    -----
    ::

        def handle_rate_limit(reset_at: datetime) -> None:
            print(f"Rate limited — resets at {reset_at}")

        detector = RateLimitDetector(on_rate_limit=handle_rate_limit)

        # Inside the on_line callback of ClaudeProcess:
        if detector.feed(clean_line):
            reset_time = detector.get_reset_time()

    Parameters
    ----------
    on_rate_limit:
        Optional callback fired once per detection event:
            on_rate_limit(reset_at: datetime)
        When no timestamp is extractable from the matched line, ``reset_at``
        is set to a short fallback (``_FALLBACK_WAIT_SECONDS`` from now) so
        the waiter always has a concrete target.
    """

    # Fallback wait when no timestamp is found in the message (seconds).
    # 5 hours — Anthropic's usage limits typically reset on a 5-hour rolling window.
    _FALLBACK_WAIT_SECONDS: float = 18_000.0

    def __init__(
        self,
        on_rate_limit: Optional[Callable[[datetime], None]] = None,
    ) -> None:
        self._on_rate_limit = on_rate_limit
        self._reset_at: Optional[datetime] = None
        self._detected: bool = False
        # Set when a ##RUNNER:COMPLETE## marker is matched.
        self._runner_complete: bool = False
        # Set to the error description when a ##RUNNER:ERROR:...## marker is matched.
        self._runner_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, clean_line: str) -> bool:
        """
        Inspect *clean_line* (ANSI-stripped) for runner protocol markers and
        rate-limit indicators.

        Returns True (and fires ``on_rate_limit`` for rate-limit matches) on
        the **first** match.  Subsequent calls keep returning True while
        ``_detected`` is set but the callback is only fired once per detection
        event.  Call :meth:`reset` to re-arm the detector for the next task.

        Use :attr:`matched_runner_complete` and :attr:`matched_runner_error`
        to distinguish runner-protocol matches from rate-limit matches.
        """
        if self._detected:
            # Already in a detected state; keep signalling but don't refire.
            return True

        # --- Check runner protocol markers first (highest priority) ----------
        m_complete = _RUNNER_COMPLETE_RE.search(clean_line)
        if m_complete is not None:
            log.info("Runner protocol: ##RUNNER:COMPLETE## detected in output.")
            self._runner_complete = True
            self._detected = True
            return True

        m_error = _RUNNER_ERROR_RE.search(clean_line)
        if m_error is not None:
            description = m_error.group(1).strip()
            log.info("Runner protocol: ##RUNNER:ERROR## detected — %r", description)
            self._runner_error = description
            self._detected = True
            return True

        # --- Fallback: rate-limit patterns -----------------------------------
        for pattern in _COMPILED_PATTERNS:
            # Skip runner marker patterns already handled above.
            if pattern.pattern in (RUNNER_COMPLETE_PATTERN, RUNNER_ERROR_PATTERN):
                continue

            match = pattern.search(clean_line)
            if match is None:
                continue

            # --- Attempt to extract a Unix timestamp ---
            reset_at = self._parse_timestamp(match)
            if reset_at is None:
                reset_at = self._fallback_reset_time()
                log.debug(
                    "Rate limit detected (no timestamp in line); using fallback reset=%s",
                    reset_at.isoformat(),
                )
            else:
                log.info(
                    "Rate limit detected; reset_at=%s (pattern=%r)",
                    reset_at.isoformat(),
                    pattern.pattern,
                )

            self._reset_at = reset_at
            self._detected = True

            if self._on_rate_limit is not None:
                try:
                    self._on_rate_limit(reset_at)
                except Exception as cb_exc:
                    log.warning("on_rate_limit callback raised: %s", cb_exc)

            return True

        return False

    def get_reset_time(self) -> Optional[datetime]:
        """Return the parsed reset ``datetime`` (UTC) if a rate limit was detected."""
        return self._reset_at

    def is_detected(self) -> bool:
        """Return True if any match has been detected since the last :meth:`reset`."""
        return self._detected

    @property
    def matched_runner_complete(self) -> bool:
        """True if ``##RUNNER:COMPLETE##`` was detected in the output stream."""
        return self._runner_complete

    @property
    def matched_runner_error(self) -> Optional[str]:
        """
        The error description extracted from ``##RUNNER:ERROR:<description>##``,
        or ``None`` if no such marker was detected.
        """
        return self._runner_error

    def is_rate_limit(self) -> bool:
        """
        True if the match was a rate-limit signal (not a runner protocol marker).

        Use this to distinguish between a rate-limit wait and a runner
        completion/error event when :meth:`feed` returns True.
        """
        return self._detected and not self._runner_complete and self._runner_error is None

    def reset(self) -> None:
        """
        Clear detection state so the detector can be re-armed for the next
        task run.  Useful when the orchestrator retries after a wait.
        """
        self._detected = False
        self._reset_at = None
        self._runner_complete = False
        self._runner_error = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_timestamp(match: re.Match[str]) -> Optional[datetime]:
        """
        Extract the first capture group from *match* and convert it to a
        UTC-aware ``datetime``.  Returns None if no group was captured or
        the value is out of the plausible range.
        """
        groups = match.groups()
        if not groups:
            return None
        raw = groups[0]
        if raw is None:
            return None
        try:
            ts = int(raw)
        except (ValueError, TypeError):
            return None

        if not (_MIN_TIMESTAMP <= ts <= _MAX_TIMESTAMP):
            log.debug("Captured timestamp %d is outside plausible range — ignoring", ts)
            return None

        return datetime.fromtimestamp(ts, tz=timezone.utc)

    def _fallback_reset_time(self) -> datetime:
        """Return ``now + _FALLBACK_WAIT_SECONDS`` as a UTC datetime."""
        return datetime.fromtimestamp(
            time.time() + self._FALLBACK_WAIT_SECONDS,
            tz=timezone.utc,
        )


# ---------------------------------------------------------------------------
# RateLimitWaiter
# ---------------------------------------------------------------------------


class RateLimitWaiter:
    """
    Asynchronously waits for a rate-limit window to expire.

    Behaviour
    ---------
    - Sleeps until ``reset_at`` (UTC), checking progress every
      ``tick_interval`` seconds (default 30 s).
    - Calls ``on_tick(remaining_seconds: float)`` on every check so that a
      TUI layer can render a live countdown.
    - Calls ``on_resume()`` exactly once when the wait completes normally.
    - :meth:`cancel` can be called from any coroutine / thread to abort the
      wait early; in that case ``on_resume`` is **not** called.

    Parameters
    ----------
    reset_at:
        UTC-aware ``datetime`` at which the rate limit resets.
    on_tick:
        Callback invoked every ``tick_interval`` seconds with the number of
        seconds remaining.  Signature: ``on_tick(remaining_seconds: float)``.
    on_resume:
        Callback invoked once when the wait completes.
        Signature: ``on_resume()``.
    tick_interval:
        How often (in seconds) to fire ``on_tick`` and re-evaluate the
        remaining wait.  Defaults to 30 s.
    buffer_seconds:
        Extra seconds added on top of the calculated wait to account for
        clock skew between the local machine and Anthropic's servers.
        Defaults to 5 s.
    """

    _DEFAULT_TICK_INTERVAL: float = 30.0
    _DEFAULT_BUFFER_SECONDS: float = 5.0

    def __init__(
        self,
        reset_at: datetime,
        on_tick: Callable[[float], None],
        on_resume: Callable[[], None],
        *,
        tick_interval: float = _DEFAULT_TICK_INTERVAL,
        buffer_seconds: float = _DEFAULT_BUFFER_SECONDS,
    ) -> None:
        if reset_at.tzinfo is None:
            raise ValueError("reset_at must be a timezone-aware datetime (use UTC)")
        self._reset_at = reset_at
        self._on_tick = on_tick
        self._on_resume = on_resume
        self._tick_interval = max(1.0, float(tick_interval))
        self._buffer_seconds = max(0.0, float(buffer_seconds))
        self._cancelled = False
        self._cancel_event: Optional[asyncio.Event] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def wait(self) -> None:
        """
        Async entry point.  Suspends the current coroutine until the rate
        limit has reset (or :meth:`cancel` is called).

        This method is safe to call in any asyncio event loop.
        """
        self._cancel_event = asyncio.Event()

        total_wait = self._seconds_until_reset() + self._buffer_seconds

        if total_wait <= 0:
            log.info("Rate limit reset time is already in the past — resuming immediately")
            self._fire_resume()
            return

        log.info(
            "Rate limit wait started: reset_at=%s, total_wait=%.1fs",
            self._reset_at.isoformat(),
            total_wait,
        )

        elapsed = 0.0
        while not self._cancelled:
            remaining = self._seconds_until_reset() + self._buffer_seconds
            if remaining <= 0:
                break

            # Fire tick callback
            self._fire_tick(remaining)

            # Sleep for the smaller of tick_interval or remaining time,
            # but also watch for cancellation.
            sleep_for = min(self._tick_interval, remaining)
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._cancel_event.wait()),
                    timeout=sleep_for,
                )
                # If we reach here, the cancel event was set.
                log.info("RateLimitWaiter cancelled after %.1fs", elapsed)
                return
            except asyncio.TimeoutError:
                pass  # Normal path — tick interval elapsed

            elapsed += sleep_for

        if not self._cancelled:
            log.info("Rate limit wait complete after %.1fs", elapsed)
            self._fire_resume()

    def cancel(self) -> None:
        """
        Signal the waiter to stop early.  Thread-safe.

        After :meth:`cancel` is called, the ``on_resume`` callback will
        **not** be fired.
        """
        self._cancelled = True
        if self._cancel_event is not None:
            # asyncio.Event.set() is not thread-safe; schedule it properly.
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.call_soon_threadsafe(self._cancel_event.set)
                else:
                    self._cancel_event.set()
            except RuntimeError:
                # No running loop — just set directly (called from sync context)
                self._cancel_event.set()

    @property
    def reset_at(self) -> datetime:
        """The UTC datetime at which the rate limit is expected to reset."""
        return self._reset_at

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _seconds_until_reset(self) -> float:
        """Seconds remaining until ``reset_at`` (may be negative if overdue)."""
        now = datetime.now(tz=timezone.utc)
        return (self._reset_at - now).total_seconds()

    def _fire_tick(self, remaining: float) -> None:
        try:
            self._on_tick(remaining)
        except Exception as exc:
            log.warning("on_tick callback raised: %s", exc)

    def _fire_resume(self) -> None:
        try:
            self._on_resume()
        except Exception as exc:
            log.warning("on_resume callback raised: %s", exc)
