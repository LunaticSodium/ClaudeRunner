"""
claude_runner/supervisor_audit.py

Append-only audit log helpers for the Supervisor Protocol.

All audit writing is done exclusively by Python scripts — never by
Claude Code directly.

Two audit files are managed:
  audit/supervisor_log.md  — All protocol events (timestamped entries)
  audit/self_check_log.md  — Structured self-check results (one entry per check)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# supervisor_log.md helpers
# ---------------------------------------------------------------------------


def append_supervisor_log(audit_dir: Path, event_type: str, detail: str) -> None:
    """Append a timestamped entry to ``audit/supervisor_log.md``.

    Creates ``audit_dir`` and the log file if they do not exist.
    Never overwrites existing content — strictly append-only.

    Parameters
    ----------
    audit_dir:
        Path to the audit directory (e.g. ``Path("audit/")``).
    event_type:
        Short uppercase label, e.g. ``"PROTOCOL_VIOLATION"``, ``"TIMEOUT"``,
        ``"CONFIRM"``, ``"COUNTER_RESET"``.
    detail:
        Human-readable description of the event.
    """
    audit_dir = Path(audit_dir)
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        log_path = audit_dir / "supervisor_log.md"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = f"[{ts}] {event_type}: {detail}\n"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(entry)
        logger.debug("supervisor_log: appended %s event", event_type)
    except Exception as exc:  # noqa: BLE001
        logger.warning("append_supervisor_log failed: %s", exc)


# ---------------------------------------------------------------------------
# self_check_log.md helpers
# ---------------------------------------------------------------------------


def append_self_check_entry(
    audit_dir: Path,
    dash_n: int,
    counter: int,
    limit: int,
    issue: str,
    source: str,
    severity: str,
    recommended_action: str,
    change_made: str,
) -> None:
    """Append a structured entry to ``audit/self_check_log.md``.

    Creates ``audit_dir`` and the log file if they do not exist.
    Each entry is separated by a markdown horizontal rule.

    Parameters
    ----------
    audit_dir:
        Path to the audit directory.
    dash_n:
        The Dash number that just completed.
    counter:
        Current self-check counter value (after increment).
    limit:
        Configured self-check limit.
    issue:
        Concrete drawback, risk, or failure mode identified.
    source:
        Paper / first principles / reasoning source.
    severity:
        ``"low"``, ``"medium"``, or ``"high"``.
    recommended_action:
        ``"ignore"``, ``"log"``, or ``"fix before next Dash"``.
    change_made:
        What Marathon actually did as a result (or "no change made, issue logged only").
    """
    audit_dir = Path(audit_dir)
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        log_path = audit_dir / "self_check_log.md"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry_lines = [
            f"\n---\n",
            f"## Self-Check Entry [{counter}/{limit}] — Dash {dash_n}",
            f"",
            f"**Timestamp:** {ts}",
            f"**Issue found:** {issue}",
            f"**Source:** {source}",
            f"**Severity:** {severity}",
            f"**Recommended action:** {recommended_action}",
            f"**Change made:** {change_made}",
            f"",
        ]
        entry = "\n".join(entry_lines)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(entry)
        logger.debug("self_check_log: appended entry for Dash %d [%d/%d]", dash_n, counter, limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("append_self_check_entry failed: %s", exc)


def count_self_checks(audit_dir: Path) -> int:
    """Count the number of self-check entries in ``audit/self_check_log.md``.

    Counts occurrences of ``## Self-Check Entry`` headers in the log file.
    Returns 0 if the file does not exist or cannot be read.

    Parameters
    ----------
    audit_dir:
        Path to the audit directory.
    """
    audit_dir = Path(audit_dir)
    log_path = audit_dir / "self_check_log.md"
    try:
        if not log_path.exists():
            return 0
        content = log_path.read_text(encoding="utf-8")
        return content.count("## Self-Check Entry")
    except Exception as exc:  # noqa: BLE001
        logger.warning("count_self_checks failed: %s", exc)
        return 0
