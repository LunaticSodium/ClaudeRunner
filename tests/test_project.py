"""
tests/test_project.py — Unit tests for claude_runner/project.py

Tests cover ProjectBook schema validation, field validators, and YAML loading.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from claude_runner.project import (
    NotifyChannel,
    NotifyConfig,
    ProjectBook,
    SandboxConfig,
    load_project_book,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_yaml(tmp_path: Path, content: str, filename: str = "book.yaml") -> Path:
    """Write YAML content to a file in tmp_path and return the path."""
    p = tmp_path / filename
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Minimal valid project book
# ---------------------------------------------------------------------------


class TestMinimalProjectBook:
    def test_minimal_loads_with_required_fields_only(self, tmp_project_dir, tmp_path):
        """A book with only name + prompt (no sandbox) should load successfully."""
        book = ProjectBook.model_validate({"name": "My Task", "prompt": "Do the thing."})
        assert book.name == "My Task"
        assert book.prompt == "Do the thing."
        assert book.description == ""
        assert book.sandbox is None

    def test_minimal_defaults_are_sane(self):
        """Default execution, output, and notify values should be present."""
        book = ProjectBook.model_validate({"name": "x", "prompt": "y"})
        assert book.execution.timeout_hours == 12.0
        assert book.execution.max_rate_limit_waits == 20
        assert book.execution.resume_strategy == "continue"
        assert book.execution.skip_permissions is False
        assert book.execution.context.checkpoint_threshold_tokens == 150_000
        assert book.execution.context.reset_on_rate_limit is True
        assert book.execution.context.inject_log_on_resume is True
        assert book.output.git.enabled is True
        assert book.output.git.auto_push is False
        assert book.output.log_dir is None
        assert book.notify.on == []
        assert book.notify.channels == []

    def test_from_yaml_minimal(self, tmp_project_dir, minimal_project_book_yaml, tmp_path):
        """ProjectBook.from_yaml() loads a minimal YAML file correctly."""
        yaml_file = write_yaml(tmp_path, minimal_project_book_yaml)
        book = ProjectBook.from_yaml(yaml_file)
        assert book.name == "Minimal Task"
        assert book.prompt == "Do something minimal."


# ---------------------------------------------------------------------------
# Full project book
# ---------------------------------------------------------------------------


class TestFullProjectBook:
    def test_full_book_loads(self, tmp_project_dir, full_project_book_yaml, tmp_path):
        """A fully-specified project book with all optional blocks should load."""
        yaml_file = write_yaml(tmp_path, full_project_book_yaml)
        book = ProjectBook.from_yaml(yaml_file)
        assert book.name == "Full Task"
        assert book.description == "A fully-specified task for testing."
        assert book.execution.resume_strategy == "restate"
        assert book.execution.timeout_hours == 8.0
        assert book.execution.max_rate_limit_waits == 10
        assert book.execution.context.checkpoint_threshold_tokens == 100_000
        assert len(book.execution.milestones) == 2
        assert book.execution.milestones[0].pattern == "All tests passed"
        assert book.execution.milestones[1].message == "Task complete marker detected"

    def test_full_book_notify_channels(self, tmp_project_dir, full_project_book_yaml, tmp_path):
        """Notify channels are parsed correctly for all three types."""
        yaml_file = write_yaml(tmp_path, full_project_book_yaml)
        book = ProjectBook.from_yaml(yaml_file)
        channel_types = {ch.type for ch in book.notify.channels}
        assert channel_types == {"email", "desktop", "webhook"}
        email_ch = next(ch for ch in book.notify.channels if ch.type == "email")
        assert email_ch.to == "test@example.com"
        webhook_ch = next(ch for ch in book.notify.channels if ch.type == "webhook")
        assert webhook_ch.url == "https://hooks.example.com/notify"

    def test_full_book_sandbox_working_dir_is_path(
        self, tmp_project_dir, full_project_book_yaml, tmp_path
    ):
        """sandbox.working_dir should be a Path object after loading."""
        yaml_file = write_yaml(tmp_path, full_project_book_yaml)
        book = ProjectBook.from_yaml(yaml_file)
        assert isinstance(book.sandbox.working_dir, Path)

    def test_full_book_network_config(self, tmp_project_dir, full_project_book_yaml, tmp_path):
        """Network allow-list is loaded correctly."""
        yaml_file = write_yaml(tmp_path, full_project_book_yaml)
        book = ProjectBook.from_yaml(yaml_file)
        assert "api.anthropic.com" in book.sandbox.network.allow
        assert book.sandbox.network.deny_all_others is True


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------


class TestMissingRequiredFields:
    def test_missing_name_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            ProjectBook.model_validate({"prompt": "Do something."})
        assert "name" in str(exc_info.value).lower()

    def test_missing_prompt_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            ProjectBook.model_validate({"name": "Task"})
        assert "prompt" in str(exc_info.value).lower()

    def test_empty_name_raises(self):
        """name has min_length=1 — empty string should fail."""
        with pytest.raises(ValidationError):
            ProjectBook.model_validate({"name": "", "prompt": "Do something."})

    def test_empty_prompt_raises(self):
        """prompt has min_length=1 — empty string should fail."""
        with pytest.raises(ValidationError):
            ProjectBook.model_validate({"name": "Task", "prompt": ""})


# ---------------------------------------------------------------------------
# Extra / unknown fields
# ---------------------------------------------------------------------------


class TestExtraFields:
    def test_unknown_top_level_field_raises(self):
        """extra='forbid' means unknown fields at the top level must raise."""
        with pytest.raises(ValidationError):
            ProjectBook.model_validate(
                {"name": "Task", "prompt": "Do it.", "unknown_field": "oops"}
            )

    def test_unknown_execution_field_raises(self):
        """extra='forbid' propagates into nested models."""
        with pytest.raises(ValidationError):
            ProjectBook.model_validate(
                {
                    "name": "Task",
                    "prompt": "Do it.",
                    "execution": {"timeout_hours": 1, "not_a_field": True},
                }
            )

    def test_unknown_sandbox_field_raises(self, tmp_project_dir):
        workspace = str((tmp_project_dir / "workspace").as_posix())
        with pytest.raises(ValidationError):
            ProjectBook.model_validate(
                {
                    "name": "Task",
                    "prompt": "Do it.",
                    "sandbox": {"working_dir": workspace, "mystery_key": 99},
                }
            )


# ---------------------------------------------------------------------------
# Invalid resume_strategy
# ---------------------------------------------------------------------------


class TestResumeStrategy:
    def test_valid_strategies_accepted(self):
        for strategy in ("continue", "restate", "summarize"):
            book = ProjectBook.model_validate(
                {"name": "T", "prompt": "P", "execution": {"resume_strategy": strategy}}
            )
            assert book.execution.resume_strategy == strategy

    def test_invalid_strategy_raises(self):
        with pytest.raises(ValidationError):
            ProjectBook.model_validate(
                {"name": "T", "prompt": "P", "execution": {"resume_strategy": "magic"}}
            )


# ---------------------------------------------------------------------------
# sandbox.working_dir path validation
# ---------------------------------------------------------------------------


class TestWorkingDirValidation:
    def test_existing_dir_passes(self, tmp_path):
        book = ProjectBook.model_validate(
            {
                "name": "T",
                "prompt": "P",
                "sandbox": {"working_dir": str(tmp_path)},
            }
        )
        assert book.sandbox.working_dir == tmp_path

    def test_nonexistent_dir_is_created(self, tmp_path):
        missing = tmp_path / "auto_created"
        assert not missing.exists()
        book = ProjectBook.model_validate(
            {"name": "T", "prompt": "P", "sandbox": {"working_dir": str(missing)}}
        )
        assert book.sandbox.working_dir == missing
        assert missing.is_dir()

    def test_file_instead_of_dir_raises(self, tmp_path):
        f = tmp_path / "a_file.txt"
        f.write_text("hello")
        with pytest.raises(ValidationError) as exc_info:
            ProjectBook.model_validate(
                {"name": "T", "prompt": "P", "sandbox": {"working_dir": str(f)}}
            )
        assert "not a directory" in str(exc_info.value)

    def test_working_dir_coerced_from_string(self, tmp_path):
        """working_dir should be coerced to Path from a string."""
        book = ProjectBook.model_validate(
            {"name": "T", "prompt": "P", "sandbox": {"working_dir": str(tmp_path)}}
        )
        assert isinstance(book.sandbox.working_dir, Path)


# ---------------------------------------------------------------------------
# from_yaml() / load_project_book()
# ---------------------------------------------------------------------------


class TestFromYaml:
    def test_load_project_book_file_not_found(self, tmp_path):
        missing = tmp_path / "nonexistent.yaml"
        with pytest.raises(FileNotFoundError):
            load_project_book(missing)

    def test_load_project_book_invalid_yaml(self, tmp_path):
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text(": invalid: yaml: {{", encoding="utf-8")
        with pytest.raises(yaml.YAMLError):
            load_project_book(bad_yaml)

    def test_load_project_book_non_mapping_yaml(self, tmp_path):
        """A YAML file whose top-level node is a list should raise YAMLError."""
        list_yaml = tmp_path / "list.yaml"
        list_yaml.write_text("- item1\n- item2\n", encoding="utf-8")
        with pytest.raises(yaml.YAMLError, match="Expected a YAML mapping"):
            load_project_book(list_yaml)

    def test_load_project_book_validation_error_on_bad_data(self, tmp_path):
        """YAML that doesn't match the schema should raise ValidationError."""
        bad = tmp_path / "bad_schema.yaml"
        bad.write_text("name: T\nprompt: P\nexecution:\n  timeout_hours: not_a_number\n")
        with pytest.raises(ValidationError):
            load_project_book(bad)

    def test_from_yaml_returns_project_book_instance(
        self, tmp_project_dir, minimal_project_book_yaml, tmp_path
    ):
        yaml_file = write_yaml(tmp_path, minimal_project_book_yaml)
        book = ProjectBook.from_yaml(yaml_file)
        assert isinstance(book, ProjectBook)


# ---------------------------------------------------------------------------
# Notification channel type validation
# ---------------------------------------------------------------------------


class TestNotifyChannelValidation:
    def test_email_channel_requires_to(self):
        with pytest.raises(ValidationError, match="requires a 'to' address"):
            NotifyChannel.model_validate({"type": "email"})

    def test_email_channel_valid(self):
        ch = NotifyChannel.model_validate({"type": "email", "to": "user@example.com"})
        assert ch.type == "email"
        assert ch.to == "user@example.com"

    def test_webhook_channel_requires_url(self):
        with pytest.raises(ValidationError, match="requires a 'url'"):
            NotifyChannel.model_validate({"type": "webhook"})

    def test_webhook_channel_valid(self):
        ch = NotifyChannel.model_validate({"type": "webhook", "url": "https://example.com/hook"})
        assert ch.type == "webhook"
        assert ch.url == "https://example.com/hook"

    def test_desktop_channel_needs_no_extra_fields(self):
        ch = NotifyChannel.model_validate({"type": "desktop"})
        assert ch.type == "desktop"

    def test_invalid_channel_type_raises(self):
        with pytest.raises(ValidationError):
            NotifyChannel.model_validate({"type": "slack"})

    def test_notify_events_validated(self):
        """Only valid NotifyEvent literals should be accepted in notify.on."""
        with pytest.raises(ValidationError):
            ProjectBook.model_validate(
                {
                    "name": "T",
                    "prompt": "P",
                    "notify": {"on": ["complete", "nonexistent_event"]},
                }
            )
