"""
Notification dispatch for claude-runner.

Delivers notifications via the apprise library across three channel types:
  - desktop  : Windows toast notification (zero config)
  - email    : SMTP via apprise, credentials from secrets file / environment
  - webhook  : HTTP POST with structured JSON payload

Routing rules (from spec section 2.4):
  - start          → desktop + webhook only
  - rate_limit     → desktop + webhook only
  - resume         → desktop + webhook only
  - complete       → all channels (email body includes git-diff-stat summary)
  - error          → all channels (email body includes partial diff summary)
  - milestone      → desktop + webhook only

EMAIL GUARD:
  Minimum 300 minutes between emails.  This is an anomaly guard, not a throttle.
  Under normal operation exactly one email is sent per task cycle.  If the guard
  fires, it is treated as a BUG: the duplicate email is blocked, a BUG-level
  entry is written to the log and state file, and a desktop popup is raised.
  The guard cannot be disabled or reconfigured.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_EVENTS: frozenset[str] = frozenset(
    {"start", "rate_limit", "resume", "complete", "error", "milestone"}
)

# Events that must NOT trigger email (desktop + webhook only).
EMAIL_EXCLUDED_EVENTS: frozenset[str] = frozenset(
    {"start", "rate_limit", "resume", "milestone"}
)

EMAIL_GUARD_MINUTES: int = 300  # 5 hours — not configurable

# A4: completion message constants
_COMPLETION_SUMMARY_MAX_BYTES: int = 3 * 1024  # 3 KB
_NTFY_MAX_CHARS: int = 4000
_TRUNCATION_MARKER: str = "…[truncated]"

# Heuristic: lines that look like tool invocations (Claude Code tool use).
# We scan backwards past these to find the last natural-language block.
import re as _re
_TOOL_LINE_PATTERN: _re.Pattern = _re.compile(
    r"^(?:"
    r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏●○◉►▶→]"   # spinner / bullet characters
    r"|[✓✗✔✘⚠]"                    # status icons
    r"|Tool:"
    r"|Using:"
    r"|Read\("
    r"|Write\("
    r"|Edit\("
    r"|Bash\("
    r"|Glob\("
    r"|Grep\("
    r"|\s*\d+\s*tool"               # "N tool calls"
    r"|\s*Running\s"
    r"|\s*\[tool_"
    r"|##RUNNER:"
    r")",
    _re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# NotificationManager
# ---------------------------------------------------------------------------

class NotificationManager:
    """
    Dispatches notifications via apprise for configured channels.

    Channels (from project book ``notify.channels``):
      - ``type: desktop``  — Windows toast via apprise (zero config)
      - ``type: email``    — SMTP via apprise, credentials from secrets file/env
      - ``type: webhook``  — HTTP POST via apprise

    Email content for ``complete``/``error`` events:
      Subject: [claude-runner] <task_name> — <STATUS>
      Body: git-diff-stat style change summary (spec section 2.4)

    Parameters
    ----------
    notify_config:
        The ``notify`` block from the parsed ProjectBook (must expose
        ``.on`` (list[str]) and ``.channels`` (list[dict])).
    task_name:
        Human-readable task identifier (used in notification titles).
    secrets_config:
        Secrets object (must expose SMTP credentials; see ``_build_email_url``).
    on_fault:
        Callback invoked with a BUG-level message string whenever the email
        guard fires.  Typically ``PersistenceManager.append_fault``.
    """

    def __init__(
        self,
        notify_config,
        task_name: str,
        secrets_config,
        on_fault: Callable[[str], None],
    ) -> None:
        self._notify_config = notify_config
        self._task_name = task_name
        self._secrets_config = secrets_config
        self._on_fault = on_fault

        # Timestamp (monotonic seconds) of the last successfully *sent* email.
        # None means no email has been sent in this session.
        self._last_email_time: float | None = None

        # Lazily-imported apprise module (None if apprise is not installed).
        self._apprise_module = self._try_import_apprise()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def dispatch(
        self,
        event: str,
        message: str,
        change_summary: str = "",
    ) -> None:
        """
        Dispatch notification for *event*.

        Respects per-channel routing rules and the email guard.

        Parameters
        ----------
        event:
            One of ``VALID_EVENTS``.
        message:
            Human-readable description of the event.
        change_summary:
            git-diff-stat style content (used in email body for complete/error).
        """
        if event not in VALID_EVENTS:
            logger.warning(
                "notify.dispatch called with unknown event %r — ignoring.", event
            )
            return

        # Determine which subscribed events should fire.
        subscribed_events: list[str] = []
        if self._notify_config is not None:
            subscribed_events = list(getattr(self._notify_config, "on", []) or [])

        if subscribed_events and event not in subscribed_events:
            logger.debug("Event %r not in notify.on list — skipping.", event)
            return

        title = f"[claude-runner] {self._task_name} — {event.upper()}"

        # Normalise channels to plain dicts so the rest of this method can use
        # .get() uniformly regardless of whether the caller passed Pydantic
        # NotifyChannel objects (production) or plain dicts (tests / mocks).
        raw_channels = (
            list(getattr(self._notify_config, "channels", []) or [])
            if self._notify_config is not None
            else []
        )
        channels: list[dict] = [
            ch.model_dump() if hasattr(ch, "model_dump") else dict(ch)
            for ch in raw_channels
        ]

        send_email = event not in EMAIL_EXCLUDED_EVENTS
        ts_iso = datetime.now(timezone.utc).isoformat()  # noqa: F841

        for channel in channels:
            ch_type: str = (channel.get("type") or "").lower()

            if ch_type == "desktop":
                self._send_desktop(title, message)

            elif ch_type == "email":
                if not send_email:
                    logger.debug(
                        "Email skipped for event %r (excluded event type).", event
                    )
                    continue

                if not self._check_email_guard():
                    # Guard fired — compute elapsed minutes for the BUG message.
                    elapsed_min = self._elapsed_since_last_email_minutes()
                    bug_msg = (
                        f"[BUG] Email guard triggered: second email attempted "
                        f"{elapsed_min:.1f} minutes after last send. "
                        f"Event: {event!r}. "
                        "This indicates abnormal execution "
                        "(crash loop, duplicate event emission, or logic error in runner.py). "
                        "Investigate state file and runner log."
                    )
                    logger.error(bug_msg)
                    self._send_desktop(
                        title="[claude-runner] BUG — Email guard triggered",
                        body=bug_msg,
                    )
                    try:
                        self._on_fault(bug_msg)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "on_fault callback raised an exception while recording "
                            "email guard fault."
                        )
                    continue  # Block the duplicate email.

                subject = f"[claude-runner] {self._task_name} \u2014 {event.upper()}"
                email_body = self.format_email_body(
                    task_name=self._task_name,
                    status=event.upper(),
                    runtime_str=self._format_runtime(),
                    change_summary=change_summary,
                )
                self._send_email(subject, email_body, change_summary)

            elif ch_type == "webhook":
                self._send_webhook(event, title, message, url=channel.get("url"))

            else:
                logger.warning(
                    "Unknown notification channel type %r — skipping.", ch_type
                )

        # If no explicit webhook channel in the project book, fall back to the
        # globally configured webhook URL (saved by `claude-runner configure`
        # when the user chose ntfy.sh).  This means users don't have to copy
        # the ntfy URL into every project book — one configure run is enough.
        has_webhook = any(ch.get("type", "").lower() == "webhook" for ch in channels)
        if not has_webhook:
            global_url = (
                getattr(self._secrets_config, "notify_webhook_url", None) or ""
            ).strip()
            if global_url:
                logger.debug("Using global webhook URL from secrets: %s", global_url)
                self._send_webhook(event, title, message, url=global_url)

    # ------------------------------------------------------------------
    # Channel send helpers
    # ------------------------------------------------------------------

    def _send_desktop(self, title: str, body: str) -> None:
        """Send a Windows desktop toast notification via apprise."""
        ap = self._apprise_module
        if ap is None:
            logger.debug("apprise not available — desktop notification skipped.")
            return
        try:
            instance = ap.Apprise()
            # windows:// is the native Windows 10/11 toast backend in apprise.
            instance.add("windows://")
            result = instance.notify(title=title, body=body)
            if not result:
                logger.warning(
                    "Desktop notification returned False (apprise reported failure)."
                )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send desktop notification.")

    def _send_email(self, subject: str, body: str, change_summary: str) -> None:
        """
        Send an SMTP email via apprise.

        Credentials are sourced from ``secrets_config`` (smtp_host, smtp_port,
        smtp_user, smtp_password, email_from, email_to).
        """
        ap = self._apprise_module
        if ap is None:
            logger.debug("apprise not available — email notification skipped.")
            return

        url = self._build_email_url()
        if url is None:
            logger.warning(
                "Email channel is configured but no SMTP credentials are available. "
                "Run `claude-runner configure` to set up email."
            )
            return

        try:
            instance = ap.Apprise()
            instance.add(url)
            result = instance.notify(title=subject, body=body)
            if result:
                self._last_email_time = time.monotonic()
                logger.info("Email sent successfully. Subject: %s", subject)
            else:
                logger.warning(
                    "Email notification returned False (apprise reported failure). "
                    "Subject: %s",
                    subject,
                )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send email notification. Subject: %s", subject)

    def _send_webhook(self, event: str, title: str, body: str, url: str | None = None) -> None:
        """POST a structured JSON payload to *url* (or skip if url is empty)."""
        ap = self._apprise_module
        if ap is None:
            logger.debug("apprise not available — webhook notification skipped.")
            return

        webhook_url = (url or "").strip()
        if not webhook_url:
            logger.debug("No webhook URL provided — webhook notification skipped.")
            return

        payload = {
            "event": event,
            "task": self._task_name,
            "message": body,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        try:
            # Use apprise's JSON plugin: json://<host>/<path>
            # We prefer to POST raw JSON ourselves via apprise's schema URL.
            # apprise supports json:// schema for HTTP JSON POST.
            parsed = self._parse_webhook_url_for_apprise(webhook_url)
            instance = ap.Apprise()
            instance.add(parsed)
            # apprise's JSON plugin sends {"title": ..., "message": ...}
            # We encode our full payload as the body so consumers get everything.
            result = instance.notify(
                title=f"[claude-runner] {event}",
                body=json.dumps(payload),
            )
            if not result:
                logger.warning(
                    "Webhook notification returned False (apprise reported failure). "
                    "URL: %s",
                    webhook_url,
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to send webhook notification. URL: %s", webhook_url
            )

    # ------------------------------------------------------------------
    # Email guard
    # ------------------------------------------------------------------

    def _check_email_guard(self) -> bool:
        """
        Return ``True`` if sending an email is allowed right now.

        Returns ``False`` (blocks the send) if the guard interval has not
        elapsed since the last send.  The guard is always active and cannot
        be disabled.
        """
        if self._last_email_time is None:
            return True
        elapsed_seconds = time.monotonic() - self._last_email_time
        guard_seconds = EMAIL_GUARD_MINUTES * 60
        return elapsed_seconds >= guard_seconds

    def _elapsed_since_last_email_minutes(self) -> float:
        """Return elapsed minutes since the last email, or 0.0 if no email sent."""
        if self._last_email_time is None:
            return 0.0
        return (time.monotonic() - self._last_email_time) / 60.0

    # ------------------------------------------------------------------
    # Email body formatting
    # ------------------------------------------------------------------

    def format_email_body(
        self,
        task_name: str,
        status: str,
        runtime_str: str,
        change_summary: str,
    ) -> str:
        """
        Format the email body in git-diff-stat style (spec section 2.4).

        Example output::

            Task:    Spirits mod card implementation
            Status:  COMPLETE
            Runtime: 4h 22m (1 rate limit cycle)

            Changed files:
              src/cards/CatStrike.cs          |  87 ++++++++++++
              ...
              5 files changed, 310 insertions(+), 0 deletions(-)
        """
        lines: list[str] = [
            f"Task:    {task_name}",
            f"Status:  {status}",
            f"Runtime: {runtime_str}",
        ]

        if change_summary:
            lines.append("")
            lines.append("Changed files:")
            # Indent each line of the summary for visual consistency.
            for summary_line in change_summary.splitlines():
                lines.append(f"  {summary_line}")
        else:
            lines.append("")
            lines.append("Changed files:")
            lines.append("  (no change summary available)")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _try_import_apprise():
        """
        Attempt to import apprise.  Returns the module on success, or ``None``
        with a warning log if apprise is not installed.
        """
        try:
            import apprise  # noqa: PLC0415
            return apprise
        except ImportError:
            logger.warning(
                "apprise is not installed — all notifications will be silently skipped. "
                "Install it with: pip install apprise"
            )
            return None

    def _build_email_url(self) -> str | None:
        """
        Construct an apprise SMTP URL from secrets_config.

        Expected secrets attributes (all optional individually; None if missing):
          smtp_host, smtp_port, smtp_user, smtp_password, email_from, email_to

        Returns an apprise ``mailtos://`` or ``mailto://`` URL, or ``None`` if
        critical credentials are missing.

        apprise SMTP URL format:
          mailtos://user:password@hostname:port/to@domain.com
          (mailtos = SMTP+TLS, mailto = plain SMTP)
        """
        sc = self._secrets_config
        if sc is None:
            return None

        smtp_host: str | None = getattr(sc, "smtp_host", None)
        smtp_port: int = int(getattr(sc, "smtp_port", 587) or 587)
        smtp_user: str | None = getattr(sc, "smtp_user", None)
        smtp_password: str | None = getattr(sc, "smtp_password", None)
        email_to: str | None = getattr(sc, "email_to", None)

        if not smtp_host or not smtp_user or not smtp_password or not email_to:
            return None

        # Choose TLS vs plain based on port convention.
        schema = "mailtos" if smtp_port in (465, 587) else "mailto"

        # URL-encode the password to handle special characters.
        from urllib.parse import quote as _quote  # noqa: PLC0415

        encoded_password = _quote(smtp_password, safe="")
        encoded_user = _quote(smtp_user, safe="")
        encoded_to = _quote(email_to, safe="@")

        url = (
            f"{schema}://{encoded_user}:{encoded_password}"
            f"@{smtp_host}:{smtp_port}/{encoded_to}"
        )
        return url

    @staticmethod
    def _parse_webhook_url_for_apprise(webhook_url: str) -> str:
        """
        Convert a plain HTTPS webhook URL to an apprise ``json://`` schema URL
        so that apprise posts a JSON body.

        apprise json:// schema:
          json://hostname/path      (HTTP)
          jsons://hostname/path     (HTTPS)
        """
        if webhook_url.startswith("https://"):
            return "jsons://" + webhook_url[len("https://"):]
        if webhook_url.startswith("http://"):
            return "json://" + webhook_url[len("http://"):]
        # Already an apprise URL or unknown schema — pass through unchanged.
        return webhook_url

    # ------------------------------------------------------------------
    # A4: Completion ntfy message builder
    # ------------------------------------------------------------------

    def build_completion_ntfy_message(
        self,
        task_name: str,
        duration_str: str,
        rate_limit_cycles: int,
        output_lines: list,
    ) -> str:
        """
        Build the ntfy message body for the 'complete' event (A4).

        Scans *output_lines* backwards for the last natural-language block
        produced by Claude Code (lines after the last tool invocation line,
        before ##RUNNER:COMPLETE##).

        Returns a string of at most 4000 chars.  Truncates with '…[truncated]'
        if needed.  Structured event fields are always appended.
        """
        prefix = (
            f"Task: {task_name} | Duration: {duration_str} | RL cycles: {rate_limit_cycles}\n\n"
        )
        summary = extract_completion_summary(output_lines)
        if summary:
            body = prefix + summary
        else:
            body = prefix

        if len(body) > _NTFY_MAX_CHARS:
            body = body[: _NTFY_MAX_CHARS - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER

        return body

    def _format_runtime(self) -> str:
        """
        Placeholder runtime string.  In production the runner passes a
        pre-formatted runtime string via ``change_summary``; this is only
        used as a fallback when the caller does not supply one.
        """
        return "unknown"


# ---------------------------------------------------------------------------
# A4: Module-level helper — extract last NL block from output buffer
# ---------------------------------------------------------------------------


def extract_completion_summary(output_lines: list) -> str:
    """
    Scan *output_lines* backwards for the last natural-language block.

    Heuristic:
      1. Skip any trailing ##RUNNER:COMPLETE## / ##RUNNER:ERROR## lines.
      2. Find the index of the last "tool invocation" line.
      3. Everything after that index (up to 3 KB) is the NL summary.

    Returns an empty string if no NL block is found.
    """
    if not output_lines:
        return ""

    lines = list(output_lines)

    # Strip trailing marker lines.
    while lines and "##RUNNER:" in lines[-1]:
        lines.pop()

    if not lines:
        return ""

    # Find the last tool line.
    last_tool_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if _TOOL_LINE_PATTERN.match(lines[i].strip()):
            last_tool_idx = i
            break

    # NL block = lines after the last tool line.
    nl_lines = lines[last_tool_idx + 1 :]
    if not nl_lines:
        return ""

    # Join and enforce 3 KB limit.
    text = "\n".join(nl_lines)
    encoded = text.encode("utf-8")
    if len(encoded) > _COMPLETION_SUMMARY_MAX_BYTES:
        text = encoded[:_COMPLETION_SUMMARY_MAX_BYTES].decode("utf-8", errors="ignore")

    return text.strip()
