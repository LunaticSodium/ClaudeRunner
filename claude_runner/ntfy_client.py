"""
claude_runner/ntfy_client.py

Thin wrapper around the ntfy.sh HTTP API for claude-runner marathon mode.

Channel names are read exclusively from Windows Credential Manager via
the keyring library. They are never read from project books or environment
variables to preserve the pipeline security boundary.

Credential Manager service names:
  "claude-runner-ntfy-out"  — outbound notification channel
  "claude-runner-ntfy-cmd"  — inbound command channel
"""
from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_DEFAULT_HOME = pathlib.Path.home() / ".claude-runner"
_NTFY_STATE_FILE = _DEFAULT_HOME / "ntfy_state.json"

# Default channel names (used as fallback display labels only — not as secrets)
_DEFAULT_OUT_CHANNEL = "claude-runner-honacoo"
_DEFAULT_CMD_CHANNEL = "claude-runner-honacoo-cmd"

_KEYRING_SERVICE_OUT = "claude-runner-ntfy-out"
_KEYRING_SERVICE_CMD = "claude-runner-ntfy-cmd"

_NTFY_BASE_URL = "https://ntfy.sh"


@dataclass
class NtfyMessage:
    """A single inbound message from an ntfy channel."""

    id: str
    message: str
    timestamp: int


class NtfyClient:
    """
    Thin wrapper around ntfy.sh HTTP API.

    Both channel names are read from Windows Credential Manager
    (service names: "claude-runner-ntfy-out", "claude-runner-ntfy-cmd").
    Never read from project books or environment variables.

    If a channel name is not found in Credential Manager, a warning is
    logged and operations for that channel degrade gracefully (publish is
    skipped, poll returns empty).
    """

    def __init__(self) -> None:
        self._out_channel: str | None = _get_channel_from_keyring(_KEYRING_SERVICE_OUT)
        self._cmd_channel: str | None = _get_channel_from_keyring(_KEYRING_SERVICE_CMD)

        if not self._out_channel:
            logger.warning(
                "ntfy out-channel name not found in Credential Manager "
                "(service=%r). Publish to 'out' will be disabled.",
                _KEYRING_SERVICE_OUT,
            )
        if not self._cmd_channel:
            logger.warning(
                "ntfy cmd-channel name not found in Credential Manager "
                "(service=%r). Poll from 'cmd' will be disabled.",
                _KEYRING_SERVICE_CMD,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def publish(self, channel: str, message: str, title: str = "") -> None:
        """POST *message* to the ntfy channel.

        *channel* is a logical name: ``"out"`` or ``"cmd"``.
        The actual channel name is resolved from Credential Manager.
        Fire-and-forget with a 5-second timeout; errors are logged only.
        """
        channel_name = self._resolve_channel(channel)
        if not channel_name:
            logger.debug("publish(%r) skipped — channel name not configured.", channel)
            return

        url = f"{_NTFY_BASE_URL}/{channel_name}"
        headers: dict = {}
        if title:
            headers["Title"] = title

        try:
            requests.post(
                url,
                data=message.encode("utf-8"),
                headers=headers,
                timeout=5,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ntfy publish to %r failed: %s", channel_name, exc)

    def poll(self, channel: str, since_id: str | None = None) -> list[NtfyMessage]:
        """Fetch new messages from the ntfy channel since *since_id*.

        Returns a list of :class:`NtfyMessage` objects (may be empty).
        Persists the last seen message ID to ``~/.claude-runner/ntfy_state.json``.

        Parameters
        ----------
        channel:
            Logical channel name: ``"out"`` or ``"cmd"``.
        since_id:
            Fetch only messages with ID > *since_id*.
            Pass ``None`` to fetch all available messages (equivalent to
            ``since=all``).
        """
        channel_name = self._resolve_channel(channel)
        if not channel_name:
            logger.debug("poll(%r) skipped — channel name not configured.", channel)
            return []

        since = since_id if since_id else "all"
        url = f"{_NTFY_BASE_URL}/{channel_name}/json"
        params = {"since": since, "poll": "1"}

        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ntfy poll from %r failed: %s", channel_name, exc)
            return []

        messages: list[NtfyMessage] = []
        raw_text = response.text.strip()
        if not raw_text:
            return []

        last_id: str | None = None
        for line in raw_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.debug("Failed to parse ntfy JSON line %r: %s", line[:80], exc)
                continue

            # ntfy emits "open" keepalive events — skip them.
            if obj.get("event") == "keepalive" or obj.get("event") == "open":
                continue

            msg_id = str(obj.get("id", ""))
            msg_text = str(obj.get("message", ""))
            msg_ts = int(obj.get("time", 0))

            if not msg_id:
                continue

            messages.append(NtfyMessage(id=msg_id, message=msg_text, timestamp=msg_ts))
            last_id = msg_id

        if last_id:
            _save_ntfy_state(last_id)

        return messages

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_channel(self, logical: str) -> str | None:
        """Map logical channel name ('out', 'cmd') to the actual ntfy channel name."""
        if logical == "out":
            return self._out_channel
        if logical == "cmd":
            return self._cmd_channel
        # Allow passing raw channel names through directly.
        return logical or None


# ---------------------------------------------------------------------------
# Keyring helpers
# ---------------------------------------------------------------------------


def _get_channel_from_keyring(service: str) -> str | None:
    """Retrieve a channel name from Windows Credential Manager.

    Returns ``None`` if keyring is unavailable or the credential is not set.
    """
    try:
        import keyring  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("keyring not installed — cannot retrieve %r from Credential Manager.", service)
        return None
    try:
        value = keyring.get_password(service, "channel_name")
        return (value or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("keyring lookup for %r failed: %s", service, exc)
        return None


def store_channel_in_keyring(service: str, channel_name: str) -> None:
    """Store a channel name in Windows Credential Manager.

    Called by the configure wizard.

    Raises
    ------
    RuntimeError
        If keyring is not installed.
    """
    try:
        import keyring  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "The keyring package is required to store ntfy channel names. "
            "Install with: pip install claude-runner[keyring]"
        ) from exc
    keyring.set_password(service, "channel_name", channel_name)
    logger.info("Stored channel name for %r in Credential Manager.", service)


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------


def _save_ntfy_state(last_message_id: str) -> None:
    """Persist the last seen message ID to ``~/.claude-runner/ntfy_state.json``."""
    try:
        _DEFAULT_HOME.mkdir(parents=True, exist_ok=True)
        state: dict = {}
        if _NTFY_STATE_FILE.exists():
            try:
                state = json.loads(_NTFY_STATE_FILE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                state = {}
        state["last_message_id"] = last_message_id
        _NTFY_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to save ntfy state: %s", exc)


# ---------------------------------------------------------------------------
# CLI interface — usable standalone or via claude-runner ntfy
# ---------------------------------------------------------------------------


def cli_send(channel: str, message: str, title: str = "") -> None:
    """Send a message to an ntfy channel.  Callable from CLI or code."""
    client = NtfyClient()
    client.publish(channel, message, title=title)
    print(f"Sent to {channel}: {message[:80]}")


def cli_poll(channel: str = "cmd") -> list[NtfyMessage]:
    """Poll an ntfy channel and print messages.  Returns the messages."""
    client = NtfyClient()
    state: dict = {}
    if _NTFY_STATE_FILE.exists():
        try:
            state = json.loads(_NTFY_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    since_id = state.get("last_message_id")
    messages = client.poll(channel, since_id)
    if not messages:
        print(f"No new messages on {channel}.")
    else:
        for msg in messages:
            print(f"[{msg.id}] {msg.message}")
    return messages


def cli_listen(channel: str = "cmd", interval_s: float = 10.0, stop_file: str = "") -> None:
    """Long-poll an ntfy channel, printing messages as they arrive.

    Runs until Ctrl-C or until *stop_file* exists (watchdog pattern).

    Parameters
    ----------
    channel:
        Logical channel name (``"cmd"`` or ``"out"``).
    interval_s:
        Seconds between polls.
    stop_file:
        Path to a sentinel file.  If it exists, exit cleanly.
        Empty string disables sentinel check.
    """
    import time as _time  # noqa: PLC0415

    client = NtfyClient()
    sentinel = pathlib.Path(stop_file) if stop_file else None
    print(f"Listening on {channel} (poll every {interval_s}s). Ctrl-C to stop.")
    if sentinel:
        print(f"Stop sentinel: {sentinel}")

    try:
        while True:
            # Sentinel check
            if sentinel and sentinel.exists():
                print("Stop sentinel detected — exiting.")
                try:
                    sentinel.unlink()
                except OSError:
                    pass
                break

            # Poll
            state: dict = {}
            if _NTFY_STATE_FILE.exists():
                try:
                    state = json.loads(_NTFY_STATE_FILE.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    pass
            since_id = state.get("last_message_id")
            messages = client.poll(channel, since_id)
            for msg in messages:
                print(f"[{msg.id}] {msg.message}")

            _time.sleep(interval_s)
    except KeyboardInterrupt:
        print("\nStopped.")


def main() -> None:
    """Entry point for ``python -m claude_runner.ntfy_client``."""
    import sys  # noqa: PLC0415

    usage = (
        "Usage: python -m claude_runner.ntfy_client <command> [args]\n"
        "Commands:\n"
        "  send <channel> <message> [title]  — publish a message\n"
        "  poll [channel]                    — poll once (default: cmd)\n"
        "  listen [channel] [interval_s]     — long-poll (default: cmd, 10s)\n"
    )

    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(usage)
        sys.exit(0)

    cmd = args[0]
    if cmd == "send":
        if len(args) < 3:
            print("send requires: <channel> <message> [title]")
            sys.exit(1)
        title = args[3] if len(args) > 3 else ""
        cli_send(args[1], args[2], title=title)
    elif cmd == "poll":
        channel = args[1] if len(args) > 1 else "cmd"
        cli_poll(channel)
    elif cmd == "listen":
        channel = args[1] if len(args) > 1 else "cmd"
        interval = float(args[2]) if len(args) > 2 else 10.0
        stop = args[3] if len(args) > 3 else ""
        cli_listen(channel, interval_s=interval, stop_file=stop)
    else:
        print(f"Unknown command: {cmd}\n{usage}")
        sys.exit(1)


if __name__ == "__main__":
    main()
