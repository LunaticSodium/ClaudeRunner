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
    _KEYRING_SERVICE_CMD,
    _KEYRING_SERVICE_OUT,
    _NTFY_STATE_FILE,
    _save_ntfy_state,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_client(out_channel: str = "test-out", cmd_channel: str = "test-cmd") -> NtfyClient:
    """Create NtfyClient with mocked keyring."""
    def fake_get_channel(service):
        if service == _KEYRING_SERVICE_OUT:
            return out_channel
        if service == _KEYRING_SERVICE_CMD:
            return cmd_channel
        return None

    with patch("claude_runner.ntfy_client._get_channel_from_keyring", side_effect=fake_get_channel):
        return NtfyClient()


def make_response(text: str = "", status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    r.raise_for_status = MagicMock()
    return r


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

    def test_publish_skipped_when_channel_not_configured(self):
        client = make_client(out_channel="", cmd_channel="test-cmd")
        with patch("requests.post") as mock_post:
            client.publish("out", "hello")
        mock_post.assert_not_called()

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

    def test_returns_empty_when_channel_not_configured(self):
        client = make_client(cmd_channel="")
        with patch("requests.get") as mock_get:
            msgs = client.poll("cmd", None)
        mock_get.assert_not_called()
        assert msgs == []

    def test_no_crash_on_network_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_runner.ntfy_client._NTFY_STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr("claude_runner.ntfy_client._DEFAULT_HOME", tmp_path)
        client = make_client(cmd_channel="test-cmd")
        with patch("requests.get", side_effect=ConnectionError("offline")):
            msgs = client.poll("cmd", None)  # must not raise
        assert msgs == []
