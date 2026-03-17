"""Tests for claude_runner.cccs_parser.

Covers:
- Loading and validating the real cccs-v1.0.cccs.toml preset
- CLAUDE.md rendering (line count, tag pairs, preamble)
- Schema validation (valid YAML, missing keys, unknown profile)
- get_tail_gates() for scisim and engineering profiles
- get_runner_params() content
- CCCSParseError / CCCSRenderError exception paths
- CccsConfig model in project.py
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from claude_runner.cccs_parser import (
    CCCSParseError,
    CCCSRenderError,
    CCCSSpec,
    load_preset,
)
from claude_runner.project import CccsConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PRESETS_DIR = Path(__file__).parent.parent / "claude_runner" / "presets"
_REAL_TOML = _PRESETS_DIR / "cccs-v1.0.cccs.toml"


def _minimal_valid_yaml() -> dict[str, Any]:
    return {
        "project": {
            "name": "Test Project",
            "description": "A test",
            "presets": ["cccs-v1.0"],
            "profile": "scisim",
        },
        "phases": [
            {
                "name": "Phase 1",
                "deliverables": ["something"],
                "hard_acceptance_criteria": ["H1: build passes"],
            }
        ],
        "environment": {
            "dotnet_version": "8.0",
            "target_framework": "net8.0",
        },
    }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

class TestFromFile:
    def test_loads_real_preset_without_error(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        assert spec is not None

    def test_load_preset_convenience(self) -> None:
        spec = load_preset("cccs-v1.0")
        assert spec.spec_name == "CCCS"
        assert spec.version == "1.0.0"

    def test_missing_file_raises_parse_error(self, tmp_path: Path) -> None:
        with pytest.raises(CCCSParseError, match="not found"):
            CCCSSpec.from_file(tmp_path / "nonexistent.cccs.toml")

    def test_invalid_toml_raises_parse_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.cccs.toml"
        bad.write_text("this is not valid toml ===", encoding="utf-8")
        with pytest.raises(CCCSParseError, match="TOML parse error"):
            CCCSSpec.from_file(bad)

    def test_missing_required_section_raises_parse_error(self, tmp_path: Path) -> None:
        # Valid TOML but missing the [meta] section
        incomplete = tmp_path / "incomplete.cccs.toml"
        incomplete.write_text(
            "[header]\nmax_rendered_lines = 150\nbootstrap_preamble = 'hi'\n"
            "[syntax]\nsection_tags = []\n[format]\n[tail]\n[runner]\n[schema]\n",
            encoding="utf-8",
        )
        with pytest.raises(CCCSParseError, match=r"\[meta\]"):
            CCCSSpec.from_file(incomplete)

    def test_load_preset_unknown_name_raises_parse_error(self) -> None:
        with pytest.raises(CCCSParseError, match="not found"):
            load_preset("nonexistent-preset")

    def test_source_path_is_absolute(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        assert spec.source_path.is_absolute()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestRenderClaudeMd:
    def test_line_count_within_limit(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        fragment = spec.render_claudemd()
        line_count = fragment.count("\n")
        assert line_count <= 150, f"Fragment has {line_count} lines, limit is 150"

    def test_all_section_tags_present_as_pairs(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        fragment = spec.render_claudemd()
        tags = spec._data["syntax"]["section_tags"]
        for tag in tags:
            # Not all tags may have rules, but those that do must be paired
            if f"<{tag}>" in fragment:
                assert f"</{tag}>" in fragment, f"Missing closing tag </{tag}>"

    def test_preamble_present(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        fragment = spec.render_claudemd()
        assert "CCCS v1.0" in fragment

    def test_ends_with_newline(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        fragment = spec.render_claudemd()
        assert fragment.endswith("\n")

    def test_must_prefix_on_must_rules(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        fragment = spec.render_claudemd()
        assert "- MUST:" in fragment

    def test_render_error_when_over_limit(self, tmp_path: Path) -> None:
        # Build a toml where max_rendered_lines = 1 so it always overflows
        content = textwrap.dedent("""\
            [meta]
            spec_name = "TEST"
            version = "0.1.0"
            language = "C#"
            runtime = ".NET 8"
            domain = "test"
            license = "MIT"
            authors = []
            bibliography = ""
            default_profile = "eng"

            [header]
            max_rendered_lines = 1
            bootstrap_preamble = "hi"

            [syntax]
            section_tags = ["rules"]

            [[format.rules.rules]]
            level = "MUST"
            text = "do something"

            [tail.eng]
            profile_name = "Engineering"

            [runner]

            [schema]
        """)
        p = tmp_path / "tiny.cccs.toml"
        p.write_text(content, encoding="utf-8")
        spec = CCCSSpec.from_file(p)
        with pytest.raises(CCCSRenderError, match="exceeds limit"):
            spec.render_claudemd(profile="eng")

    def test_default_profile_used_when_none_given(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        assert spec.default_profile == "scisim"
        # Should not raise — scisim is valid
        fragment = spec.render_claudemd()
        assert fragment


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class TestGetSchemaValidator:
    def test_valid_yaml_returns_empty_list(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        validator = spec.get_schema_validator()
        errors = validator(_minimal_valid_yaml())
        assert errors == []

    def test_missing_project_name_returns_error(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        validator = spec.get_schema_validator()
        yaml = _minimal_valid_yaml()
        del yaml["project"]["name"]
        errors = validator(yaml)
        assert any("project.name" in e for e in errors)

    def test_missing_phases_key_returns_error(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        validator = spec.get_schema_validator()
        yaml = _minimal_valid_yaml()
        del yaml["phases"]
        errors = validator(yaml)
        assert any("phases" in e for e in errors)

    def test_phase_missing_required_key_returns_error(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        validator = spec.get_schema_validator()
        yaml = _minimal_valid_yaml()
        del yaml["phases"][0]["hard_acceptance_criteria"]
        errors = validator(yaml)
        assert any("hard_acceptance_criteria" in e for e in errors)

    def test_unknown_profile_returns_error(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        validator = spec.get_schema_validator()
        yaml = _minimal_valid_yaml()
        yaml["project"]["profile"] = "nonexistent_profile"
        errors = validator(yaml)
        assert any("profile" in e for e in errors)

    def test_valid_engineering_profile_passes(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        validator = spec.get_schema_validator()
        yaml = _minimal_valid_yaml()
        yaml["project"]["profile"] = "engineering"
        errors = validator(yaml)
        assert errors == []

    def test_missing_environment_required_key(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        validator = spec.get_schema_validator()
        yaml = _minimal_valid_yaml()
        del yaml["environment"]["dotnet_version"]
        errors = validator(yaml)
        assert any("dotnet_version" in e for e in errors)


# ---------------------------------------------------------------------------
# Tail gates
# ---------------------------------------------------------------------------

class TestGetTailGates:
    def test_scisim_has_min_ensemble_central(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        gates = spec.get_tail_gates("scisim")
        assert gates.get("numerical.min_ensemble_central_tendency") == 500

    def test_scisim_has_mcse_required(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        gates = spec.get_tail_gates("scisim")
        assert gates.get("numerical.mcse_must_be_reported") is True

    def test_engineering_has_no_numerical_keys(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        gates = spec.get_tail_gates("engineering")
        numerical_keys = [k for k in gates if k.startswith("numerical.")]
        assert numerical_keys == []

    def test_unknown_profile_raises_parse_error(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        with pytest.raises(CCCSParseError, match="Unknown tail profile"):
            spec.get_tail_gates("does_not_exist")

    def test_get_tail_profile_names(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        names = spec.get_tail_profile_names()
        assert "scisim" in names
        assert "engineering" in names


# ---------------------------------------------------------------------------
# Runner params
# ---------------------------------------------------------------------------

class TestGetRunnerParams:
    def test_compact_trigger_pct_is_55(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        params = spec.get_runner_params()
        assert params.get("compact_trigger_pct") == 55

    def test_returns_dict(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        params = spec.get_runner_params()
        assert isinstance(params, dict)

    def test_max_retry_per_phase(self) -> None:
        spec = CCCSSpec.from_file(_REAL_TOML)
        params = spec.get_runner_params()
        assert params.get("max_retry_per_phase") == 3


# ---------------------------------------------------------------------------
# CccsConfig model
# ---------------------------------------------------------------------------

class TestCccsConfig:
    def test_defaults(self) -> None:
        cfg = CccsConfig()
        assert cfg.enabled is True
        assert cfg.preset == "cccs-v1.0"
        assert cfg.profile is None

    def test_enabled_false(self) -> None:
        cfg = CccsConfig(enabled=False)
        assert cfg.enabled is False

    def test_custom_preset_and_profile(self) -> None:
        cfg = CccsConfig(preset="cccs-v2.0", profile="engineering")
        assert cfg.preset == "cccs-v2.0"
        assert cfg.profile == "engineering"
