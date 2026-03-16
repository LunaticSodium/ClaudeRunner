"""
tests/test_self_test_yaml.py

Validates that projects/self-test.yaml parses correctly and passes
pydantic ProjectBook validation.
"""
from __future__ import annotations

import pathlib

import pytest

from claude_runner.project import ProjectBook, load_project_book


SELF_TEST_YAML = pathlib.Path(__file__).parent.parent / "projects" / "self-test.yaml"


class TestSelfTestYaml:
    def test_file_exists(self):
        assert SELF_TEST_YAML.exists(), f"self-test.yaml not found at {SELF_TEST_YAML}"

    def test_parses_and_validates(self):
        """self-test.yaml must load and pass full pydantic ProjectBook validation."""
        book = load_project_book(SELF_TEST_YAML)
        assert isinstance(book, ProjectBook)

    def test_name(self):
        book = load_project_book(SELF_TEST_YAML)
        assert book.name == "self-test"

    def test_description(self):
        book = load_project_book(SELF_TEST_YAML)
        assert "self-diagnostic" in book.description

    def test_prompt_contains_pytest(self):
        book = load_project_book(SELF_TEST_YAML)
        assert "pytest" in book.prompt

    def test_sandbox_native(self):
        book = load_project_book(SELF_TEST_YAML)
        assert book.sandbox is not None
        assert book.sandbox.backend == "native"

    def test_execution_timeout(self):
        book = load_project_book(SELF_TEST_YAML)
        assert book.execution.timeout_hours == 2.0

    def test_execution_skip_permissions(self):
        book = load_project_book(SELF_TEST_YAML)
        assert book.execution.skip_permissions is True

    def test_acceptance_criteria_checks(self):
        book = load_project_book(SELF_TEST_YAML)
        assert book.acceptance_criteria is not None
        checks = book.acceptance_criteria.checks
        check_types = [c.type for c in checks]
        assert "file_exists" in check_types
        assert "command" in check_types

    def test_acceptance_file_exists_path(self):
        book = load_project_book(SELF_TEST_YAML)
        file_checks = [c for c in book.acceptance_criteria.checks if c.type == "file_exists"]
        paths = [c.path for c in file_checks]
        assert ".claude-runner/self-test-report.md" in paths

    def test_notify_on_events(self):
        book = load_project_book(SELF_TEST_YAML)
        assert "complete" in book.notify.on
        assert "error" in book.notify.on

    def test_notify_webhook_channel(self):
        book = load_project_book(SELF_TEST_YAML)
        channels = book.notify.channels
        webhook_channels = [c for c in channels if c.type == "webhook"]
        assert len(webhook_channels) >= 1
        assert "ntfy.sh" in (webhook_channels[0].url or "")
