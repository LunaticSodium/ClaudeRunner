"""Tests for the first-run auto-configure flow and new Config defaults.

Covers:
- Config.cccs_enabled / marathon_mode_default defaults and yaml loading
- _save_to_config_yaml() merges without clobbering existing keys
- configure wizard writes the right keys when user says yes/no
- first-run auto-configure invokes wizard (and does not recurse)
"""

from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from claude_runner.config import Config
from claude_runner.main import cli


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

class TestConfigDefaults:
    def test_cccs_enabled_defaults_false(self) -> None:
        assert Config().cccs_enabled is False

    def test_marathon_mode_default_defaults_false(self) -> None:
        assert Config().marathon_mode_default is False

    def test_cccs_enabled_loaded_from_yaml(self, tmp_path: pathlib.Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("cccs_enabled: true\n", encoding="utf-8")
        cfg = Config()
        cfg._apply_dict({"cccs_enabled": True}, source=str(cfg_file))
        assert cfg.cccs_enabled is True

    def test_marathon_mode_default_loaded_from_yaml(self, tmp_path: pathlib.Path) -> None:
        cfg = Config()
        cfg._apply_dict({"marathon_mode_default": True}, source="test")
        assert cfg.marathon_mode_default is True

    def test_unknown_key_ignored(self) -> None:
        cfg = Config()
        # Should not raise
        cfg._apply_dict({"totally_unknown_key": 42}, source="test")
        assert not hasattr(cfg, "totally_unknown_key")

    def test_both_fields_false_by_default(self) -> None:
        cfg = Config()
        assert cfg.cccs_enabled is False
        assert cfg.marathon_mode_default is False


# ---------------------------------------------------------------------------
# _save_to_config_yaml
# ---------------------------------------------------------------------------

class TestSaveToConfigYaml:
    def _invoke_save(self, tmp_home: pathlib.Path, updates: dict) -> None:
        """Call _save_to_config_yaml with a patched config dir."""
        import claude_runner.main as m  # noqa: PLC0415
        orig = m._DEFAULT_CONFIG_FILE
        try:
            m._DEFAULT_CONFIG_FILE = tmp_home / "config.yaml"
            m._save_to_config_yaml(updates)
        finally:
            m._DEFAULT_CONFIG_FILE = orig

    def test_creates_file_when_missing(self, tmp_path: pathlib.Path) -> None:
        self._invoke_save(tmp_path, {"cccs_enabled": True})
        data = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert data["cccs_enabled"] is True

    def test_merges_with_existing_keys(self, tmp_path: pathlib.Path) -> None:
        cfg = tmp_path / "config.yaml"
        cfg.write_text("sandbox_backend: native\n", encoding="utf-8")
        self._invoke_save(tmp_path, {"cccs_enabled": True})
        data = yaml.safe_load(cfg.read_text())
        assert data["sandbox_backend"] == "native"   # preserved
        assert data["cccs_enabled"] is True           # new key added

    def test_overwrites_existing_value(self, tmp_path: pathlib.Path) -> None:
        cfg = tmp_path / "config.yaml"
        cfg.write_text("cccs_enabled: false\n", encoding="utf-8")
        self._invoke_save(tmp_path, {"cccs_enabled": True})
        data = yaml.safe_load(cfg.read_text())
        assert data["cccs_enabled"] is True

    def test_does_not_touch_file_on_empty_dict(self, tmp_path: pathlib.Path) -> None:
        # An empty update dict should still be a no-op (file written but identical)
        self._invoke_save(tmp_path, {})
        cfg = tmp_path / "config.yaml"
        assert cfg.exists()
        data = yaml.safe_load(cfg.read_text()) or {}
        assert data == {}


# ---------------------------------------------------------------------------
# Configure wizard — Step 3 (feature defaults)
# ---------------------------------------------------------------------------

class TestConfigureWizardFeatureStep:
    """Test Step 3 (default features) of the configure wizard.

    We drive the full wizard via Click's test runner, feeding just enough
    stdin to answer all prompts.  External I/O (keyring, SMTP, ntfy) is
    stubbed.  _detect_oauth_session is imported inside configure() so it
    must be patched at the source module (claude_runner.config).
    """

    def _run_configure(self, tmp_path: pathlib.Path, inputs: str):
        runner = CliRunner()
        marker = tmp_path / ".initialized"
        marker.touch()  # not a first run — avoid auto-configure recursion

        patches = [
            patch("claude_runner.main._DEFAULT_CONFIG_DIR", tmp_path),
            patch("claude_runner.main._DEFAULT_CONFIG_FILE", tmp_path / "config.yaml"),
            patch("claude_runner.main._DEFAULT_SECRETS_FILE", tmp_path / "secrets.yaml"),
            patch("claude_runner.main._DEFAULT_LOG_DIR", tmp_path / "logs"),
            patch("claude_runner.main._DEFAULT_STATE_DIR", tmp_path / "state"),
            patch("claude_runner.main._DEFAULT_TRASH_DIR", tmp_path / "trash"),
            patch("claude_runner.main._DEFAULT_PROJECTS_DIR", tmp_path / "projects"),
            patch("claude_runner.main._INITIALIZED_MARKER", marker),
            patch("claude_runner.main._check_docker_quick", return_value=True),
            patch("claude_runner.main._resolve_api_key", return_value="sk-ant-test1234"),
            # deferred import inside configure() — must patch at source
            patch("claude_runner.config._detect_oauth_session", return_value=True),
            patch("claude_runner.main._save_to_keyring", return_value=None),
            patch("claude_runner.main._save_to_secrets_yaml", return_value=None),
        ]

        with _apply_patches(patches):
            # OAuth active → no API key prompt (Step 1 silent)
            # notify=3 (skip, Step 2)
            # cccs=? marathon=? (Step 3 — provided by caller)
            # save=1 (Step 4)
            result = runner.invoke(cli, ["configure"], input=inputs, catch_exceptions=False)
        return result

    def test_cccs_yes_writes_config(self, tmp_path: pathlib.Path) -> None:
        # notify skip, cccs=y, marathon=n, save=1
        result = self._run_configure(tmp_path, "3\ny\nn\n1\n")
        assert result.exit_code == 0, result.output
        cfg_file = tmp_path / "config.yaml"
        assert cfg_file.exists(), "config.yaml not written"
        data = yaml.safe_load(cfg_file.read_text()) or {}
        assert data.get("cccs_enabled") is True

    def test_marathon_yes_writes_config(self, tmp_path: pathlib.Path) -> None:
        result = self._run_configure(tmp_path, "3\nn\ny\n1\n")
        assert result.exit_code == 0, result.output
        cfg_file = tmp_path / "config.yaml"
        assert cfg_file.exists(), "config.yaml not written"
        data = yaml.safe_load(cfg_file.read_text()) or {}
        assert data.get("marathon_mode_default") is True

    def test_both_no_does_not_write_feature_keys(self, tmp_path: pathlib.Path) -> None:
        result = self._run_configure(tmp_path, "3\nn\nn\n1\n")
        assert result.exit_code == 0, result.output
        cfg_file = tmp_path / "config.yaml"
        data: dict[str, Any] = yaml.safe_load(cfg_file.read_text()) or {} if cfg_file.exists() else {}
        assert data.get("cccs_enabled") is not True
        assert data.get("marathon_mode_default") is not True


# ---------------------------------------------------------------------------
# First-run auto-configure — test _ensure_initialized directly
# ---------------------------------------------------------------------------

class TestFirstRunAutoConfig:
    def test_first_run_invokes_configure(self, tmp_path: pathlib.Path) -> None:
        """_ensure_initialized() calls configure.callback() on first run."""
        import claude_runner.main as m  # noqa: PLC0415

        marker = tmp_path / ".initialized"
        configure_called: list[bool] = []

        patches = [
            patch.object(m, "_DEFAULT_CONFIG_DIR", tmp_path),
            patch.object(m, "_DEFAULT_LOG_DIR", tmp_path / "logs"),
            patch.object(m, "_DEFAULT_STATE_DIR", tmp_path / "state"),
            patch.object(m, "_DEFAULT_TRASH_DIR", tmp_path / "trash"),
            patch.object(m, "_DEFAULT_PROJECTS_DIR", tmp_path / "projects"),
            patch.object(m, "_INITIALIZED_MARKER", marker),
            patch.object(m, "_check_docker_quick", return_value=True),
            patch.object(m, "_resolve_api_key", return_value="sk-ant-test1234"),
            patch.object(m.configure, "callback", lambda: configure_called.append(True)),
        ]
        with _apply_patches(patches):
            m._ensure_initialized()

        assert configure_called, "configure.callback() was not called on first run"
        assert marker.exists(), "Initialized marker was not created"

    def test_second_run_skips_configure(self, tmp_path: pathlib.Path) -> None:
        """_ensure_initialized() does NOT call configure.callback() on second run."""
        import claude_runner.main as m  # noqa: PLC0415

        marker = tmp_path / ".initialized"
        marker.touch()  # already initialized
        configure_called: list[bool] = []

        patches = [
            patch.object(m, "_DEFAULT_CONFIG_DIR", tmp_path),
            patch.object(m, "_DEFAULT_LOG_DIR", tmp_path / "logs"),
            patch.object(m, "_DEFAULT_STATE_DIR", tmp_path / "state"),
            patch.object(m, "_DEFAULT_TRASH_DIR", tmp_path / "trash"),
            patch.object(m, "_DEFAULT_PROJECTS_DIR", tmp_path / "projects"),
            patch.object(m, "_INITIALIZED_MARKER", marker),
            patch.object(m, "_check_docker_quick", return_value=True),
            patch.object(m, "_resolve_api_key", return_value="sk-ant-test1234"),
            patch.object(m.configure, "callback", lambda: configure_called.append(True)),
        ]
        with _apply_patches(patches):
            m._ensure_initialized()

        assert not configure_called, "configure.callback() was unexpectedly called on second run"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from contextlib import contextmanager, ExitStack  # noqa: E402


@contextmanager
def _apply_patches(patch_list):
    with ExitStack() as stack:
        for p in patch_list:
            stack.enter_context(p)
        yield
