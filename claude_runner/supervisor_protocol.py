"""
claude_runner/supervisor_protocol.py

Supervisor Protocol — hardcoded marathon behavioral layer.

All mechanisms in this module run as Python code only.  Claude Code never
writes directly to ntfy channels or audit logs; all such actions are
performed here by the Python script layer.

Activated by ``supervisor_protocol: enabled: true`` in the project book.
Once activated, all protocol rules are mandatory and cannot be overridden.
"""
from __future__ import annotations

import logging
import time
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
