"""
Tests for claude_runner.constraint_checker module.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_runner.constraint_checker import (
    ConstraintResult,
    check_all_constraints,
    check_constraint,
)
from claude_runner.project import ConstraintVerifyBackend, ImplementationConstraint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _constraint(
    id: str = "test-c",
    description: str = "Test constraint",
    verify_with: ConstraintVerifyBackend = ConstraintVerifyBackend.file_contains,
    file: str | None = None,
    pattern: str | None = None,
    prompt: str | None = None,
) -> ImplementationConstraint:
    return ImplementationConstraint(
        id=id,
        description=description,
        verify_with=verify_with,
        file=file,
        pattern=pattern,
        prompt=prompt,
    )


# ---------------------------------------------------------------------------
# file_contains backend
# ---------------------------------------------------------------------------


class TestFileContains:
    def test_pattern_found(self, tmp_path):
        (tmp_path / "main.py").write_text("import redis\n")
        c = _constraint(
            verify_with=ConstraintVerifyBackend.file_contains,
            file="main.py",
            pattern=r"import redis",
        )
        result = check_constraint(c, tmp_path)
        assert result.passed is True
        assert "found" in result.reason

    def test_pattern_not_found(self, tmp_path):
        (tmp_path / "main.py").write_text("import sqlite3\n")
        c = _constraint(
            verify_with=ConstraintVerifyBackend.file_contains,
            file="main.py",
            pattern=r"import redis",
        )
        result = check_constraint(c, tmp_path)
        assert result.passed is False
        assert "not found" in result.reason

    def test_file_missing(self, tmp_path):
        c = _constraint(
            verify_with=ConstraintVerifyBackend.file_contains,
            file="no_such_file.py",
            pattern=r"redis",
        )
        result = check_constraint(c, tmp_path)
        assert result.passed is False
        assert "does not exist" in result.reason

    def test_missing_file_field(self, tmp_path):
        c = _constraint(
            verify_with=ConstraintVerifyBackend.file_contains,
            file=None,
            pattern=r"redis",
        )
        result = check_constraint(c, tmp_path)
        assert result.passed is False
        assert "missing 'file'" in result.reason

    def test_missing_pattern_field(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1\n")
        c = _constraint(
            verify_with=ConstraintVerifyBackend.file_contains,
            file="main.py",
            pattern=None,
        )
        result = check_constraint(c, tmp_path)
        assert result.passed is False
        assert "missing 'pattern'" in result.reason

    def test_invalid_regex(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1\n")
        c = _constraint(
            verify_with=ConstraintVerifyBackend.file_contains,
            file="main.py",
            pattern=r"[invalid(regex",
        )
        result = check_constraint(c, tmp_path)
        assert result.passed is False
        assert "invalid pattern" in result.reason

    def test_multiline_pattern(self, tmp_path):
        content = "def __init__(self):\n    self.cache = redis.Redis()\n"
        (tmp_path / "store.py").write_text(content)
        c = _constraint(
            verify_with=ConstraintVerifyBackend.file_contains,
            file="store.py",
            pattern=r"redis\.Redis\(",
        )
        result = check_constraint(c, tmp_path)
        assert result.passed is True


# ---------------------------------------------------------------------------
# llm_judge backend
# ---------------------------------------------------------------------------


class TestLlmJudge:
    def test_yes_verdict_passes(self, tmp_path):
        c = _constraint(
            verify_with=ConstraintVerifyBackend.llm_judge,
            prompt="Does the code use Redis?",
        )
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="YES")]

        with patch("anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = mock_response
            result = check_constraint(c, tmp_path, api_key="sk-test")

        assert result.passed is True
        assert "YES" in result.reason

    def test_no_verdict_fails(self, tmp_path):
        c = _constraint(
            verify_with=ConstraintVerifyBackend.llm_judge,
            prompt="Does the code use Redis?",
        )
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="NO")]

        with patch("anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = mock_response
            result = check_constraint(c, tmp_path, api_key="sk-test")

        assert result.passed is False

    def test_yes_case_insensitive(self, tmp_path):
        c = _constraint(
            verify_with=ConstraintVerifyBackend.llm_judge,
            prompt="Does the code use Redis?",
        )
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="yes, it does")]

        with patch("anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = mock_response
            result = check_constraint(c, tmp_path, api_key="sk-test")

        assert result.passed is True

    def test_missing_prompt_fails(self, tmp_path):
        c = _constraint(
            verify_with=ConstraintVerifyBackend.llm_judge,
            prompt=None,
        )
        result = check_constraint(c, tmp_path, api_key="sk-test")
        assert result.passed is False
        assert "missing 'prompt'" in result.reason

    def test_oauth_mode_no_env_passes(self, tmp_path, monkeypatch):
        """In OAuth mode without ANTHROPIC_API_KEY, check is skipped (PASS)."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        c = _constraint(
            verify_with=ConstraintVerifyBackend.llm_judge,
            prompt="Does the code use Redis?",
        )
        result = check_constraint(c, tmp_path, api_key="__claude_oauth__")
        assert result.passed is True

    def test_api_error_returns_failed(self, tmp_path):
        c = _constraint(
            verify_with=ConstraintVerifyBackend.llm_judge,
            prompt="Does the code use Redis?",
        )
        with patch("anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.side_effect = RuntimeError("API down")
            result = check_constraint(c, tmp_path, api_key="sk-test")

        assert result.passed is False
        assert "API call failed" in result.reason


# ---------------------------------------------------------------------------
# check_all_constraints
# ---------------------------------------------------------------------------


class TestCheckAllConstraints:
    def test_all_pass(self, tmp_path):
        (tmp_path / "app.py").write_text("import redis\n")
        constraints = [
            _constraint(id="c1", file="app.py", pattern=r"import redis"),
            _constraint(id="c2", file="app.py", pattern=r"redis"),
        ]
        results = check_all_constraints(constraints, tmp_path)
        assert len(results) == 2
        assert all(r.passed for r in results)

    def test_one_fails(self, tmp_path):
        (tmp_path / "app.py").write_text("import sqlite3\n")
        constraints = [
            _constraint(id="c1", file="app.py", pattern=r"import sqlite3"),
            _constraint(id="c2", file="app.py", pattern=r"import redis"),
        ]
        results = check_all_constraints(constraints, tmp_path)
        assert results[0].passed is True
        assert results[1].passed is False

    def test_empty_constraints_returns_empty_list(self, tmp_path):
        results = check_all_constraints([], tmp_path)
        assert results == []

    def test_result_ids_match_constraints(self, tmp_path):
        (tmp_path / "f.py").write_text("x = 1\n")
        constraints = [
            _constraint(id="alpha", file="f.py", pattern=r"x = 1"),
            _constraint(id="beta", file="f.py", pattern=r"missing"),
        ]
        results = check_all_constraints(constraints, tmp_path)
        assert results[0].id == "alpha"
        assert results[1].id == "beta"


# ---------------------------------------------------------------------------
# ConstraintResult dataclass
# ---------------------------------------------------------------------------


class TestConstraintResult:
    def test_str_pass(self):
        r = ConstraintResult(id="c1", passed=True, reason="found")
        assert "PASS" in str(r)
        assert "c1" in str(r)

    def test_str_fail(self):
        r = ConstraintResult(id="c1", passed=False, reason="not found")
        assert "FAIL" in str(r)
