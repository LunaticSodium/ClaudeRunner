"""
tests/test_supervisor_protocol.py

Tests for the Supervisor Protocol feature.

Coverage:
- Schema: SupervisorProtocolConfig defaults and field validation
- Schema: supervisor_protocol and cccs mutual exclusion
- SupervisorProtocol.wait_for_confirm: confirm, timeout, non-confirm re-prompt
- SupervisorProtocol.handle_violation: logs to audit and sets halt flag
- SupervisorProtocol.trigger_self_check: skips at limit, increments counter,
  writes to pending.md
- Audit helpers: append_supervisor_log, append_self_check_entry, count_self_checks
- Daemon: supervisor_confirm delegates to protocol, returns True when no supervisor
- Pipeline: _require_confirm gates commands when supervisor present
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_supervisor(tmp_path: Path, ntfy_client=None, *, self_check_limit=10,
                    confirm_timeout_minutes=1):
    """Create a SupervisorProtocol instance pointing to tmp_path."""
    from claude_runner.project import SupervisorProtocolConfig
    from claude_runner.supervisor_protocol import SupervisorProtocol

    config = SupervisorProtocolConfig(
        enabled=True,
        self_check_limit=self_check_limit,
        confirm_timeout_minutes=confirm_timeout_minutes,
        audit_dir="audit/",
    )
    if ntfy_client is None:
        ntfy_client = MagicMock()
    return SupervisorProtocol(
        config=config,
        project_id="test-project",
        ntfy_client=ntfy_client,
        working_dir=tmp_path,
    )


# ---------------------------------------------------------------------------
# 1. Schema: SupervisorProtocolConfig defaults and validation
# ---------------------------------------------------------------------------


class TestSupervisorProtocolConfigSchema:
    def test_default_disabled(self):
        from claude_runner.project import SupervisorProtocolConfig
        cfg = SupervisorProtocolConfig()
        assert cfg.enabled is False

    def test_default_self_check_limit(self):
        from claude_runner.project import SupervisorProtocolConfig
        cfg = SupervisorProtocolConfig()
        assert cfg.self_check_limit == 10

    def test_default_confirm_timeout(self):
        from claude_runner.project import SupervisorProtocolConfig
        cfg = SupervisorProtocolConfig()
        assert cfg.confirm_timeout_minutes == 5

    def test_default_audit_dir(self):
        from claude_runner.project import SupervisorProtocolConfig
        cfg = SupervisorProtocolConfig()
        assert cfg.audit_dir == "audit/"

    def test_custom_values(self):
        from claude_runner.project import SupervisorProtocolConfig
        cfg = SupervisorProtocolConfig(
            enabled=True,
            self_check_limit=5,
            confirm_timeout_minutes=3,
            audit_dir="logs/audit/",
        )
        assert cfg.enabled is True
        assert cfg.self_check_limit == 5
        assert cfg.confirm_timeout_minutes == 3
        assert cfg.audit_dir == "logs/audit/"

    def test_self_check_limit_ge_1(self):
        from claude_runner.project import SupervisorProtocolConfig
        with pytest.raises(ValidationError):
            SupervisorProtocolConfig(self_check_limit=0)

    def test_confirm_timeout_ge_1(self):
        from claude_runner.project import SupervisorProtocolConfig
        with pytest.raises(ValidationError):
            SupervisorProtocolConfig(confirm_timeout_minutes=0)

    def test_extra_fields_forbidden(self):
        from claude_runner.project import SupervisorProtocolConfig
        with pytest.raises(ValidationError):
            SupervisorProtocolConfig(unknown_field="boom")


# ---------------------------------------------------------------------------
# 2. Schema: supervisor_protocol and cccs mutual exclusion
# ---------------------------------------------------------------------------


class TestMutualExclusion:
    def _minimal_book_yaml(self, extras: str = "") -> str:
        return f"""
name: test
prompt: do something
{extras}
"""

    def test_supervisor_enabled_without_cccs_ok(self):
        from claude_runner.project import ProjectBook
        book = ProjectBook.model_validate({
            "name": "test",
            "prompt": "do something",
            "supervisor_protocol": {"enabled": True},
        })
        assert book.supervisor_protocol.enabled is True

    def test_cccs_enabled_without_supervisor_ok(self):
        from claude_runner.project import ProjectBook
        book = ProjectBook.model_validate({
            "name": "test",
            "prompt": "do something",
            "cccs": {"enabled": True, "preset": "cccs-v1.0"},
        })
        assert book.cccs is not None
        assert book.cccs.enabled is True

    def test_both_enabled_raises_config_error(self):
        from claude_runner.project import ProjectBook
        from claude_runner.config import ConfigError
        with pytest.raises((ValidationError, ConfigError)):
            ProjectBook.model_validate({
                "name": "test",
                "prompt": "do something",
                "supervisor_protocol": {"enabled": True},
                "cccs": {"enabled": True, "preset": "cccs-v1.0"},
            })

    def test_supervisor_enabled_cccs_disabled_ok(self):
        """supervisor_protocol=enabled with cccs=disabled should NOT raise."""
        from claude_runner.project import ProjectBook
        book = ProjectBook.model_validate({
            "name": "test",
            "prompt": "do something",
            "supervisor_protocol": {"enabled": True},
            "cccs": {"enabled": False, "preset": "cccs-v1.0"},
        })
        assert book.supervisor_protocol.enabled is True

    def test_both_disabled_ok(self):
        from claude_runner.project import ProjectBook
        book = ProjectBook.model_validate({
            "name": "test",
            "prompt": "do something",
            "supervisor_protocol": {"enabled": False},
            "cccs": {"enabled": False, "preset": "cccs-v1.0"},
        })
        assert book.supervisor_protocol.enabled is False

    def test_error_message_content(self):
        from claude_runner.project import ProjectBook
        from claude_runner.config import ConfigError
        try:
            ProjectBook.model_validate({
                "name": "test",
                "prompt": "do something",
                "supervisor_protocol": {"enabled": True},
                "cccs": {"enabled": True, "preset": "cccs-v1.0"},
            })
            pytest.fail("Expected an exception")
        except (ValidationError, ConfigError) as exc:
            assert "mutually exclusive" in str(exc).lower() or "supervisor_protocol" in str(exc)


# ---------------------------------------------------------------------------
# 3. wait_for_confirm: confirm path, timeout path, non-confirm re-prompt
# ---------------------------------------------------------------------------


class TestWaitForConfirm:
    def test_confirm_returns_true(self, tmp_path):
        """Receiving exactly 'confirm' returns True."""
        from claude_runner.ntfy_client import NtfyMessage

        mock_ntfy = MagicMock()
        confirm_msg = NtfyMessage(id="1", message="confirm", timestamp=0)
        mock_ntfy.poll.return_value = [confirm_msg]

        sp = make_supervisor(tmp_path, ntfy_client=mock_ntfy, confirm_timeout_minutes=1)
        result = sp.wait_for_confirm("Intent: launch project X.")
        assert result is True

    def test_confirm_case_insensitive(self, tmp_path):
        """'CONFIRM' and 'Confirm' are also accepted."""
        from claude_runner.ntfy_client import NtfyMessage

        mock_ntfy = MagicMock()
        confirm_msg = NtfyMessage(id="1", message="CONFIRM", timestamp=0)
        mock_ntfy.poll.return_value = [confirm_msg]

        sp = make_supervisor(tmp_path, ntfy_client=mock_ntfy, confirm_timeout_minutes=1)
        result = sp.wait_for_confirm("do something")
        assert result is True

    def test_timeout_returns_false(self, tmp_path):
        """When no 'confirm' arrives within timeout, returns False."""
        mock_ntfy = MagicMock()
        mock_ntfy.poll.return_value = []  # Never confirms

        # Use a very short timeout so the test runs quickly.
        sp = make_supervisor(tmp_path, ntfy_client=mock_ntfy, confirm_timeout_minutes=1)
        # Patch the timeout to be very short (0.05 min = 3 seconds → too slow).
        # Instead, patch time.monotonic to fast-forward.
        original_monotonic = time.monotonic
        call_count = {"n": 0}

        def fast_monotonic():
            call_count["n"] += 1
            # After a few calls, return a value past the deadline.
            if call_count["n"] > 4:
                return original_monotonic() + 1000
            return original_monotonic()

        import claude_runner.supervisor_protocol as sp_mod
        with patch.object(sp_mod.time, "monotonic", side_effect=fast_monotonic):
            with patch.object(sp_mod.time, "sleep"):
                result = sp.wait_for_confirm("Intent: do X.")

        assert result is False

    def test_timeout_publishes_expired_message(self, tmp_path):
        """On timeout, publishes 'input expired, no action taken' to out channel."""
        mock_ntfy = MagicMock()
        mock_ntfy.poll.return_value = []

        sp = make_supervisor(tmp_path, ntfy_client=mock_ntfy, confirm_timeout_minutes=1)

        original_monotonic = time.monotonic
        call_count = {"n": 0}

        def fast_monotonic():
            call_count["n"] += 1
            if call_count["n"] > 4:
                return original_monotonic() + 1000
            return original_monotonic()

        import claude_runner.supervisor_protocol as sp_mod
        with patch.object(sp_mod.time, "monotonic", side_effect=fast_monotonic):
            with patch.object(sp_mod.time, "sleep"):
                sp.wait_for_confirm("Intent: do X.")

        publish_calls = [str(c) for c in mock_ntfy.publish.call_args_list]
        assert any("input expired" in c or "no action taken" in c for c in publish_calls)

    def test_non_confirm_prompts_again(self, tmp_path):
        """Non-confirm reply causes re-prompt then eventually times out."""
        from claude_runner.ntfy_client import NtfyMessage

        mock_ntfy = MagicMock()
        non_confirm = NtfyMessage(id="1", message="maybe", timestamp=0)

        call_count = {"n": 0}
        def poll_side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return [non_confirm]
            return []

        mock_ntfy.poll.side_effect = poll_side_effect

        sp = make_supervisor(tmp_path, ntfy_client=mock_ntfy, confirm_timeout_minutes=1)

        original_monotonic = time.monotonic
        mono_count = {"n": 0}

        def fast_monotonic():
            mono_count["n"] += 1
            if mono_count["n"] > 8:
                return original_monotonic() + 1000
            return original_monotonic()

        import claude_runner.supervisor_protocol as sp_mod
        with patch.object(sp_mod.time, "monotonic", side_effect=fast_monotonic):
            with patch.object(sp_mod.time, "sleep"):
                result = sp.wait_for_confirm("Intent: do Y.")

        # Should have published the "please send confirm" re-prompt message.
        publish_calls = [str(c) for c in mock_ntfy.publish.call_args_list]
        assert any("please send confirm" in c for c in publish_calls)
        assert result is False  # Eventually timed out


# ---------------------------------------------------------------------------
# 4. handle_violation: logs and sets halt flag
# ---------------------------------------------------------------------------


class TestHandleViolation:
    def test_sets_halt_requested(self, tmp_path):
        sp = make_supervisor(tmp_path)
        assert sp._halt_requested is False
        sp.handle_violation("some violation detail")
        assert sp._halt_requested is True

    def test_logs_to_audit(self, tmp_path):
        sp = make_supervisor(tmp_path)
        sp.handle_violation("agent wrote to ntfy directly")
        log_path = tmp_path / "audit" / "supervisor_log.md"
        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        assert "PROTOCOL_VIOLATION" in content
        assert "agent wrote to ntfy directly" in content

    def test_publishes_violation_alert(self, tmp_path):
        mock_ntfy = MagicMock()
        sp = make_supervisor(tmp_path, ntfy_client=mock_ntfy)
        sp.handle_violation("bad actor")
        publish_calls = [str(c) for c in mock_ntfy.publish.call_args_list]
        assert any("PROTOCOL VIOLATION" in c or "violation" in c.lower() for c in publish_calls)


# ---------------------------------------------------------------------------
# 5. trigger_self_check: skips at limit, writes to pending.md
# ---------------------------------------------------------------------------


class TestTriggerSelfCheck:
    def test_skips_when_at_limit(self, tmp_path):
        """When counter >= limit, skips the self-check and publishes limit-reached."""
        mock_ntfy = MagicMock()
        sp = make_supervisor(tmp_path, ntfy_client=mock_ntfy, self_check_limit=2)
        sp._self_check_counter = 2  # already at limit

        import claude_runner.inbox as inbox_mod
        with patch.object(inbox_mod, "append_message") as mock_append:
            sp.trigger_self_check(dash_n=3)
            mock_append.assert_not_called()

        publish_calls = [str(c) for c in mock_ntfy.publish.call_args_list]
        assert any("limit reached" in c.lower() for c in publish_calls)

    def test_increments_counter(self, tmp_path):
        """Counter is incremented when self-check runs."""
        mock_ntfy = MagicMock()
        sp = make_supervisor(tmp_path, ntfy_client=mock_ntfy, self_check_limit=5)
        assert sp._self_check_counter == 0

        import claude_runner.inbox as inbox_mod

        # Patch inbox to avoid actually writing, and patch _wait_for_self_check_response
        # to return immediately with a canned result.
        canned_result = {
            "issue": "test issue",
            "source": "test source",
            "severity": "low",
            "recommended_action": "log",
            "change_made": "no change made, issue logged only",
        }
        with patch.object(inbox_mod, "append_message"):
            with patch.object(sp, "_wait_for_self_check_response", return_value=canned_result):
                sp.trigger_self_check(dash_n=1)

        assert sp._self_check_counter == 1

    def test_writes_to_pending_md(self, tmp_path):
        """trigger_self_check calls inbox.append_message with the request."""
        mock_ntfy = MagicMock()
        sp = make_supervisor(tmp_path, ntfy_client=mock_ntfy, self_check_limit=5)

        import claude_runner.inbox as inbox_mod

        canned_result = {
            "issue": "test issue",
            "source": "first principles",
            "severity": "medium",
            "recommended_action": "log",
            "change_made": "no change made, issue logged only",
        }
        with patch.object(inbox_mod, "append_message") as mock_append:
            with patch.object(sp, "_wait_for_self_check_response", return_value=canned_result):
                sp.trigger_self_check(dash_n=2)

        mock_append.assert_called_once()
        call_text = mock_append.call_args[0][0]
        assert "SELF-CHECK REQUEST" in call_text
        assert "Dash 2" in call_text

    def test_publishes_decision_and_action_messages(self, tmp_path):
        """After a self-check, two ntfy messages are published."""
        mock_ntfy = MagicMock()
        sp = make_supervisor(tmp_path, ntfy_client=mock_ntfy, self_check_limit=5)

        import claude_runner.inbox as inbox_mod

        canned_result = {
            "issue": "risk A",
            "source": "web search",
            "severity": "high",
            "recommended_action": "fix before next Dash",
            "change_made": "fixed parameter X",
        }
        with patch.object(inbox_mod, "append_message"):
            with patch.object(sp, "_wait_for_self_check_response", return_value=canned_result):
                sp.trigger_self_check(dash_n=1)

        publish_calls = [str(c) for c in mock_ntfy.publish.call_args_list]
        assert any("SELF-CHECK" in c and "complete" in c.lower() for c in publish_calls)
        assert any("SELF-CHECK" in c and "action" in c.lower() for c in publish_calls)

    def test_writes_audit_entry(self, tmp_path):
        """After self-check, an entry is appended to self_check_log.md."""
        mock_ntfy = MagicMock()
        sp = make_supervisor(tmp_path, ntfy_client=mock_ntfy, self_check_limit=5)

        import claude_runner.inbox as inbox_mod

        canned_result = {
            "issue": "identified risk",
            "source": "first principles",
            "severity": "medium",
            "recommended_action": "log",
            "change_made": "no change made, issue logged only",
        }
        with patch.object(inbox_mod, "append_message"):
            with patch.object(sp, "_wait_for_self_check_response", return_value=canned_result):
                sp.trigger_self_check(dash_n=3)

        log_path = tmp_path / "audit" / "self_check_log.md"
        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        assert "Self-Check Entry" in content
        assert "identified risk" in content


# ---------------------------------------------------------------------------
# 6. Audit helpers
# ---------------------------------------------------------------------------


class TestAuditHelpers:
    def test_append_supervisor_log_creates_file(self, tmp_path):
        from claude_runner.supervisor_audit import append_supervisor_log
        audit_dir = tmp_path / "audit"
        append_supervisor_log(audit_dir, "TEST_EVENT", "some detail")
        log_path = audit_dir / "supervisor_log.md"
        assert log_path.exists()

    def test_append_supervisor_log_format(self, tmp_path):
        from claude_runner.supervisor_audit import append_supervisor_log
        audit_dir = tmp_path / "audit"
        append_supervisor_log(audit_dir, "MY_EVENT", "the detail here")
        content = (audit_dir / "supervisor_log.md").read_text(encoding="utf-8")
        assert "MY_EVENT" in content
        assert "the detail here" in content
        # Must contain a timestamp in [YYYY-... format
        assert "[20" in content

    def test_append_supervisor_log_is_append_only(self, tmp_path):
        from claude_runner.supervisor_audit import append_supervisor_log
        audit_dir = tmp_path / "audit"
        append_supervisor_log(audit_dir, "EVENT_A", "first")
        append_supervisor_log(audit_dir, "EVENT_B", "second")
        content = (audit_dir / "supervisor_log.md").read_text(encoding="utf-8")
        assert "EVENT_A" in content
        assert "EVENT_B" in content
        assert "first" in content
        assert "second" in content

    def test_append_self_check_entry_creates_file(self, tmp_path):
        from claude_runner.supervisor_audit import append_self_check_entry
        audit_dir = tmp_path / "audit"
        append_self_check_entry(
            audit_dir=audit_dir,
            dash_n=1,
            counter=1,
            limit=10,
            issue="test issue",
            source="web search",
            severity="low",
            recommended_action="log",
            change_made="no change",
        )
        log_path = audit_dir / "self_check_log.md"
        assert log_path.exists()

    def test_append_self_check_entry_content(self, tmp_path):
        from claude_runner.supervisor_audit import append_self_check_entry
        audit_dir = tmp_path / "audit"
        append_self_check_entry(
            audit_dir=audit_dir,
            dash_n=2,
            counter=3,
            limit=10,
            issue="memory leak risk",
            source="first principles",
            severity="high",
            recommended_action="fix before next Dash",
            change_made="patched buffer allocation",
        )
        content = (audit_dir / "self_check_log.md").read_text(encoding="utf-8")
        assert "memory leak risk" in content
        assert "first principles" in content
        assert "high" in content
        assert "patched buffer allocation" in content

    def test_count_self_checks_zero_when_no_file(self, tmp_path):
        from claude_runner.supervisor_audit import count_self_checks
        audit_dir = tmp_path / "audit"
        assert count_self_checks(audit_dir) == 0

    def test_count_self_checks_counts_entries(self, tmp_path):
        from claude_runner.supervisor_audit import append_self_check_entry, count_self_checks
        audit_dir = tmp_path / "audit"

        for i in range(3):
            append_self_check_entry(
                audit_dir=audit_dir,
                dash_n=i + 1,
                counter=i + 1,
                limit=10,
                issue=f"issue {i}",
                source="test",
                severity="low",
                recommended_action="log",
                change_made="none",
            )

        assert count_self_checks(audit_dir) == 3

    def test_count_self_checks_empty_file_returns_zero(self, tmp_path):
        from claude_runner.supervisor_audit import count_self_checks
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        (audit_dir / "self_check_log.md").write_text("", encoding="utf-8")
        assert count_self_checks(audit_dir) == 0


# ---------------------------------------------------------------------------
# 7. Daemon: supervisor_confirm delegates to protocol
# ---------------------------------------------------------------------------


class TestDaemonSupervisorConfirm:
    def test_returns_true_when_no_supervisor(self):
        """supervisor_confirm returns True when _supervisor is None."""
        from unittest.mock import MagicMock
        from claude_runner.daemon import MarathonDaemon

        config = MagicMock()
        config.marathon.poll_interval_minutes = 1
        daemon = MarathonDaemon(config=config)
        assert daemon._supervisor is None
        assert daemon.supervisor_confirm("any intent") is True

    def test_delegates_to_supervisor(self):
        """When _supervisor is set, supervisor_confirm delegates to it."""
        from claude_runner.daemon import MarathonDaemon

        config = MagicMock()
        config.marathon.poll_interval_minutes = 1
        daemon = MarathonDaemon(config=config)

        mock_supervisor = MagicMock()
        mock_supervisor.wait_for_confirm.return_value = True
        daemon._supervisor = mock_supervisor

        result = daemon.supervisor_confirm("test intent")
        mock_supervisor.wait_for_confirm.assert_called_once_with("test intent")
        assert result is True

    def test_on_dash_complete_triggers_self_check(self):
        """on_dash_complete calls trigger_self_check when supervisor is set."""
        from claude_runner.daemon import MarathonDaemon

        config = MagicMock()
        config.marathon.poll_interval_minutes = 1
        daemon = MarathonDaemon(config=config)

        mock_supervisor = MagicMock()
        daemon._supervisor = mock_supervisor

        daemon.on_dash_complete(dash_n=5)
        mock_supervisor.trigger_self_check.assert_called_once_with(5)

    def test_on_dash_complete_no_op_without_supervisor(self):
        """on_dash_complete is a no-op when _supervisor is None."""
        from claude_runner.daemon import MarathonDaemon

        config = MagicMock()
        config.marathon.poll_interval_minutes = 1
        daemon = MarathonDaemon(config=config)
        # Should not raise
        daemon.on_dash_complete(dash_n=1)


# ---------------------------------------------------------------------------
# 8. Pipeline: _require_confirm gates commands
# ---------------------------------------------------------------------------


class TestPipelineRequireConfirm:
    def test_require_confirm_returns_true_when_no_supervisor_confirm(self):
        """When daemon has no supervisor_confirm, _require_confirm returns True."""
        from claude_runner.pipeline import Pipeline
        from claude_runner.ntfy_client import NtfyMessage

        daemon = MagicMock(spec=[])  # no supervisor_confirm attribute
        ntfy = MagicMock()
        pipeline = Pipeline(daemon=daemon, ntfy_client=ntfy)
        msg = NtfyMessage(id="x", message="test", timestamp=0)
        assert pipeline._require_confirm("intent", msg) is True

    def test_require_confirm_delegates_to_daemon(self):
        """When daemon has supervisor_confirm, _require_confirm uses it."""
        from claude_runner.pipeline import Pipeline
        from claude_runner.ntfy_client import NtfyMessage

        daemon = MagicMock()
        daemon.supervisor_confirm.return_value = False
        ntfy = MagicMock()
        pipeline = Pipeline(daemon=daemon, ntfy_client=ntfy)
        msg = NtfyMessage(id="x", message="test", timestamp=0)
        result = pipeline._require_confirm("intent", msg)
        daemon.supervisor_confirm.assert_called_once_with("intent")
        assert result is False

    def test_stop_command_skipped_when_not_confirmed(self, tmp_path):
        """When supervisor_confirm returns False, stop command is not executed."""
        import claude_runner.pipeline as pm_mod
        from claude_runner.pipeline import Pipeline
        from claude_runner.ntfy_client import NtfyMessage

        daemon = MagicMock()
        daemon.supervisor_confirm.return_value = False
        ntfy = MagicMock()
        pipeline = Pipeline(daemon=daemon, ntfy_client=ntfy)
        pm_mod._INBOX_DIR = tmp_path / "inbox"
        pm_mod._TRASH_DIR = tmp_path / "trash"

        msg = NtfyMessage(id="x", message="stop", timestamp=0)
        pipeline.process(msg)

        daemon.stop.assert_not_called()

    def test_pause_command_skipped_when_not_confirmed(self, tmp_path):
        """When supervisor_confirm returns False, pause command is not executed."""
        import claude_runner.pipeline as pm_mod
        from claude_runner.pipeline import Pipeline
        from claude_runner.ntfy_client import NtfyMessage

        daemon = MagicMock()
        daemon.supervisor_confirm.return_value = False
        ntfy = MagicMock()
        pipeline = Pipeline(daemon=daemon, ntfy_client=ntfy)
        pm_mod._INBOX_DIR = tmp_path / "inbox"
        pm_mod._TRASH_DIR = tmp_path / "trash"

        msg = NtfyMessage(id="x", message="pause my-project", timestamp=0)
        pipeline.process(msg)

        daemon.pause_project.assert_not_called()
