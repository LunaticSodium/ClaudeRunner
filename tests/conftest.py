"""
conftest.py — Shared pytest fixtures for the claude-runner test suite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Directory / project-structure fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_project_dir(tmp_path: Path) -> Path:
    """
    Create a temporary directory that mimics a minimal project structure:
      <tmp>/
        workspace/        ← usable as sandbox.working_dir
        state/            ← usable as PersistenceManager state_dir
        .claude-runner/
          progress.log    ← empty placeholder
    Returns the root tmp directory.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    cr_dir = workspace / ".claude-runner"
    cr_dir.mkdir()
    (cr_dir / "progress.log").write_text("", encoding="utf-8")

    return tmp_path


# ---------------------------------------------------------------------------
# YAML string fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def minimal_project_book_yaml(tmp_project_dir: Path) -> str:
    """
    Minimal valid project book YAML string.

    Only the two required top-level fields (name, prompt) are set;
    sandbox.working_dir points to the temp workspace so the path validator passes.
    """
    workspace = tmp_project_dir / "workspace"
    return f"""\
name: Minimal Task
prompt: Do something minimal.
sandbox:
  working_dir: "{workspace.as_posix()}"
"""


@pytest.fixture()
def full_project_book_yaml(tmp_project_dir: Path) -> str:
    """
    Full valid project book YAML with all optional blocks populated.
    """
    workspace = tmp_project_dir / "workspace"
    return f"""\
name: Full Task
description: A fully-specified task for testing.
prompt: |
  Implement the feature described in the spec.
  Make sure all tests pass.
sandbox:
  working_dir: "{workspace.as_posix()}"
  readonly_mounts:
    - path: "{workspace.as_posix()}"
      mount_as: /ref/workspace
  network:
    allow:
      - api.anthropic.com
      - github.com
    deny_all_others: true
execution:
  timeout_hours: 8.0
  max_rate_limit_waits: 10
  resume_strategy: restate
  skip_permissions: false
  context:
    checkpoint_threshold_tokens: 100000
    reset_on_rate_limit: true
    inject_log_on_resume: true
  milestones:
    - pattern: "All tests passed"
      message: "Test suite green"
    - pattern: "DONE"
      message: "Task complete marker detected"
output:
  git:
    enabled: true
    branch_prefix: "claude-task/"
    auto_push: false
  log_dir: "{(tmp_project_dir / 'logs').as_posix()}"
notify:
  on:
    - complete
    - error
  channels:
    - type: email
      to: test@example.com
    - type: desktop
    - type: webhook
      url: https://hooks.example.com/notify
"""


# ---------------------------------------------------------------------------
# Apprise mock fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_apprise():
    """
    Patch the apprise module so that no real notifications are sent.

    Yields a MagicMock that replaces the apprise module object returned by
    NotificationManager._try_import_apprise().

    Usage:
        def test_something(mock_apprise):
            nm = NotificationManager(...)
            nm._apprise_module = mock_apprise
            ...
            mock_apprise.Apprise.return_value.notify.assert_called_once()
    """
    mock_module = MagicMock()
    # Make Apprise() instances return a mock with notify() that succeeds.
    mock_instance = MagicMock()
    mock_instance.notify.return_value = True
    mock_module.Apprise.return_value = mock_instance

    with patch.dict("sys.modules", {"apprise": mock_module}):
        yield mock_module
