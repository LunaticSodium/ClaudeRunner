"""
model_resolver.py — Resolve stale model ID aliases in a ProjectBook.

At project-load time, short or stale model IDs (e.g. ``claude-opus-4-5``)
are substituted with the current canonical IDs using a hardcoded alias table.
This prevents the runner from crashing at runtime when a model alias has
been deprecated or renamed.

Usage (called from preflight.run_preflight())::

    from claude_runner.model_resolver import resolve_model_ids
    updated_book, log_msgs = resolve_model_ids(book)
"""
from __future__ import annotations

import copy
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .project import ProjectBook

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Fully-qualified model ID: claude-<family>-<major>[-<minor>][-<YYYYMMDD>]
# Examples:  claude-haiku-4-5-20251001, claude-sonnet-4-6
_CURRENT_MODEL_RE = re.compile(r"^claude-[a-z]+-\d+(?:-\d+)?(?:-\d{8})?$")

# ---------------------------------------------------------------------------
# Alias table
#
# Maps short/stale IDs → current canonical IDs.
# Keys that already map to themselves are "canonical" (no change required).
# All lookups are exact string matches; no fuzzy matching.
# ---------------------------------------------------------------------------

_KNOWN_ALIASES: dict[str, str] = {
    # Opus
    "claude-opus-4-6": "claude-opus-4-6",          # canonical
    "claude-opus-4-5": "claude-opus-4-6",           # stale → current

    # Sonnet
    "claude-sonnet-4-6": "claude-sonnet-4-6",       # canonical
    "claude-sonnet-4-5": "claude-sonnet-4-6",       # stale → current

    # Haiku
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",        # resolve to full
    "claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001",  # canonical
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_model_ids(project: "ProjectBook") -> tuple["ProjectBook", list[str]]:
    """Resolve stale model IDs in *project*'s model schedule.

    Returns a **copy** of the project book with any substitutions applied and
    a list of human-readable log messages describing each substitution (or
    warning for unknown IDs).

    Parameters
    ----------
    project:
        A :class:`~claude_runner.project.ProjectBook` instance.

    Returns
    -------
    (updated_project, log_messages)
        ``updated_project`` is a deep copy of *project* with substituted IDs.
        ``log_messages`` describes each substitution or warning.
    """
    log_messages: list[str] = []
    schedule = getattr(project, "model_schedule", None)
    if schedule is None:
        return project, log_messages

    rules = getattr(schedule, "rules", []) or []
    if not rules:
        return project, log_messages

    # Deep-copy so we never mutate the original.
    updated = _copy_project(project)
    updated_schedule = getattr(updated, "model_schedule", None)
    if updated_schedule is None:
        return project, log_messages
    updated_rules = getattr(updated_schedule, "rules", []) or []

    from datetime import date  # noqa: PLC0415
    today = date.today().isoformat()

    for rule in updated_rules:
        action = getattr(rule, "action", None)
        if action is None:
            continue
        model_id = getattr(action, "model_id", "") or ""
        if not model_id:
            continue

        if model_id in _KNOWN_ALIASES:
            canonical = _KNOWN_ALIASES[model_id]
            if canonical != model_id:
                action.model_id = canonical
                msg = (
                    f"model_id {model_id!r} resolved to {canonical!r} "
                    f"via alias table on {today}"
                )
                log_messages.append(msg)
                logger.info("[model_resolver] %s", msg)
            # else: already canonical, no change needed
        else:
            # Not in alias table — warn but don't fail.
            msg = f"model_id {model_id!r} is unknown — leaving unchanged"
            log_messages.append(msg)
            logger.warning("[model_resolver] %s", msg)

    return updated, log_messages


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _copy_project(project: "ProjectBook") -> "ProjectBook":
    """Return a deep copy of *project* suitable for mutation.

    Uses ``model_copy(deep=True)`` for Pydantic v2 models; falls back to
    ``copy.deepcopy`` for non-Pydantic objects (e.g. in unit tests).
    """
    if hasattr(project, "model_copy"):
        return project.model_copy(deep=True)
    return copy.deepcopy(project)
