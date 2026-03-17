"""CCCS — Claude Code C# Standards parser.

Reads a ``.cccs.toml`` file and provides:

- ``render_claudemd()``      → CLAUDE.md fragment string for injection
- ``get_schema_validator()`` → callable that validates project YAML dicts
- ``get_tail_gates()``       → flat dict of hard acceptance thresholds
- ``get_tail_profile_names()`` → list of known profile names
- ``get_runner_params()``    → dict of runner configuration values

This module has NO external runtime dependencies beyond the Python 3.11 stdlib
(uses ``tomllib``).  ``pyyaml`` / ``ruamel.yaml`` are NOT imported here;
callers already have a parsed YAML dict by the time they call the validator.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CCCSParseError(Exception):
    """Raised when a .cccs.toml file is malformed or missing required sections."""


class CCCSRenderError(Exception):
    """Raised when the rendered CLAUDE.md fragment exceeds the configured line limit."""


# ---------------------------------------------------------------------------
# Required top-level TOML sections
# ---------------------------------------------------------------------------

_REQUIRED_SECTIONS: tuple[str, ...] = (
    "meta",
    "header",
    "syntax",
    "format",
    "tail",
    "runner",
    "schema",
)

# ---------------------------------------------------------------------------
# CCCSSpec
# ---------------------------------------------------------------------------

class CCCSSpec:
    """Parsed and validated representation of a ``.cccs.toml`` file."""

    def __init__(self, data: dict[str, Any], source_path: Path) -> None:
        self._data = data
        self._source_path = source_path

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_file(cls, path: str | Path) -> "CCCSSpec":
        """Load and validate a ``.cccs.toml`` file.

        Raises :class:`CCCSParseError` when the file is missing, not valid
        TOML, or lacks a required top-level section.
        """
        p = Path(path)
        try:
            with open(p, "rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            raise CCCSParseError(f"File not found: {path}")
        except tomllib.TOMLDecodeError as exc:
            raise CCCSParseError(f"TOML parse error in {path}: {exc}")

        for section in _REQUIRED_SECTIONS:
            if section not in data:
                raise CCCSParseError(
                    f"Missing required section [{section}] in {path}"
                )

        header = data["header"]
        for key in ("max_rendered_lines", "bootstrap_preamble"):
            if key not in header:
                raise CCCSParseError(f"Missing header.{key} in {path}")

        syntax = data["syntax"]
        if "section_tags" not in syntax:
            raise CCCSParseError(f"Missing syntax.section_tags in {path}")

        return cls(data, p)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render_claudemd(self, profile: str | None = None) -> str:
        """Render the CLAUDE.md fragment for the given tail *profile*.

        Returns a string using ``<tag>`` / ``</tag>`` delimiters for each
        section listed in ``syntax.section_tags``.

        Raises :class:`CCCSRenderError` when the output exceeds
        ``header.max_rendered_lines``.
        """
        if profile is None:
            profile = self._data.get("default_profile", "scisim")

        header: dict[str, Any] = self._data["header"]
        max_lines: int = header["max_rendered_lines"]
        preamble: str = header["bootstrap_preamble"].strip()

        section_tags: list[str] = self._data["syntax"]["section_tags"]
        format_data: dict[str, Any] = self._data.get("format", {})

        lines: list[str] = [preamble, ""]

        for tag in section_tags:
            tag_data: dict[str, Any] = format_data.get(tag, {})
            rules: list[dict[str, Any]] = tag_data.get("rules", [])
            if not rules:
                continue
            lines.append(f"<{tag}>")
            for rule in rules:
                level: str = rule.get("level", "MUST")
                text: str = rule.get("text", "")
                lines.append(f"- {level}: {text}")
            lines.append(f"</{tag}>")
            lines.append("")

        # Trim trailing blank lines, then add exactly one trailing newline.
        while lines and lines[-1] == "":
            lines.pop()

        output = "\n".join(lines) + "\n"
        line_count = output.count("\n")

        if line_count > max_lines:
            overflow = line_count - max_lines
            raise CCCSRenderError(
                f"Rendered CLAUDE.md fragment is {line_count} lines, "
                f"exceeds limit of {max_lines} by {overflow} lines."
            )

        return output

    # ------------------------------------------------------------------
    # Schema validation
    # ------------------------------------------------------------------

    def get_schema_validator(self) -> Callable[[dict[str, Any]], list[str]]:
        """Return a callable that validates a parsed project YAML dict.

        The callable returns a list of human-readable error strings.
        An empty list means the YAML is valid.
        """
        schema: dict[str, Any] = self._data["schema"]
        required_keys: list[str] = schema.get("required_keys", [])
        required_phase_keys: list[str] = schema.get("required_phase_keys", [])
        env_required: list[str] = schema.get("environment", {}).get("required", [])
        valid_profiles: list[str] = list(self._data.get("tail", {}).keys())

        def _validator(yaml_dict: dict[str, Any]) -> list[str]:
            errors: list[str] = []

            # 1. Required top-level keys (dot-notation path traversal)
            for dotted_key in required_keys:
                parts = dotted_key.split(".")
                node: Any = yaml_dict
                found = True
                for part in parts:
                    if not isinstance(node, dict) or part not in node:
                        errors.append(f"Missing required key: {dotted_key}")
                        found = False
                        break
                    node = node[part]
                _ = found  # silence unused-variable warning

            # 2. Phase structure
            phases = yaml_dict.get("phases", [])
            if isinstance(phases, list):
                for i, phase in enumerate(phases):
                    if not isinstance(phase, dict):
                        errors.append(f"phases[{i}] is not a mapping")
                        continue
                    for pk in required_phase_keys:
                        if pk not in phase:
                            errors.append(
                                f"phases[{i}] missing required key: {pk}"
                            )

            # 3. Profile validity
            project_section = yaml_dict.get("project", {})
            if isinstance(project_section, dict):
                profile_val = project_section.get("profile")
                if profile_val is not None and profile_val not in valid_profiles:
                    errors.append(
                        f"project.profile '{profile_val}' is not a known profile "
                        f"(valid: {valid_profiles})"
                    )

            # 4. Environment fields
            env_section = yaml_dict.get("environment", {})
            if isinstance(env_section, dict):
                for ef in env_required:
                    if ef not in env_section:
                        errors.append(
                            f"environment.{ef} is required but missing"
                        )

            return errors

        return _validator

    # ------------------------------------------------------------------
    # Tail gates
    # ------------------------------------------------------------------

    def get_tail_gates(self, profile: str) -> dict[str, Any]:
        """Return the tail gate thresholds for *profile* as a flat dict.

        Keys use dot notation with the gate-category prefix stripped of
        the ``_gate`` suffix, e.g. ``"numerical.min_ensemble_central"``.

        Raises :class:`CCCSParseError` for unknown profiles.
        """
        tail: dict[str, Any] = self._data.get("tail", {})
        if profile not in tail:
            raise CCCSParseError(
                f"Unknown tail profile: '{profile}' "
                f"(available: {list(tail.keys())})"
            )

        profile_data: dict[str, Any] = tail[profile]
        result: dict[str, Any] = {}

        def _flatten(obj: dict[str, Any], prefix: str) -> None:
            for k, v in obj.items():
                full_key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    _flatten(v, full_key)
                else:
                    result[full_key] = v

        for section_name, section_val in profile_data.items():
            if isinstance(section_val, dict):
                # Strip "_gate" suffix for cleaner keys:
                # "build_gate" → "build", "test_gate" → "test", etc.
                prefix = section_name.removesuffix("_gate")
                _flatten(section_val, prefix)
            # Scalar fields like profile_name / description are intentionally skipped.

        return result

    def get_tail_profile_names(self) -> list[str]:
        """Return the list of profile names defined in the [tail] section."""
        return list(self._data.get("tail", {}).keys())

    # ------------------------------------------------------------------
    # Runner params
    # ------------------------------------------------------------------

    def get_runner_params(self) -> dict[str, Any]:
        """Return the ``[runner]`` section as a flat dict."""
        return dict(self._data.get("runner", {}))

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def default_profile(self) -> str:
        """The default tail profile name (from ``default_profile`` key)."""
        return str(self._data.get("default_profile", "scisim"))

    @property
    def version(self) -> str:
        """Spec version string from ``[meta]``."""
        return str(self._data.get("meta", {}).get("version", "unknown"))

    @property
    def spec_name(self) -> str:
        """Short name of the spec (e.g. ``"CCCS"``)."""
        return str(self._data.get("meta", {}).get("spec_name", "CCCS"))

    @property
    def source_path(self) -> Path:
        """Absolute path to the loaded ``.cccs.toml`` file."""
        return self._source_path


# ---------------------------------------------------------------------------
# Convenience: resolve a preset by short name
# ---------------------------------------------------------------------------

_PRESETS_DIR = Path(__file__).parent / "presets"


def load_preset(name: str) -> CCCSSpec:
    """Load a built-in preset by short name (e.g. ``"cccs-v1.0"``).

    Looks in ``claude_runner/presets/<name>.cccs.toml``.

    Raises :class:`CCCSParseError` if the preset file is not found.
    """
    candidate = _PRESETS_DIR / f"{name}.cccs.toml"
    if not candidate.exists():
        available = [p.stem.removesuffix(".cccs") for p in _PRESETS_DIR.glob("*.cccs.toml")]
        raise CCCSParseError(
            f"Preset '{name}' not found in {_PRESETS_DIR}. "
            f"Available: {available}"
        )
    return CCCSSpec.from_file(candidate)
