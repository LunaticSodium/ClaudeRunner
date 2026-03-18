"""Pre-flight checks run before Claude Code subprocess is spawned.

Each check either:
  - raises ``PreflightError`` (hard failure — runner must not launch Claude), or
  - appends a warning string to the returned list (soft warning — run continues).

The checks are intentionally lightweight so they complete in under a second on
the first call.  Network-dependent checks (ntfy reachability) are run only when
the relevant config is present.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .project import ProjectBook

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model-ID format pattern — matches a "current" fully-qualified model ID.
# IDs that do NOT match this are flagged as potentially stale aliases.
# ---------------------------------------------------------------------------
_CURRENT_MODEL_RE = re.compile(r"^claude-[a-z]+-\d+(-\d+)?(-\d{8})?$")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class PreflightError(Exception):
    """Raised by ``run_preflight`` when a hard precondition fails."""


def run_preflight(
    project: "ProjectBook",
    working_dir: Path,
    skip: bool = False,
) -> list[str]:
    """Run all pre-flight checks for *project*.

    Parameters
    ----------
    project:
        The fully-loaded and validated :class:`ProjectBook`.
    working_dir:
        The task's working directory (must exist for the run to proceed).
    skip:
        When ``True`` all checks are skipped and an empty list is returned.
        Also respected when ``project.preflight.skip`` is ``True``.

    Returns
    -------
    list[str]
        Warning messages (soft issues that do not abort the run).

    Raises
    ------
    PreflightError
        When a hard precondition fails (missing working_dir, missing env var).
    """
    # Honour skip flags.
    project_skip = getattr(getattr(project, "preflight", None), "skip", False)
    if skip or project_skip:
        logger.debug("[preflight] Checks skipped (skip=%s, project_skip=%s).", skip, project_skip)
        return []

    warnings: list[str] = []

    # ── Check 1: working_dir exists ────────────────────────────────────────
    wd = Path(working_dir)
    if not wd.exists():
        raise PreflightError(
            f"preflight: working_dir does not exist: {wd!r}.  "
            "Create the directory or correct sandbox.working_dir in the project book."
        )
    logger.debug("[preflight] working_dir exists: %s", wd)

    # ── Check 2: working_dir is a git repo (warn only) ─────────────────────
    if not (wd / ".git").exists():
        warnings.append(
            f"preflight: working_dir {wd!r} is not a git repository.  "
            "Phase-aware model switching and git output collection will be unavailable."
        )
        logger.debug("[preflight] no .git in working_dir — warned.")

    # ── Check 3: model_id alias resolution + format (warn if stale) ──────
    _check_and_resolve_model_ids(project, warnings)

    # ── Check 4: required_env (hard fail if any missing) ──────────────────
    _check_required_env(project, warnings)

    # ── Check 5: ntfy reachability (warn only) ─────────────────────────────
    _check_ntfy(project, warnings)

    if warnings:
        for w in warnings:
            logger.warning("[preflight] %s", w)
    else:
        logger.info("[preflight] All checks passed.")

    return warnings


# ---------------------------------------------------------------------------
# Internal check helpers
# ---------------------------------------------------------------------------


def _check_and_resolve_model_ids(project: "ProjectBook", warnings: list[str]) -> None:
    """Run alias resolution and warn about any unknown/stale model IDs."""
    try:
        from .model_resolver import resolve_model_ids  # noqa: PLC0415
        _, resolver_msgs = resolve_model_ids(project)
        for msg in resolver_msgs:
            warnings.append(f"preflight: {msg}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[preflight] model_resolver raised: %s", exc)
        # Fall back to format-only check.
        _check_model_ids_format(project, warnings)


def _check_model_ids_format(project: "ProjectBook", warnings: list[str]) -> None:
    """Warn if any model_id in the schedule looks like a stale alias (format check only)."""
    schedule = getattr(project, "model_schedule", None)
    if schedule is None:
        return
    for rule in getattr(schedule, "rules", []):
        action = getattr(rule, "action", None)
        if action is None:
            continue
        model_id = getattr(action, "model_id", "") or ""
        if model_id and not _CURRENT_MODEL_RE.match(model_id):
            warnings.append(
                f"preflight: model_id {model_id!r} does not match the expected "
                "canonical format (e.g. 'claude-sonnet-4-6').  "
                "It may be a stale alias — consider running model_id resolution."
            )


def _check_required_env(project: "ProjectBook", warnings: list[str]) -> None:  # noqa: ARG001
    """Hard-fail if any env var in preflight.required_env is missing."""
    preflight_cfg = getattr(project, "preflight", None)
    if preflight_cfg is None:
        return
    required = getattr(preflight_cfg, "required_env", []) or []
    for var in required:
        if not os.environ.get(var, "").strip():
            raise PreflightError(
                f"preflight: required environment variable {var!r} is not set.  "
                "Set it before launching claude-runner."
            )
    if required:
        logger.debug("[preflight] All %d required_env var(s) present.", len(required))


def _check_ntfy(project: "ProjectBook", warnings: list[str]) -> None:
    """Warn if configured ntfy channel is unreachable (do not fail the run)."""
    try:
        _do_check_ntfy(project, warnings)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[preflight] ntfy reachability check raised unexpectedly: %s", exc)


def _do_check_ntfy(project: "ProjectBook", warnings: list[str]) -> None:
    """Inner ntfy check (may raise; wrapped by _check_ntfy)."""
    # Only check if notify config has channels — look for ntfy-style channels
    # by checking secrets config for ntfy configuration.
    notify = getattr(project, "notify", None)
    if notify is None:
        return
    channels = getattr(notify, "channels", []) or []
    if not channels:
        return

    # Attempt to discover ntfy channel name from keyring / secrets.
    channel_name = _find_ntfy_channel()
    if not channel_name:
        return

    try:
        import requests  # noqa: PLC0415
        url = f"https://ntfy.sh/{channel_name}/json?poll=1"
        resp = requests.get(url, timeout=5)
        if resp.status_code >= 500:
            warnings.append(
                f"preflight: ntfy channel {channel_name!r} returned HTTP {resp.status_code} "
                "— notifications may not work."
            )
        else:
            logger.debug("[preflight] ntfy channel %r is reachable (HTTP %d).", channel_name, resp.status_code)
    except Exception as exc:  # noqa: BLE001
        warnings.append(
            f"preflight: ntfy channel {channel_name!r} is unreachable ({exc}).  "
            "Notifications will silently fail.  Check network connectivity."
        )


def _find_ntfy_channel() -> str:
    """Try to read the ntfy channel name from keyring or secrets.yaml."""
    try:
        import keyring  # noqa: PLC0415
        channel = (keyring.get_password("claude-runner/ntfy", "channel") or "").strip()
        if channel:
            return channel
    except Exception:  # noqa: BLE001
        pass

    # Fall back to secrets.yaml
    secrets_path = Path.home() / ".claude-runner" / "secrets.yaml"
    if secrets_path.exists():
        try:
            import yaml  # noqa: PLC0415
            data = yaml.safe_load(secrets_path.read_text(encoding="utf-8")) or {}
            channel = str(data.get("ntfy_channel", "") or "").strip()
            if channel:
                return channel
        except Exception:  # noqa: BLE001
            pass

    return ""
