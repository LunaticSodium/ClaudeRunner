"""
Tests for claude_runner.model_resolver.resolve_model_ids().
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from claude_runner.model_resolver import resolve_model_ids, _KNOWN_ALIASES


# ---------------------------------------------------------------------------
# Helpers — mock ProjectBook with model_schedule
# ---------------------------------------------------------------------------


def _action(model_id: str):
    action = MagicMock()
    action.model_id = model_id
    return action


def _rule(model_id: str):
    rule = MagicMock()
    rule.action = _action(model_id)
    return rule


def _schedule(*model_ids: str):
    schedule = MagicMock()
    schedule.rules = [_rule(mid) for mid in model_ids]
    return schedule


def _book(schedule=None):
    book = MagicMock()
    # model_copy(deep=True) must return a mutable copy.
    # We use a plain MagicMock so deepcopy works.
    book.model_schedule = schedule
    # Simulate Pydantic v2 model_copy
    book.model_copy = lambda deep=False: _shallow_copy_book(book)
    return book


def _shallow_copy_book(original):
    """Create a test-friendly copy of a book mock."""
    import copy  # noqa: PLC0415
    copied = copy.deepcopy(original)
    # Re-attach model_copy so further copies work.
    copied.model_copy = lambda deep=False: _shallow_copy_book(copied)
    return copied


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestResolveModelIds:
    def test_no_schedule_returns_original_unchanged(self):
        book = _book(schedule=None)
        result, msgs = resolve_model_ids(book)
        assert msgs == []
        assert result is book  # no copy made

    def test_canonical_id_unchanged(self):
        book = _book(schedule=_schedule("claude-opus-4-6"))
        result, msgs = resolve_model_ids(book)
        # No substitution message for canonical IDs.
        subst_msgs = [m for m in msgs if "resolved" in m]
        assert len(subst_msgs) == 0

    def test_stale_opus_resolved(self):
        book = _book(schedule=_schedule("claude-opus-4-5"))
        result, msgs = resolve_model_ids(book)
        resolved = [m for m in msgs if "resolved" in m]
        assert len(resolved) == 1
        assert "claude-opus-4-5" in resolved[0]
        assert "claude-opus-4-6" in resolved[0]

    def test_stale_sonnet_resolved(self):
        book = _book(schedule=_schedule("claude-sonnet-4-5"))
        result, msgs = resolve_model_ids(book)
        resolved = [m for m in msgs if "resolved" in m]
        assert len(resolved) == 1
        assert "claude-sonnet-4-5" in resolved[0]
        assert "claude-sonnet-4-6" in resolved[0]

    def test_haiku_short_resolved_to_full(self):
        book = _book(schedule=_schedule("claude-haiku-4-5"))
        result, msgs = resolve_model_ids(book)
        resolved = [m for m in msgs if "resolved" in m]
        assert len(resolved) == 1
        assert "claude-haiku-4-5-20251001" in resolved[0]

    def test_unknown_id_warns_and_leaves_unchanged(self):
        book = _book(schedule=_schedule("claude-unknown-99"))
        result, msgs = resolve_model_ids(book)
        unknown_msgs = [m for m in msgs if "unknown" in m]
        assert len(unknown_msgs) == 1
        assert "claude-unknown-99" in unknown_msgs[0]

    def test_multiple_rules_mixed(self):
        book = _book(schedule=_schedule(
            "claude-opus-4-5",        # stale → resolved
            "claude-sonnet-4-6",      # canonical → no msg
            "claude-haiku-4-5",       # short → resolved to full
        ))
        result, msgs = resolve_model_ids(book)
        resolved = [m for m in msgs if "resolved" in m]
        assert len(resolved) == 2  # opus and haiku resolved; sonnet canonical

    def test_original_not_mutated(self):
        """resolve_model_ids must not mutate the input project."""
        book = _book(schedule=_schedule("claude-opus-4-5"))
        original_id = book.model_schedule.rules[0].action.model_id
        resolve_model_ids(book)
        # Original is unchanged.
        assert book.model_schedule.rules[0].action.model_id == original_id

    def test_returns_updated_copy(self):
        """The returned project should have the substituted model ID."""
        book = _book(schedule=_schedule("claude-opus-4-5"))
        result, msgs = resolve_model_ids(book)
        # The result is a different object.
        assert result is not book
        # The updated rule has the resolved ID.
        assert result.model_schedule.rules[0].action.model_id == "claude-opus-4-6"

    def test_log_message_contains_date(self):
        """Substitution messages should include the current date."""
        from datetime import date  # noqa: PLC0415
        today = date.today().isoformat()
        book = _book(schedule=_schedule("claude-opus-4-5"))
        _, msgs = resolve_model_ids(book)
        resolved = [m for m in msgs if "resolved" in m]
        assert any(today in m for m in resolved)


class TestKnownAliasTable:
    def test_canonical_ids_map_to_themselves(self):
        canonical = [
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ]
        for cid in canonical:
            assert _KNOWN_ALIASES.get(cid) == cid, f"{cid!r} should map to itself"

    def test_stale_ids_map_to_current(self):
        assert _KNOWN_ALIASES["claude-opus-4-5"] == "claude-opus-4-6"
        assert _KNOWN_ALIASES["claude-haiku-4-5"] == "claude-haiku-4-5-20251001"
