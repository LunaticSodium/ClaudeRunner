"""
tests/test_rate_limit.py — Unit tests for claude_runner/rate_limit.py

Tests cover RateLimitDetector pattern matching, callback firing, state
management, timestamp validation, and RateLimitWaiter async behaviour.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from claude_runner.rate_limit import (
    RateLimitDetector,
    RateLimitError,
    RateLimitWaiter,
    _MAX_TIMESTAMP,
    _MIN_TIMESTAMP,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_future_ts(offset_seconds: int = 3600) -> int:
    """Return a plausible Unix timestamp offset_seconds into the future."""
    dt = datetime.now(tz=timezone.utc) + timedelta(seconds=offset_seconds)
    return int(dt.timestamp())


# ---------------------------------------------------------------------------
# RateLimitDetector — pattern detection
# ---------------------------------------------------------------------------


class TestRateLimitDetectorPatterns:
    def test_primary_format_detected(self):
        """Primary structured format 'Claude AI usage limit reached|<ts>' is detected."""
        ts = _make_future_ts()
        detector = RateLimitDetector()
        result = detector.feed(f"Claude AI usage limit reached|{ts}")
        assert result is True

    def test_primary_format_parses_timestamp(self):
        """feed() correctly parses the Unix timestamp from the primary format."""
        ts = _make_future_ts(7200)
        detector = RateLimitDetector()
        detector.feed(f"Claude AI usage limit reached|{ts}")
        reset = detector.get_reset_time()
        assert reset is not None
        assert isinstance(reset, datetime)
        assert reset.tzinfo is not None
        # Timestamp should be within a few seconds of expected.
        assert abs(reset.timestamp() - ts) < 2

    def test_prose_variant_usage_limit_reached(self):
        """'usage limit reached ... <ts>' prose variant is detected."""
        ts = _make_future_ts()
        result = RateLimitDetector().feed(f"Your usage limit reached, resets at {ts}.")
        assert result is True

    def test_prose_variant_rate_limit(self):
        """'rate limit ... <ts>' prose variant is detected."""
        ts = _make_future_ts()
        result = RateLimitDetector().feed(f"Rate limit exceeded. Resets at {ts}.")
        assert result is True

    def test_rate_limit_with_dash_variant(self):
        """'ratelimit' (no space) variant is detected."""
        ts = _make_future_ts()
        result = RateLimitDetector().feed(f"ratelimit reset {ts}")
        assert result is True

    def test_interactive_menu_prompt_detected(self):
        """/rate-limit-options interactive menu line is detected (no timestamp)."""
        result = RateLimitDetector().feed("/rate-limit-options")
        assert result is True

    def test_rate_limit_options_text_detected(self):
        """'Rate limit options' text is detected (no timestamp)."""
        result = RateLimitDetector().feed("Rate limit options")
        assert result is True

    def test_normal_output_returns_false(self):
        """Ordinary output lines return False and don't trigger detection."""
        detector = RateLimitDetector()
        assert detector.feed("Building project...") is False
        assert detector.feed("All tests passed.") is False
        assert detector.feed("") is False
        assert detector.feed("No rate limiting here.") is False

    def test_case_insensitive_matching(self):
        """Patterns are case-insensitive."""
        ts = _make_future_ts()
        result = RateLimitDetector().feed(f"RATE LIMIT EXCEEDED {ts}")
        assert result is True


# ---------------------------------------------------------------------------
# RateLimitDetector — callback behaviour
# ---------------------------------------------------------------------------


class TestRateLimitDetectorCallback:
    def test_callback_fired_with_correct_datetime(self):
        """on_rate_limit callback receives a UTC-aware datetime matching the timestamp."""
        ts = _make_future_ts(3600)
        received = []
        detector = RateLimitDetector(on_rate_limit=received.append)
        detector.feed(f"Claude AI usage limit reached|{ts}")
        assert len(received) == 1
        dt = received[0]
        assert isinstance(dt, datetime)
        assert dt.tzinfo is not None
        assert abs(dt.timestamp() - ts) < 2

    def test_callback_fired_only_once(self):
        """Callback is only fired once even if feed() is called multiple times."""
        ts = _make_future_ts()
        call_count = [0]

        def cb(dt):
            call_count[0] += 1

        detector = RateLimitDetector(on_rate_limit=cb)
        line = f"Claude AI usage limit reached|{ts}"
        detector.feed(line)
        detector.feed(line)
        detector.feed(line)
        assert call_count[0] == 1

    def test_feed_returns_true_on_subsequent_calls_when_detected(self):
        """feed() keeps returning True after detection without re-firing callback."""
        ts = _make_future_ts()
        detector = RateLimitDetector()
        detector.feed(f"Claude AI usage limit reached|{ts}")
        # Subsequent calls on unrelated lines still return True.
        assert detector.feed("unrelated output") is True

    def test_fallback_reset_time_when_no_timestamp(self):
        """When no timestamp is in the matched line, a fallback reset time is used."""
        received = []
        detector = RateLimitDetector(on_rate_limit=received.append)
        detector.feed("/rate-limit-options")
        assert len(received) == 1
        dt = received[0]
        assert isinstance(dt, datetime)
        # Fallback is approximately 5 hours from now (Anthropic's rolling window).
        now = datetime.now(tz=timezone.utc)
        delta = (dt - now).total_seconds()
        assert 17900 < delta < 18100  # within a generous 100s window around 5 h

    def test_callback_exception_does_not_propagate(self):
        """Exceptions in the callback are caught and don't crash the detector."""
        def bad_callback(dt):
            raise RuntimeError("callback failure")

        ts = _make_future_ts()
        detector = RateLimitDetector(on_rate_limit=bad_callback)
        # Should not raise.
        result = detector.feed(f"Claude AI usage limit reached|{ts}")
        assert result is True


# ---------------------------------------------------------------------------
# RateLimitDetector — reset() re-arms the detector
# ---------------------------------------------------------------------------


class TestRateLimitDetectorReset:
    def test_reset_clears_detected_state(self):
        ts = _make_future_ts()
        detector = RateLimitDetector()
        detector.feed(f"Claude AI usage limit reached|{ts}")
        assert detector.is_detected() is True
        detector.reset()
        assert detector.is_detected() is False
        assert detector.get_reset_time() is None

    def test_reset_allows_callback_to_fire_again(self):
        """After reset(), a new matching line fires the callback again."""
        ts = _make_future_ts()
        calls = []
        detector = RateLimitDetector(on_rate_limit=calls.append)
        detector.feed(f"Claude AI usage limit reached|{ts}")
        detector.reset()
        detector.feed(f"Claude AI usage limit reached|{ts}")
        assert len(calls) == 2

    def test_is_detected_initially_false(self):
        assert RateLimitDetector().is_detected() is False


# ---------------------------------------------------------------------------
# Timestamp validation
# ---------------------------------------------------------------------------


class TestTimestampValidation:
    def test_timestamp_below_min_uses_fallback(self):
        """A timestamp below _MIN_TIMESTAMP is out of range; fallback is used."""
        calls = []
        detector = RateLimitDetector(on_rate_limit=calls.append)
        # Use a timestamp just below the minimum (year 2019).
        too_old = _MIN_TIMESTAMP - 1
        detector.feed(f"Claude AI usage limit reached|{too_old}")
        # Should still detect (pattern matched), but reset time is fallback.
        assert detector.is_detected() is True
        dt = calls[0]
        now = datetime.now(tz=timezone.utc)
        # Fallback is ~1 hour from now, not year 2019.
        assert (dt - now).total_seconds() > 3000

    def test_timestamp_above_max_uses_fallback(self):
        """A timestamp above _MAX_TIMESTAMP (year 2100+) uses fallback."""
        calls = []
        detector = RateLimitDetector(on_rate_limit=calls.append)
        too_far = _MAX_TIMESTAMP + 1
        detector.feed(f"Claude AI usage limit reached|{too_far}")
        assert detector.is_detected() is True
        dt = calls[0]
        now = datetime.now(tz=timezone.utc)
        assert (dt - now).total_seconds() > 3000

    def test_plausible_timestamp_used_directly(self):
        """A timestamp within the plausible range is used as-is."""
        ts = _make_future_ts(7200)
        detector = RateLimitDetector()
        detector.feed(f"Claude AI usage limit reached|{ts}")
        reset = detector.get_reset_time()
        assert abs(reset.timestamp() - ts) < 2


# ---------------------------------------------------------------------------
# RateLimitWaiter — async wait and tick behaviour
# ---------------------------------------------------------------------------


class TestRateLimitWaiter:
    @pytest.mark.asyncio
    async def test_waiter_completes_and_fires_on_resume(self):
        """Waiter fires on_resume after the reset time passes."""
        # Set reset_at to 10 ms in the future; use tiny tick interval.
        reset_at = datetime.now(tz=timezone.utc) + timedelta(milliseconds=50)
        ticks = []
        resumes = []

        waiter = RateLimitWaiter(
            reset_at=reset_at,
            on_tick=ticks.append,
            on_resume=lambda: resumes.append(True),
            tick_interval=0.02,   # 20 ms ticks for speed
            buffer_seconds=0.0,
        )
        await waiter.wait()
        assert len(resumes) == 1

    @pytest.mark.asyncio
    async def test_waiter_fires_ticks(self):
        """Waiter fires on_tick at least once before completing."""
        reset_at = datetime.now(tz=timezone.utc) + timedelta(milliseconds=100)
        ticks = []

        waiter = RateLimitWaiter(
            reset_at=reset_at,
            on_tick=ticks.append,
            on_resume=lambda: None,
            tick_interval=0.02,
            buffer_seconds=0.0,
        )
        await waiter.wait()
        assert len(ticks) >= 1
        # Each tick value is remaining_seconds (a float > 0 at time of tick).
        for remaining in ticks:
            assert isinstance(remaining, float)

    @pytest.mark.asyncio
    async def test_waiter_past_reset_resumes_immediately(self):
        """When reset_at is already in the past, waiter resumes immediately."""
        reset_at = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
        resumes = []

        waiter = RateLimitWaiter(
            reset_at=reset_at,
            on_tick=lambda r: None,
            on_resume=lambda: resumes.append(True),
            tick_interval=1.0,
            buffer_seconds=0.0,
        )
        await waiter.wait()
        assert len(resumes) == 1

    @pytest.mark.asyncio
    async def test_cancel_prevents_on_resume(self):
        """Cancelling the waiter before completion prevents on_resume firing."""
        reset_at = datetime.now(tz=timezone.utc) + timedelta(seconds=300)
        resumes = []

        waiter = RateLimitWaiter(
            reset_at=reset_at,
            on_tick=lambda r: None,
            on_resume=lambda: resumes.append(True),
            tick_interval=0.05,
            buffer_seconds=0.0,
        )

        async def cancel_soon():
            await asyncio.sleep(0.02)
            waiter.cancel()

        await asyncio.gather(waiter.wait(), cancel_soon())
        assert len(resumes) == 0

    def test_naive_datetime_raises(self):
        """reset_at without tzinfo should raise ValueError."""
        naive_dt = datetime(2030, 1, 1, 12, 0, 0)  # no tzinfo
        with pytest.raises(ValueError, match="timezone-aware"):
            RateLimitWaiter(
                reset_at=naive_dt,
                on_tick=lambda r: None,
                on_resume=lambda: None,
            )

    def test_reset_at_property(self):
        """The reset_at property returns the datetime passed at construction."""
        dt = datetime(2030, 6, 1, tzinfo=timezone.utc)
        waiter = RateLimitWaiter(
            reset_at=dt,
            on_tick=lambda r: None,
            on_resume=lambda: None,
        )
        assert waiter.reset_at is dt

    def test_tick_interval_clamped_to_minimum(self):
        """tick_interval below 1 s is clamped to 1 s."""
        dt = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        waiter = RateLimitWaiter(
            reset_at=dt,
            on_tick=lambda r: None,
            on_resume=lambda: None,
            tick_interval=0.0,  # below minimum
        )
        assert waiter._tick_interval == 1.0

    def test_buffer_seconds_clamped_to_zero(self):
        """Negative buffer_seconds is clamped to 0."""
        dt = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        waiter = RateLimitWaiter(
            reset_at=dt,
            on_tick=lambda r: None,
            on_resume=lambda: None,
            buffer_seconds=-99,
        )
        assert waiter._buffer_seconds == 0.0


# ---------------------------------------------------------------------------
# RateLimitError
# ---------------------------------------------------------------------------


class TestRateLimitError:
    def test_attributes(self):
        dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        err = RateLimitError("too many waits", waits_exhausted=5, reset_at=dt)
        assert err.waits_exhausted == 5
        assert err.reset_at is dt
        assert "too many waits" in str(err)

    def test_is_exception(self):
        with pytest.raises(RateLimitError):
            raise RateLimitError("bang")
