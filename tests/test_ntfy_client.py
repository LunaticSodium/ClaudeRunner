"""
tests/test_ntfy_client.py

Unit tests for claude_runner.ntfy_client — NtfyClient.

All HTTP calls are mocked via unittest.mock. No real network calls.
"""
from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock, patch, call

import pytest

from claude_runner.ntfy_client import (
    NtfyClient,
    NtfyMessage,
    NtfyNotConfiguredError,
    _KEYRING_SERVICE_CMD,
    _KEYRING_SERVICE_OUT,
    _NTFY_STATE_FILE,
    _UNCONFIGURED_SENTINEL,
    _is_plausibly_intentional,
    _is_strictly_valid_channel,
    _save_ntfy_state,
    store_channel_in_keyring,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_client(
    out_channel: str = "test-out",
    cmd_channel: str = "test-cmd",
    *,
    book_out: str | None = None,
    book_cmd: str | None = None,
) -> NtfyClient:
    """Create NtfyClient with mocked keyring.

    *out_channel* / *cmd_channel* simulate what keyring returns (raw value).
    *book_out* / *book_cmd* are project-book overrides.
    """
    def fake_read_raw(service: str) -> str | None:
        if service == _KEYRING_SERVICE_OUT:
            return out_channel or None
        if service == _KEYRING_SERVICE_CMD:
            return cmd_channel or None
        return None

    with patch("claude_runner.ntfy_client._read_raw_from_keyring", side_effect=fake_read_raw):
        return NtfyClient(
            out_channel_override=book_out,
            cmd_channel_override=book_cmd,
        )


def make_response(text: str = "", status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    r.raise_for_status = MagicMock()
    return r


# ---------------------------------------------------------------------------
# Tests: two-tier validation
# ---------------------------------------------------------------------------


class TestValidation:
    """Tests for _is_strictly_valid_channel and _is_plausibly_intentional."""

    # Strict validation
    def test_strict_accepts_normal_channel(self):
        assert _is_strictly_valid_channel("claude-runner-honacoo") is True

    def test_strict_rejects_pure_numeric(self):
        assert _is_strictly_valid_channel("123456") is False

    def test_strict_rejects_short(self):
        assert _is_strictly_valid_channel("abc") is False

    def test_strict_rejects_single_digit(self):
        assert _is_strictly_valid_channel("1") is False

    def test_strict_rejects_boolean_like(self):
        for v in ("True", "False", "true", "false", "None", "null"):
            assert _is_strictly_valid_channel(v) is False

    def test_strict_rejects_empty(self):
        assert _is_strictly_valid_channel("") is False

    # Loose validation
    def test_loose_accepts_normal_channel(self):
        assert _is_plausibly_intentional("claude-runner-honacoo") is True

    def test_loose_accepts_pure_numeric_if_long(self):
        assert _is_plausibly_intentional("123456") is True

    def test_loose_rejects_short(self):
        assert _is_plausibly_intentional("abc") is False

    def test_loose_rejects_single_digit(self):
        assert _is_plausibly_intentional("1") is False

    def test_loose_rejects_boolean_like(self):
        for v in ("True", "False", "None", "null"):
            assert _is_plausibly_intentional(v) is False


# ---------------------------------------------------------------------------
# Tests: priority chain
# ---------------------------------------------------------------------------


class TestPriorityChain:
    """Tests for the 4-tier resolution in NtfyClient.__init__."""

    def test_tier1_keyring_strict_wins_over_project_book(self):
        """Keyring with valid value beats project book."""
        client = make_client(out_channel="keyring-ch", book_out="book-ch")
        assert client._out_channel == "keyring-ch"

    def test_tier2_project_book_wins_when_keyring_empty(self):
        """Project book is used when keyring has nothing."""
        client = make_client(out_channel="", book_out="book-channel")
        assert client._out_channel == "book-channel"

    def test_tier2_project_book_wins_when_keyring_corrupted(self):
        """Project book is used when keyring has a strictly-invalid value."""
        client = make_client(out_channel="1", book_out="book-channel")
        assert client._out_channel == "book-channel"

    def test_tier4_loose_keyring_used_when_book_empty(self):
        """Keyring with '123456' (strict-invalid, loose-valid) is used when book is empty."""
        client = make_client(out_channel="123456", book_out=None)
        assert client._out_channel == "123456"

    def test_tier4_loose_keyring_loses_to_book(self):
        """Keyring with '123456' loses to project book."""
        client = make_client(out_channel="123456", book_out="book-ch")
        assert client._out_channel == "book-ch"

    def test_sentinel_when_nothing_configured(self):
        """Sentinel reached when all tiers exhausted."""
        client = make_client(out_channel="", cmd_channel="", book_out=None, book_cmd=None)
        assert client._out_channel == _UNCONFIGURED_SENTINEL
        assert client._cmd_channel == _UNCONFIGURED_SENTINEL

    def test_sentinel_when_keyring_has_corrupted_short_value(self):
        """'1' fails both strict and loose → sentinel."""
        client = make_client(out_channel="1", book_out=None)
        assert client._out_channel == _UNCONFIGURED_SENTINEL

    def test_cmd_channel_independent(self):
        """out and cmd channels resolve independently."""
        client = make_client(
            out_channel="good-out", cmd_channel="",
            book_out=None, book_cmd="book-cmd",
        )
        assert client._out_channel == "good-out"
        assert client._cmd_channel == "book-cmd"


# ---------------------------------------------------------------------------
# Tests: publish()
# ---------------------------------------------------------------------------


class TestPublish:
    def test_posts_to_correct_url(self):
        client = make_client(out_channel="my-channel")
        with patch("requests.post", return_value=make_response()) as mock_post:
            client.publish("out", "hello world")
        mock_post.assert_called_once()
        url = mock_post.call_args.args[0]
        assert url == "https://ntfy.sh/my-channel"

    def test_posts_message_as_utf8_bytes(self):
        client = make_client(out_channel="my-channel")
        with patch("requests.post", return_value=make_response()) as mock_post:
            client.publish("out", "test message")
        kwargs = mock_post.call_args.kwargs
        assert kwargs["data"] == b"test message"

    def test_title_sent_as_header(self):
        client = make_client(out_channel="my-channel")
        with patch("requests.post", return_value=make_response()) as mock_post:
            client.publish("out", "body", title="My Title")
        headers = mock_post.call_args.kwargs.get("headers", {})
        assert headers.get("Title") == "My Title"

    def test_publish_raises_when_unconfigured(self):
        """v2.0a: sentinel default → NtfyNotConfiguredError, not silent send."""
        client = make_client(out_channel="", cmd_channel="test-cmd")
        with pytest.raises(NtfyNotConfiguredError, match="not configured"):
            client.publish("out", "hello")

    def test_timeout_is_5_seconds(self):
        client = make_client(out_channel="my-channel")
        with patch("requests.post", return_value=make_response()) as mock_post:
            client.publish("out", "msg")
        kwargs = mock_post.call_args.kwargs
        assert kwargs.get("timeout") == 5

    def test_posts_to_cmd_channel(self):
        client = make_client(cmd_channel="my-cmd-ch")
        with patch("requests.post", return_value=make_response()) as mock_post:
            client.publish("cmd", "cmd-message")
        url = mock_post.call_args.args[0]
        assert "my-cmd-ch" in url

    def test_no_crash_on_network_error(self):
        client = make_client(out_channel="my-channel")
        with patch("requests.post", side_effect=ConnectionError("offline")):
            client.publish("out", "hello")  # must not raise

    def test_publish_works_with_loose_keyring_channel(self):
        """A pure-numeric channel from keyring (loose tier) should still publish."""
        client = make_client(out_channel="123456")
        with patch("requests.post", return_value=make_response()) as mock_post:
            client.publish("out", "hello")
        mock_post.assert_called_once()
        url = mock_post.call_args.args[0]
        assert "123456" in url


# ---------------------------------------------------------------------------
# Tests: poll()
# ---------------------------------------------------------------------------


SAMPLE_MESSAGES = "\n".join([
    json.dumps({"id": "aaa", "event": "message", "time": 1000, "message": "run self-test"}),
    json.dumps({"id": "bbb", "event": "message", "time": 1001, "message": "status"}),
])


class TestPoll:
    def test_returns_list_of_ntfy_messages(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_runner.ntfy_client._NTFY_STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr("claude_runner.ntfy_client._DEFAULT_HOME", tmp_path)
        client = make_client(cmd_channel="test-cmd")
        with patch("requests.get", return_value=make_response(SAMPLE_MESSAGES)):
            msgs = client.poll("cmd", None)
        assert len(msgs) == 2
        assert all(isinstance(m, NtfyMessage) for m in msgs)

    def test_parses_id_message_timestamp(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_runner.ntfy_client._NTFY_STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr("claude_runner.ntfy_client._DEFAULT_HOME", tmp_path)
        client = make_client(cmd_channel="test-cmd")
        with patch("requests.get", return_value=make_response(SAMPLE_MESSAGES)):
            msgs = client.poll("cmd", None)
        assert msgs[0].id == "aaa"
        assert msgs[0].message == "run self-test"
        assert msgs[0].timestamp == 1000

    def test_url_construction_with_since_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_runner.ntfy_client._NTFY_STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr("claude_runner.ntfy_client._DEFAULT_HOME", tmp_path)
        client = make_client(cmd_channel="test-cmd")
        with patch("requests.get", return_value=make_response("")) as mock_get:
            client.poll("cmd", "xyz123")
        url = mock_get.call_args.args[0]
        assert url == "https://ntfy.sh/test-cmd/json"
        params = mock_get.call_args.kwargs.get("params", {})
        assert params["since"] == "xyz123"
        assert params["poll"] == "1"

    def test_url_construction_with_no_since_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_runner.ntfy_client._NTFY_STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr("claude_runner.ntfy_client._DEFAULT_HOME", tmp_path)
        client = make_client(cmd_channel="test-cmd")
        with patch("requests.get", return_value=make_response("")) as mock_get:
            client.poll("cmd", None)
        params = mock_get.call_args.kwargs.get("params", {})
        assert params["since"] == "all"

    def test_empty_response_returns_empty_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_runner.ntfy_client._NTFY_STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr("claude_runner.ntfy_client._DEFAULT_HOME", tmp_path)
        client = make_client(cmd_channel="test-cmd")
        with patch("requests.get", return_value=make_response("")):
            msgs = client.poll("cmd", None)
        assert msgs == []

    def test_skips_keepalive_events(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_runner.ntfy_client._NTFY_STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr("claude_runner.ntfy_client._DEFAULT_HOME", tmp_path)
        client = make_client(cmd_channel="test-cmd")
        lines = "\n".join([
            json.dumps({"id": "k1", "event": "keepalive", "time": 999, "message": ""}),
            json.dumps({"id": "m1", "event": "message", "time": 1000, "message": "hello"}),
        ])
        with patch("requests.get", return_value=make_response(lines)):
            msgs = client.poll("cmd", None)
        assert len(msgs) == 1
        assert msgs[0].id == "m1"

    def test_saves_last_message_id_to_state_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_runner.ntfy_client._NTFY_STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr("claude_runner.ntfy_client._DEFAULT_HOME", tmp_path)
        client = make_client(cmd_channel="test-cmd")
        with patch("requests.get", return_value=make_response(SAMPLE_MESSAGES)):
            client.poll("cmd", None)
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["last_message_id"] == "bbb"  # last message id

    def test_poll_raises_when_unconfigured(self):
        """v2.0a: sentinel default → NtfyNotConfiguredError, not silent empty list."""
        client = make_client(cmd_channel="")
        with pytest.raises(NtfyNotConfiguredError, match="not configured"):
            client.poll("cmd", None)

    def test_no_crash_on_network_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_runner.ntfy_client._NTFY_STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr("claude_runner.ntfy_client._DEFAULT_HOME", tmp_path)
        client = make_client(cmd_channel="test-cmd")
        with patch("requests.get", side_effect=ConnectionError("offline")):
            msgs = client.poll("cmd", None)  # must not raise
        assert msgs == []


# ---------------------------------------------------------------------------
# Tests: store_channel_in_keyring
# ---------------------------------------------------------------------------


class TestStoreChannel:
    def test_accepts_valid_channel(self):
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "my-channel"
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            store_channel_in_keyring("test-service", "my-channel")
        mock_keyring.set_password.assert_called_once_with(
            "test-service", "channel_name", "my-channel",
        )

    def test_accepts_pure_numeric_with_warning(self):
        """'123456' is loose-valid — stored with a warning, not rejected."""
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "123456"
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            store_channel_in_keyring("test-service", "123456")
        mock_keyring.set_password.assert_called_once()

    def test_rejects_single_digit(self):
        with pytest.raises(RuntimeError, match="Refusing to store"):
            store_channel_in_keyring("test-service", "1")

    def test_rejects_boolean_like(self):
        with pytest.raises(RuntimeError, match="Refusing to store"):
            store_channel_in_keyring("test-service", "True")

    def test_rejects_too_short(self):
        with pytest.raises(RuntimeError, match="Refusing to store"):
            store_channel_in_keyring("test-service", "ab")

    def test_readback_verification_failure(self):
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "WRONG"
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            with pytest.raises(RuntimeError, match="verification failed"):
                store_channel_in_keyring("test-service", "my-channel")
