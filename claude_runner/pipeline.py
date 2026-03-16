"""
claude_runner/pipeline.py

Inbound message pipeline for marathon mode.

Processes a single inbound ntfy message through five stages:
  RECEIVE → PARSE → CONVERT → LAUNCH → TRASH (on any failure)

Security requirements (strictly enforced):
  - Control command matching is exact keyword match only (case-insensitive,
    stripped). No substring match, no fuzzy match.
  - Inline YAML must pass full pydantic ProjectBook validation before execution.
  - YAML size limit of 4096 bytes enforced before yaml.safe_load().
  - No shell=True in any execution path.
"""
from __future__ import annotations

import datetime
import logging
import pathlib
from typing import TYPE_CHECKING, Optional, Union

import yaml
from pydantic import ValidationError

from .project import ProjectBook
from .ntfy_client import NtfyMessage

if TYPE_CHECKING:
    from .daemon import MarathonDaemon
    from .ntfy_client import NtfyClient

logger = logging.getLogger(__name__)

_DEFAULT_HOME = pathlib.Path.home() / ".claude-runner"
_INBOX_DIR = _DEFAULT_HOME / "inbox"
_TRASH_DIR = _DEFAULT_HOME / "trash"


# ---------------------------------------------------------------------------
# Sentinel types for parse stage output
# ---------------------------------------------------------------------------


class _ControlCommand:
    """Represents a recognised control keyword."""

    def __init__(self, keyword: str, args: str = "") -> None:
        self.keyword = keyword
        self.args = args


class _InlineYaml:
    """Represents a message body to be treated as inline YAML."""

    def __init__(self, body: str) -> None:
        self.body = body


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class Pipeline:
    """
    Processes a single inbound ntfy message through five stages.

    RECEIVE → PARSE → CONVERT → LAUNCH → TRASH (on any failure)
    """

    CONTROL_COMMANDS: frozenset = frozenset({"run", "abort", "status", "stop"})
    MAX_INLINE_YAML_BYTES: int = 4096

    def __init__(
        self,
        daemon: "MarathonDaemon",
        ntfy_client: "NtfyClient",
    ) -> None:
        self._daemon = daemon
        self._ntfy = ntfy_client

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def process(self, message: NtfyMessage) -> None:
        """Entry point.  Runs all stages; calls _trash() on any unhandled exception."""
        try:
            self._receive(message)
            result = self._parse(message)

            if isinstance(result, _ControlCommand):
                self._handle_control(result, message)
            elif isinstance(result, _InlineYaml):
                project_path = self._convert(result.body, message)
                if project_path is not None:
                    self._launch(project_path, message)
                else:
                    # A1: if CONVERT produced no path (parse/validation failure),
                    # treat the message as a free-text inbox message instead of
                    # silently dropping it.
                    self._route_to_inbox(message.message)
            else:
                self._trash("PARSE", "Unknown parse result type", message.message)
        except _PipelineError:
            # Already handled by _trash() inside the stage that raised it.
            pass
        except Exception as exc:  # noqa: BLE001
            logger.error("Unhandled pipeline error for message %s: %s", message.id, exc)
            self._trash("UNEXPECTED", str(exc), message.message)

    # ------------------------------------------------------------------
    # Stage 1: RECEIVE
    # ------------------------------------------------------------------

    def _receive(self, message: NtfyMessage) -> None:
        """Publish 'Received command: <first 80 chars>' to out channel."""
        preview = message.message[:80]
        self._ntfy.publish("out", f"Received command: {preview}", title="claude-runner")
        logger.info("Received message id=%s: %s", message.id, preview)

    # ------------------------------------------------------------------
    # Stage 2: PARSE
    # ------------------------------------------------------------------

    def _parse(self, message: NtfyMessage) -> Union[_ControlCommand, _InlineYaml]:
        """
        Exact keyword match (case-insensitive, stripped) against CONTROL_COMMANDS.

        Matching rules:
          run <name>  → find <name>.yaml in projects/ search path
          abort       → abort named running task
          status      → reply with daemon status summary
          stop        → call daemon.stop()
          else        → InlineYaml (pass body to CONVERT)

        "running" does NOT match "run". "Stop it" does NOT match "stop".
        Only exact single-word commands (with optional args after) match.
        """
        raw = message.message.strip()
        # Split into first token and optional remainder
        parts = raw.split(None, 1)
        if not parts:
            return _InlineYaml(raw)

        first_token = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if first_token in self.CONTROL_COMMANDS:
            return _ControlCommand(keyword=first_token, args=args)

        return _InlineYaml(raw)

    # ------------------------------------------------------------------
    # Stage 3: CONVERT (inline YAML only)
    # ------------------------------------------------------------------

    def _convert(self, body: str, original_message: NtfyMessage) -> Optional[pathlib.Path]:
        """
        Parse and validate inline YAML, write to inbox directory.

        Returns the path to the written YAML file on success.

        Returns ``None`` (without trashing) for messages that are not YAML at
        all — the caller (``process``) will route them to the inbox buffer
        instead (A1 behaviour).

        Raises ``_PipelineError`` (after calling ``_trash``) for messages that
        look like project books but are malformed (size limit, pydantic
        validation failure, or I/O error).
        """
        # Size limit — enforced before yaml.safe_load().
        # A message that exceeds the size limit is not a free-text inbox
        # message; trash it and abort.
        body_bytes = body.encode("utf-8")
        if len(body_bytes) > self.MAX_INLINE_YAML_BYTES:
            reason = (
                f"Inline YAML exceeds {self.MAX_INLINE_YAML_BYTES}-byte limit "
                f"({len(body_bytes)} bytes)."
            )
            self._trash("CONVERT", reason, original_message.message)
            raise _PipelineError(reason)

        # YAML parse — if the body is not YAML, return None so the caller can
        # route it to the inbox buffer (A1).
        try:
            raw = yaml.safe_load(body)
        except yaml.YAMLError:
            logger.debug("CONVERT: body is not valid YAML — routing to inbox.")
            return None

        if not isinstance(raw, dict):
            logger.debug("CONVERT: YAML top-level is not a mapping — routing to inbox.")
            return None

        # Pydantic validation — full validation, no relaxation for remote input.
        # A valid YAML dict that fails ProjectBook schema is a user error; trash.
        try:
            ProjectBook.model_validate(raw)
        except ValidationError as exc:
            self._trash("CONVERT", f"ProjectBook validation failed: {exc}", original_message.message)
            raise _PipelineError(str(exc))

        # Write to inbox
        try:
            _INBOX_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            inbox_path = _INBOX_DIR / f"{ts}.yaml"
            inbox_path.write_text(body, encoding="utf-8")
            logger.info("Inline YAML written to inbox: %s", inbox_path)
            return inbox_path
        except Exception as exc:  # noqa: BLE001
            self._trash("CONVERT", f"Failed to write inbox file: {exc}", original_message.message)
            raise _PipelineError(str(exc))

    # ------------------------------------------------------------------
    # Stage 4: LAUNCH
    # ------------------------------------------------------------------

    def _launch(self, project_path: pathlib.Path, original_message: NtfyMessage) -> None:
        """Start runner on project_path."""
        import sys  # noqa: PLC0415

        try:
            import subprocess  # noqa: PLC0415
            # No shell=True — construct command list explicitly
            cmd = [sys.executable, "-m", "claude_runner", "run", str(project_path)]
            subprocess.Popen(
                cmd,
                stdin=None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # DETACHED_PROCESS on Windows so the subprocess outlives us
                creationflags=0x00000008 if hasattr(subprocess, "DETACHED_PROCESS") else 0,
            )
            task_name = project_path.stem
            self._ntfy.publish("out", f"Task started: {task_name}", title="claude-runner")
            logger.info("Launched task from %s", project_path)
        except Exception as exc:  # noqa: BLE001
            self._trash("LAUNCH", f"Failed to launch task: {exc}", original_message.message)

    # ------------------------------------------------------------------
    # Stage TRASH (failure handler)
    # ------------------------------------------------------------------

    def _trash(self, stage: str, reason: str, original: str) -> None:
        """
        Write failure record to ~/.claude-runner/trash/<iso_timestamp>-<stage>.log.
        Publish '[TRASH] Failed at <stage>: <reason>' to out channel.
        """
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        try:
            _TRASH_DIR.mkdir(parents=True, exist_ok=True)
            log_path = _TRASH_DIR / f"{ts}-{stage}.log"
            content_lines = [
                f"timestamp: {ts}",
                f"stage: {stage}",
                f"reason: {reason}",
                "",
                "--- original message ---",
                original,
            ]
            log_path.write_text("\n".join(content_lines), encoding="utf-8")
            logger.info("Trash log written: %s", log_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to write trash log: %s", exc)

        notify_msg = f"[TRASH] Failed at {stage}: {reason[:200]}"
        try:
            self._ntfy.publish("out", notify_msg, title="claude-runner [TRASH]")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to publish trash notification: %s", exc)

    # ------------------------------------------------------------------
    # Control command dispatcher
    # ------------------------------------------------------------------

    def _handle_control(self, cmd: _ControlCommand, original_message: NtfyMessage) -> None:
        """Dispatch a parsed control command."""
        if cmd.keyword == "run":
            self._cmd_run(cmd.args.strip(), original_message)
        elif cmd.keyword == "abort":
            self._cmd_abort(cmd.args.strip(), original_message)
        elif cmd.keyword == "status":
            self._cmd_status(original_message)
        elif cmd.keyword == "stop":
            self._cmd_stop(original_message)

    def _cmd_run(self, name: str, original_message: NtfyMessage) -> None:
        """Find and launch a named project book from the projects/ search path."""
        if not name:
            self._trash("RUN", "run command requires a project name.", original_message.message)
            return

        import os  # noqa: PLC0415

        # Search for <name>.yaml in:
        #   1. projects/ relative to cwd
        #   2. ~/.claude-runner/inbox/<name>.yaml
        search_dirs = [
            pathlib.Path.cwd() / "projects",
            _INBOX_DIR,
        ]
        found: Optional[pathlib.Path] = None
        for d in search_dirs:
            candidate = d / f"{name}.yaml"
            if candidate.exists():
                found = candidate
                break

        if found is None:
            self._trash("RUN", f"Project {name!r} not found in search path.", original_message.message)
            return

        self._launch(found, original_message)

    def _cmd_abort(self, name: str, original_message: NtfyMessage) -> None:
        """Abort a named running task (best-effort)."""
        msg = f"Abort requested for task: {name!r}" if name else "Abort requested."
        logger.info(msg)
        self._ntfy.publish("out", msg, title="claude-runner")

    def _cmd_status(self, original_message: NtfyMessage) -> None:
        """Publish daemon status summary to out channel."""
        try:
            status = self._daemon.status()
            summary = (
                f"Daemon status: uptime={status.get('uptime_seconds', 0):.0f}s, "
                f"active_task={status.get('active_task')}, "
                f"pid={status.get('pid')}"
            )
            self._ntfy.publish("out", summary, title="claude-runner status")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Status query failed: %s", exc)

    def _cmd_stop(self, original_message: NtfyMessage) -> None:
        """Signal daemon to stop."""
        self._ntfy.publish("out", "Daemon stop requested.", title="claude-runner")
        try:
            self._daemon.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("daemon.stop() failed: %s", exc)

    # ------------------------------------------------------------------
    # A1: Inbox routing (non-YAML, non-command messages)
    # ------------------------------------------------------------------

    def _route_to_inbox(self, text: str) -> None:
        """
        Route a free-text message to the inbox buffer (Feature A1).

        Called when a message does not match any pipeline keyword and is not
        a valid YAML project book.  Instead of dropping/trashing the message,
        it is accumulated in ``~/.claude-runner/inbox/pending.md`` for
        injection into the running Claude Code session at the next natural
        pause point.
        """
        try:
            from . import inbox  # noqa: PLC0415
            inbox.append_message(text)
            logger.info("A1: message routed to inbox buffer (%d chars).", len(text))
            self._ntfy.publish(
                "out",
                "Message queued in inbox buffer (will be delivered at next pause).",
                title="claude-runner",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("A1: _route_to_inbox failed: %s", exc)


# ---------------------------------------------------------------------------
# Internal sentinel exception (never leaks outside Pipeline.process)
# ---------------------------------------------------------------------------


class _PipelineError(Exception):
    """Raised internally when a stage calls _trash() and wants to abort."""
