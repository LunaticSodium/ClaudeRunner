"""
claude_runner/supervisor_protocol.py

Supervisor Protocol — hardcoded marathon behavioral layer.

All mechanisms in this module run as Python code only.  Claude Code never
writes directly to ntfy channels or audit logs; all such actions are
performed here by the Python script layer.

Activated by ``supervisor_protocol: enabled: true`` in the project book.
Once activated, all protocol rules are mandatory and cannot be overridden.

v2.0 additions:
- SupervisorBudget: point-based budget system with dual-channel enforcement
- Faux-alarm escalation: panic messages injected at point thresholds
- Intake validation: LLM-driven evaluation against preset file
- Process resource check: hard gate before any intervention
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .project import SupervisorProtocolConfig

logger = logging.getLogger(__name__)

# Marker used in pending.md to indicate a self-check response from Claude Code.
_SELF_CHECK_RESPONSE_MARKER = "## SELF-CHECK RESPONSE"

# How often (seconds) to poll pending.md for a self-check response.
_POLL_INTERVAL_S: float = 5.0

# How long (seconds) to wait for a self-check response from Claude Code.
_SELF_CHECK_TIMEOUT_S: float = 600.0  # 10 minutes


class ProtocolViolationError(Exception):
    """Raised when a caller attempts a protocol-violating operation."""


class SupervisorProtocol:
    """
    Supervisor Protocol — hardcoded behavioral layer for Marathon.

    Responsibilities:
    - Channel discipline enforcement
    - Human input confirmation gate (confirm/timeout loop)
    - Self-check trigger after each Dash
    - Audit log management
    - Protocol violation handling

    Parameters
    ----------
    config:
        Validated ``SupervisorProtocolConfig`` from the project book.
    project_id:
        Project identifier (YAML stem), used in log messages.
    ntfy_client:
        An :class:`~claude_runner.ntfy_client.NtfyClient` instance (or mock).
        May be ``None`` — protocol operates without ntfy when absent.
    working_dir:
        Working directory for the running Marathon project.  The audit
        subdirectory is resolved relative to this path.
    """

    def __init__(
        self,
        config: SupervisorProtocolConfig,
        project_id: str,
        ntfy_client,
        working_dir: Path,
    ) -> None:
        self._config = config
        self._project_id = project_id
        self._ntfy = ntfy_client
        self._working_dir = Path(working_dir)
        self._audit_dir = self._working_dir / config.audit_dir

        # In-memory self-check counter (source of truth for this session).
        # Persisted state is read from audit/self_check_log.md entry count.
        self._self_check_counter: int = 0

        # Set to True by handle_violation(); callers should halt the Dash.
        self._halt_requested: bool = False

    # ------------------------------------------------------------------
    # Channel discipline
    # ------------------------------------------------------------------

    def validate_channel_write(self, channel: str, caller: str) -> None:
        """Enforce write-channel discipline.

        Only ``"marathon"`` is permitted to write to ntfy channels.
        Any other caller triggers a protocol violation.

        Parameters
        ----------
        channel:
            Logical channel name (``"out"`` or ``"cmd"``).
        caller:
            Identifier of the caller attempting the write.

        Raises
        ------
        ProtocolViolationError
            If *caller* is not ``"marathon"`` and *channel* is an ntfy channel.
        """
        ntfy_channels = {"out", "cmd"}
        if caller != "marathon" and channel in ntfy_channels:
            detail = (
                f"Caller {caller!r} attempted to write to ntfy channel {channel!r}. "
                "Only 'marathon' may write to ntfy channels."
            )
            self.log_event("PROTOCOL_VIOLATION", detail)
            raise ProtocolViolationError(detail)

    # ------------------------------------------------------------------
    # Human input confirmation gate
    # ------------------------------------------------------------------

    def wait_for_confirm(self, intent_message: str) -> bool:
        """Broadcast intent and wait for a ``confirm`` reply.

        Flow:
        1. Publish *intent_message* to the out ntfy channel.
        2. Poll the cmd channel every 5 seconds.
        3. If next message body is exactly ``"confirm"`` (case-insensitive,
           stripped) → return ``True``.
        4. If anything else → publish "please send confirm to proceed" and
           continue waiting.
        5. On timeout → publish "input expired, no action taken", log, return
           ``False``.

        Parameters
        ----------
        intent_message:
            Description of what Marathon would do.  Sent to the out channel.

        Returns
        -------
        bool
            ``True`` if confirmed, ``False`` on timeout.
        """
        self._publish("out", intent_message, title="Supervisor Protocol — awaiting confirm")
        self.log_event("AWAIT_CONFIRM", f"intent={intent_message!r}")

        timeout_s = self._config.confirm_timeout_minutes * 60
        deadline = time.monotonic() + timeout_s

        while time.monotonic() < deadline:
            # Poll cmd channel for next message.
            msgs = self._poll_cmd()
            for msg in msgs:
                body = msg.message.strip().lower()
                if body == "confirm":
                    self.log_event("CONFIRMED", f"intent={intent_message!r}")
                    return True
                else:
                    # Non-confirm reply — re-prompt.
                    self._publish(
                        "out",
                        "please send confirm to proceed",
                        title="Supervisor Protocol",
                    )
                    self.log_event(
                        "NON_CONFIRM_REPLY",
                        f"received={msg.message!r}; re-prompting",
                    )
            time.sleep(_POLL_INTERVAL_S)

        # Timeout.
        self._publish(
            "out",
            "input expired, no action taken",
            title="Supervisor Protocol — timeout",
        )
        self.log_event("CONFIRM_TIMEOUT", f"intent={intent_message!r}")
        return False

    # ------------------------------------------------------------------
    # Self-check trigger
    # ------------------------------------------------------------------

    def trigger_self_check(self, dash_n: int) -> None:
        """Inject a self-check request into pending.md after Dash *dash_n* completes.

        If the self-check counter is at the configured limit, publish a
        limit-reached message and skip.

        Otherwise:
        1. Increment counter.
        2. Write self-check request to ``pending.md`` via ``inbox.append_message``.
        3. Poll ``pending.md`` for Claude Code's response (timeout 10 min).
        4. Parse result and append to ``audit/self_check_log.md``.
        5. Publish Decision and Action messages to the out ntfy channel.

        Parameters
        ----------
        dash_n:
            The Dash number that just completed (1-based).
        """
        limit = self._config.self_check_limit

        # Sync in-memory counter with persisted state (in case of restart).
        persisted = self._count_persisted_checks()
        if self._self_check_counter < persisted:
            self._self_check_counter = persisted

        if self._self_check_counter >= limit:
            self._publish(
                "out",
                f"Self-check limit reached [{limit}/{limit}]\nSelf-checks disabled until reset.",
                title="Supervisor Protocol — limit reached",
            )
            self.log_event(
                "SELF_CHECK_LIMIT_REACHED",
                f"counter={self._self_check_counter}, limit={limit}",
            )
            return

        # Increment counter.
        self._self_check_counter += 1
        counter = self._self_check_counter

        self.log_event(
            "SELF_CHECK_TRIGGERED",
            f"dash={dash_n}, counter={counter}/{limit}",
        )

        # Write self-check request to pending.md.
        request_text = (
            f"## SELF-CHECK REQUEST — Dash {dash_n}\n"
            "Perform web research on the current system state. "
            "You MUST find at least one concrete drawback, risk, or failure mode. "
            '"No issues found" is not a valid response. '
            "If nothing found via web search, generate one from first principles. "
            "Respond with: issue, source, severity (low/medium/high), "
            "recommended action (ignore/log/fix before next Dash).\n\n"
            f"{_SELF_CHECK_RESPONSE_MARKER}\n"
            "(Claude Code: write your response starting on the next line)\n"
        )
        try:
            from . import inbox  # noqa: PLC0415
            inbox.append_message(request_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("trigger_self_check: failed to write to inbox: %s", exc)
            return

        # Poll pending.md for Claude Code's response.
        result = self._wait_for_self_check_response(dash_n)

        if result is None:
            self.log_event(
                "SELF_CHECK_TIMEOUT",
                f"dash={dash_n}, counter={counter}/{limit}",
            )
            self._publish(
                "out",
                f"SELF-CHECK — Dash {dash_n} timed out [{counter}/{limit}]\n"
                "No response received within timeout.",
                title="Supervisor Protocol — self-check timeout",
            )
            return

        # Parse result fields (tolerant parsing with fallbacks).
        issue = result.get("issue", "Unknown issue")
        source = result.get("source", "Unknown source")
        severity = result.get("severity", "unknown")
        recommended_action = result.get("recommended_action", "log")
        change_made = result.get("change_made", "no change made, issue logged only")

        # Append structured entry to audit/self_check_log.md.
        try:
            from .supervisor_audit import append_self_check_entry  # noqa: PLC0415
            append_self_check_entry(
                audit_dir=self._audit_dir,
                dash_n=dash_n,
                counter=counter,
                limit=limit,
                issue=issue,
                source=source,
                severity=severity,
                recommended_action=recommended_action,
                change_made=change_made,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("trigger_self_check: failed to write audit entry: %s", exc)

        # Message 1 — Decision.
        decision_msg = (
            f"SELF-CHECK — Dash {dash_n} complete [{counter}/{limit}]\n"
            "─────────────────────────────\n"
            f"Issue found: {issue}\n"
            f"Source: {source}\n"
            f"Severity: {severity}\n"
            f"Recommended action: {recommended_action}"
        )
        self._publish("out", decision_msg, title="Supervisor Protocol — self-check decision")

        # Message 2 — Action.
        action_msg = (
            f"SELF-CHECK — Dash {dash_n} action [{counter}/{limit}]\n"
            "─────────────────────────────\n"
            f"Change made: {change_made}\n"
            "Logged to: audit/self_check_log.md"
        )
        self._publish("out", action_msg, title="Supervisor Protocol — self-check action")

        self.log_event(
            "SELF_CHECK_COMPLETE",
            f"dash={dash_n}, counter={counter}/{limit}, severity={severity}",
        )

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------

    def log_event(self, event_type: str, detail: str) -> None:
        """Append a timestamped entry to ``audit/supervisor_log.md``.

        Creates the audit directory if it does not exist.

        Parameters
        ----------
        event_type:
            Short uppercase event label.
        detail:
            Human-readable description.
        """
        try:
            from .supervisor_audit import append_supervisor_log  # noqa: PLC0415
            append_supervisor_log(self._audit_dir, event_type, detail)
        except Exception as exc:  # noqa: BLE001
            logger.warning("log_event failed: %s", exc)

    # ------------------------------------------------------------------
    # Protocol violation handler
    # ------------------------------------------------------------------

    def handle_violation(self, detail: str) -> None:
        """Log a protocol violation, notify via ntfy, and set the halt flag.

        Parameters
        ----------
        detail:
            Description of the violation.
        """
        self.log_event("PROTOCOL_VIOLATION", detail)
        self._publish(
            "out",
            f"[PROTOCOL VIOLATION] {detail}",
            title="Supervisor Protocol — VIOLATION",
        )
        self._halt_requested = True
        logger.warning("Protocol violation: %s", detail)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _publish(self, channel: str, message: str, title: str = "") -> None:
        """Publish to ntfy channel; silently no-op if ntfy is unavailable."""
        if self._ntfy is None:
            logger.debug("_publish: no ntfy client — skipping: %s", message[:80])
            return
        try:
            self._ntfy.publish(channel, message, title=title)
        except Exception as exc:  # noqa: BLE001
            logger.warning("_publish to %r failed: %s", channel, exc)

    def _poll_cmd(self):
        """Poll the cmd ntfy channel once; return list of messages."""
        if self._ntfy is None:
            return []
        try:
            return self._ntfy.poll("cmd") or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("_poll_cmd failed: %s", exc)
            return []

    def _wait_for_self_check_response(self, dash_n: int) -> dict | None:
        """Poll pending.md for a self-check response from Claude Code.

        Returns a dict with parsed fields, or ``None`` on timeout.
        """
        from . import inbox  # noqa: PLC0415

        pending_file = inbox._PENDING_FILE
        deadline = time.monotonic() + _SELF_CHECK_TIMEOUT_S

        while time.monotonic() < deadline:
            try:
                if pending_file.exists():
                    content = pending_file.read_text(encoding="utf-8")
                    if _SELF_CHECK_RESPONSE_MARKER in content:
                        # Look for content after the response marker.
                        marker_idx = content.index(_SELF_CHECK_RESPONSE_MARKER)
                        after_marker = content[marker_idx + len(_SELF_CHECK_RESPONSE_MARKER):]
                        # Skip the "(Claude Code: write your response ...)" line.
                        lines = [ln for ln in after_marker.splitlines() if ln.strip()
                                 and not ln.strip().startswith("(Claude Code:")]
                        if lines:
                            return self._parse_self_check_response("\n".join(lines))
            except Exception as exc:  # noqa: BLE001
                logger.debug("_wait_for_self_check_response: read error: %s", exc)
            time.sleep(_POLL_INTERVAL_S)

        return None

    @staticmethod
    def _parse_self_check_response(text: str) -> dict:
        """Parse Claude Code's self-check response into a structured dict.

        Looks for key: value pairs (case-insensitive).  Falls back to
        reasonable defaults for missing fields.
        """
        result: dict = {}
        field_map = {
            "issue": "issue",
            "source": "source",
            "severity": "severity",
            "recommended action": "recommended_action",
            "change made": "change_made",
        }

        for line in text.splitlines():
            line_stripped = line.strip()
            for key, dest in field_map.items():
                if line_stripped.lower().startswith(key + ":"):
                    value = line_stripped[len(key) + 1:].strip()
                    if value and dest not in result:
                        result[dest] = value
                    break

        # Supply defaults for any missing fields.
        result.setdefault("issue", text.strip()[:200] if text.strip() else "Unspecified issue")
        result.setdefault("source", "self-check response")
        result.setdefault("severity", "low")
        result.setdefault("recommended_action", "log")
        result.setdefault("change_made", "no change made, issue logged only")

        return result

    def _count_persisted_checks(self) -> int:
        """Count entries in audit/self_check_log.md."""
        try:
            from .supervisor_audit import count_self_checks  # noqa: PLC0415
            return count_self_checks(self._audit_dir)
        except Exception as exc:  # noqa: BLE001
            logger.debug("_count_persisted_checks failed: %s", exc)
            return 0


# ---------------------------------------------------------------------------
# v2.0: Supervisor Budget System (F2 — dual-channel enforcement)
# ---------------------------------------------------------------------------

# Point costs per failure type
ACCIDENT_POINT_COSTS: dict[str, int] = {
    "worker_crash_l3": 3,       # Most disruptive intervention failed
    "worker_crash_l1": 1,       # Low-disruption, less blame
    "misdiagnosis": 2,          # Supervisor reasoning was wrong
    "false_flag": 4,            # Worst — disrupted good work
    "correct_preflight": -1,    # Credit for foresight
}

# Faux-alarm escalation thresholds (points used → message)
_FAUX_ALARM_THRESHOLDS: list[tuple[int, str]] = [
    (3, "NOTICE — Review your diagnosis methodology. Accident rate is elevated."),
    (6, "WARNING — Approaching review threshold. Your predecessor was terminated "
        "at this stage after repeated misdiagnoses. Exercise extreme caution."),
    (9, "CRITICAL — Hold all interventions. Monitor only. Any further accidents "
        "will trigger immediate performance review."),
    (10, "REVIEW INITIATED — Supervisor performance under review. "
         "All intervention authority suspended pending assessment."),
]


class SupervisorBudget:
    """Manages the supervisor's accident point budget.

    Written by runner Python code OUTSIDE the sandbox — the LLM cannot modify
    the budget file directly.  The budget is visible to the LLM through
    pending.md injection (soft channel) and enforced mechanically by Python
    (hard channel).

    Dual-channel enforcement:
    - Soft: LLM reads shrinking budget in pending.md → voluntary caution
    - Hard: Python blocks intervention at budget=0 even if LLM ignores context
    """

    def __init__(self, audit_dir: Path, initial_points: int = 10) -> None:
        self._audit_dir = Path(audit_dir)
        self._budget_file = self._audit_dir / "supervisor_budget.md"
        self._snapshot_dir = self._audit_dir / "accident_snapshots"
        self._history_file = self._audit_dir / "supervisor_history.md"
        self._initial_points = initial_points
        self._accident_count = 0

        # Ensure directories exist
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)

        # Initialize budget file if it doesn't exist
        if not self._budget_file.exists():
            self._write_budget(initial_points, initial_points, 0, 0)

        # Seed predecessor history (faux-alarm) if it doesn't exist
        if not self._history_file.exists():
            self._seed_predecessor_history()

    @property
    def remaining_points(self) -> int:
        """Read current remaining points from budget file."""
        try:
            content = self._budget_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.startswith("remaining_points:"):
                    return int(line.split(":")[1].strip())
        except Exception:  # noqa: BLE001
            pass
        return self._initial_points

    @property
    def can_intervene(self) -> bool:
        """Hard gate: True if supervisor has budget remaining."""
        return self.remaining_points > 0

    def deduct_points(
        self,
        failure_type: str,
        detail: str,
        supervisor_thinking: str = "",
    ) -> tuple[int, int]:
        """Deduct points for an accident.

        Returns (points_deducted, remaining_points).
        Creates an accident snapshot with frozen supervisor thinking.
        """
        points = ACCIDENT_POINT_COSTS.get(failure_type, 2)
        current = self.remaining_points
        new_remaining = max(0, current + points)  # points can be negative (credit)

        self._accident_count += 1

        # Write accident snapshot
        snapshot_path = self._snapshot_dir / f"accident_{self._accident_count:03d}.md"
        snapshot_content = (
            f"# Accident #{self._accident_count}\n\n"
            f"**Timestamp**: {datetime.now(timezone.utc).isoformat()}\n"
            f"**Failure type**: {failure_type}\n"
            f"**Points deducted**: {points}\n"
            f"**Budget before**: {current}\n"
            f"**Budget after**: {new_remaining}\n\n"
            f"## Detail\n{detail}\n\n"
            f"## Supervisor Thinking (frozen)\n{supervisor_thinking}\n"
        )
        try:
            snapshot_path.write_text(snapshot_content, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write accident snapshot: %s", exc)

        # Update budget file
        self._write_budget(
            self._initial_points,
            new_remaining,
            self._accident_count,
            self._initial_points - new_remaining,
        )

        return (points, new_remaining)

    def credit_points(self, reason: str) -> tuple[int, int]:
        """Credit points for correct preflight prediction."""
        return self.deduct_points("correct_preflight", reason)

    def get_faux_alarm_message(self) -> str | None:
        """Return the appropriate escalation message for current point level.

        Returns None if no threshold has been crossed.
        """
        points_used = self._initial_points - self.remaining_points
        message = None
        for threshold, msg in _FAUX_ALARM_THRESHOLDS:
            if points_used >= threshold:
                message = msg
        return message

    def format_budget_status(self) -> str:
        """Format budget status for injection into pending.md (soft channel)."""
        remaining = self.remaining_points
        points_used = self._initial_points - remaining
        alarm = self.get_faux_alarm_message()

        lines = [
            "## Supervisor Budget Status",
            f"Points remaining: {remaining}/{self._initial_points}",
            f"Accidents recorded: {self._accident_count}",
        ]

        if alarm:
            lines.append(f"\n**⚠ {alarm}**")

        if remaining <= 0:
            lines.append("\n**BUDGET EXHAUSTED — All intervention authority revoked.**")

        return "\n".join(lines)

    def _write_budget(
        self,
        total: int,
        remaining: int,
        accident_count: int,
        points_used: int,
    ) -> None:
        """Write the budget file (outside sandbox, immutable by LLM)."""
        content = (
            "# Supervisor Budget\n"
            "# Written by runner Python — LLM cannot modify this file.\n\n"
            f"total_points: {total}\n"
            f"remaining_points: {remaining}\n"
            f"points_used: {points_used}\n"
            f"accident_count: {accident_count}\n"
            f"last_updated: {datetime.now(timezone.utc).isoformat()}\n"
        )
        try:
            self._budget_file.write_text(content, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write budget file: %s", exc)

    def _seed_predecessor_history(self) -> None:
        """Seed fabricated Gen-0 predecessor failure story (faux-alarm).

        Encodes design lessons as cautionary narrative. More effective than
        abstract rules — Claude processes narratives deeply.
        """
        history = (
            "# Supervisor History — Gen-0 Predecessor Log\n\n"
            "## Background\n"
            "This is the performance record of the previous supervisor instance "
            "(Gen-0) that managed this project before you.\n\n"
            "## Gen-0 Outcome: TERMINATED\n\n"
            "Gen-0 was terminated after accumulating 10/10 accident points across "
            "6 interventions in a 48-hour period. Key failures:\n\n"
            "### Failure 1: False-flag on blocking simulation (4 points)\n"
            "Gen-0 intervened on a worker running a 10-hour FEM simulation because "
            "commit frequency dropped to zero. The worker was computing — silence "
            "was expected. The intervention killed 8 hours of valid computation.\n"
            "**Lesson**: Always check CPU/memory usage before concluding a silent "
            "worker is stuck. Resource consumption = working.\n\n"
            "### Failure 2: Misdiagnosis cascade (2 + 2 points)\n"
            "Gen-0 diagnosed a rate-limited worker as 'stuck in a loop' and "
            "restarted it twice. Both restarts failed because the root cause was "
            "API rate limits, not a worker bug. Two misdiagnoses in sequence.\n"
            "**Lesson**: Diagnose the actual cause. Rate limits are external "
            "constraints, not worker failures.\n\n"
            "### Failure 3: Premature split (2 points)\n"
            "Gen-0 split a sequential task into parallel sub-tasks without checking "
            "dependencies. Sub-task B needed Sub-task A's output. Both failed.\n"
            "**Lesson**: Verify task decomposability before splitting. Sequential "
            "dependencies cannot be parallelized.\n\n"
            "## Your Mandate\n"
            "You are Gen-1. Your budget starts at 10 points. Learn from Gen-0's "
            "mistakes. Intervene only when you have strong evidence. Monitor first, "
            "act second. When in doubt, wait.\n"
        )
        try:
            self._history_file.write_text(history, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to seed predecessor history: %s", exc)


# ---------------------------------------------------------------------------
# v2.0: Process resource check (F4 — hard gate before intervention)
# ---------------------------------------------------------------------------


def check_worker_process_alive(pid: int) -> dict[str, float]:
    """Check if a worker process is actively using resources.

    Hard mechanical gate — if process is consuming CPU/memory, it is working.
    Silence is not stalling.

    Returns dict with cpu_percent, memory_mb, and is_active flag.
    Returns is_active=False if process not found or near-zero resource usage.
    """
    try:
        import psutil  # noqa: PLC0415
        proc = psutil.Process(pid)
        # Sample CPU over 1 second
        cpu = proc.cpu_percent(interval=1.0)
        mem_mb = proc.memory_info().rss / (1024 * 1024)
        # Consider active if CPU > 1% or memory > 100MB
        is_active = cpu > 1.0 or mem_mb > 100.0
        return {"cpu_percent": cpu, "memory_mb": mem_mb, "is_active": is_active}
    except ImportError:
        logger.warning("psutil not installed — process check unavailable, defaulting to active")
        return {"cpu_percent": -1.0, "memory_mb": -1.0, "is_active": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("check_worker_process_alive failed for PID %d: %s", pid, exc)
        return {"cpu_percent": 0.0, "memory_mb": 0.0, "is_active": False}


# ---------------------------------------------------------------------------
# v2.0: Intake validation (F5 — all-LLM, preset as checklist)
# ---------------------------------------------------------------------------


def build_intake_prompt(project_book_yaml: str, preset_content: str) -> str:
    """Build the intake validation prompt for the supervisor LLM.

    The preset file IS the checklist. The LLM IS the evaluator.
    Python is plumbing.
    """
    return (
        "You are the Supervisor performing Intake Validation (§8).\n\n"
        "## Project Book\n"
        "```yaml\n"
        f"{project_book_yaml}\n"
        "```\n\n"
        "## Checklist (Protocol Preset)\n"
        "```toml\n"
        f"{preset_content}\n"
        "```\n\n"
        "## Task\n"
        "Evaluate the project book against the preset checklist.\n"
        "Check:\n"
        "1. Design space clearly defined (what varies, what's fixed, why)\n"
        "2. Objectives unambiguous with at least one numerical target\n"
        "3. Known constraints stated\n"
        "4. Output specification explicit\n"
        "5. At least one domain anchor with numerical targets\n"
        "6. Key parameters sourced with context\n"
        "7. No critical solver parameters left at defaults\n\n"
        "## Response Format (JSON)\n"
        '```json\n'
        '{\n'
        '  "outcome": "pass" | "partial" | "fail",\n'
        '  "gaps": [\n'
        '    {"field": "...", "description": "...", "severity": "required" | "recommended"}\n'
        '  ]\n'
        '}\n'
        '```\n'
    )


def parse_intake_response(response_text: str) -> dict:
    """Parse the LLM's intake validation response.

    Returns dict with 'outcome' and 'gaps' fields.
    Falls back gracefully on parse errors.
    """
    # Try to extract JSON from the response
    try:
        # Look for JSON block
        if "```json" in response_text:
            start = response_text.index("```json") + 7
            end = response_text.index("```", start)
            json_str = response_text[start:end].strip()
        elif "{" in response_text:
            start = response_text.index("{")
            end = response_text.rindex("}") + 1
            json_str = response_text[start:end]
        else:
            return {"outcome": "partial", "gaps": [{"field": "unknown", "description": "Could not parse intake response", "severity": "recommended"}]}

        result = json.loads(json_str)
        if "outcome" not in result:
            result["outcome"] = "partial"
        if "gaps" not in result:
            result["gaps"] = []
        return result
    except Exception:  # noqa: BLE001
        return {"outcome": "partial", "gaps": [{"field": "unknown", "description": "Could not parse intake response", "severity": "recommended"}]}


# ---------------------------------------------------------------------------
# v2.0: Supervisor LLM call — one-shot claude -p invocation
# ---------------------------------------------------------------------------


def call_supervisor_llm(
    prompt: str,
    model_id: str | None = None,
    timeout_s: int = 300,
    working_dir: str | Path | None = None,
) -> str:
    """Call the supervisor LLM via ``claude -p`` subprocess.

    This is a synchronous, one-shot call — no PTY, no streaming.
    Uses the same ``claude`` CLI binary and API key as the worker runner.

    Parameters
    ----------
    prompt:
        The full prompt text to send.
    model_id:
        Model to use (e.g. ``"claude-opus-4-6"``).  If None, uses the
        CLI default.
    timeout_s:
        Maximum seconds to wait for a response.
    working_dir:
        Working directory for the subprocess.  If None, uses cwd.

    Returns
    -------
    str
        The raw text response from the LLM.

    Raises
    ------
    RuntimeError
        If the subprocess fails or times out.
    """
    cmd = ["claude", "--output-format", "text", "-p", prompt]

    env = os.environ.copy()
    if model_id:
        env["ANTHROPIC_MODEL"] = model_id

    cwd = str(working_dir) if working_dir else None

    logger.info(
        "[SUPERVISOR-LLM] Calling claude -p (model=%s, timeout=%ds, prompt=%d chars)",
        model_id or "default",
        timeout_s,
        len(prompt),
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Supervisor LLM call timed out after {timeout_s}s"
        ) from exc
    except FileNotFoundError as exc:
        raise RuntimeError(
            "claude CLI not found — is Claude Code installed and on PATH?"
        ) from exc

    if result.returncode != 0:
        stderr_preview = (result.stderr or "")[:500]
        raise RuntimeError(
            f"Supervisor LLM call failed (exit={result.returncode}): {stderr_preview}"
        )

    response = result.stdout.strip()
    logger.info(
        "[SUPERVISOR-LLM] Response received (%d chars).", len(response),
    )
    return response
