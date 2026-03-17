# CCCS Parser — Requirements Specification
# For implementation by Claude Code in the claude-runner repository

## 1. Purpose

A Python module (`cccs_parser.py`) that reads `.cccs.toml` files and produces two outputs:
1. A CLAUDE.md fragment (string) for injection into the agent's context.
2. A schema validator (callable) that checks project YAML files for structural compliance.

This module is invoked by Runner during session initialisation, BEFORE the first `claude -p` call.

## 2. Runtime Requirements

- Python ≥ 3.11 (uses `tomllib` from stdlib for reading; no external dependency for parse)
- `tomli-w` only if the Runner ever needs to WRITE .cccs.toml (not required for v1.0)
- YAML parsing: `pyyaml` or `ruamel.yaml` (already a runner dependency)
- No other external dependencies

## 3. Interface

```python
class CCCSSpec:
    """Parsed representation of a .cccs.toml file."""

    @classmethod
    def from_file(cls, path: str | Path) -> "CCCSSpec":
        """Load and validate a .cccs.toml file. Raises CCCSParseError on malformed input."""

    def render_claudemd(self, profile: str = "scisim") -> str:
        """
        Render the CLAUDE.md fragment for the given tail profile.
        
        Returns a string using <xml_tag> delimiters per the syntax.section_tags list.
        The output MUST be ≤ spec.header.max_rendered_lines lines.
        If the rendered output exceeds this limit, raise CCCSRenderError.
        """

    def get_schema_validator(self) -> Callable[[dict], list[str]]:
        """
        Return a callable that accepts a parsed project YAML dict
        and returns a list of validation error strings (empty = valid).
        
        Checks:
        - All schema.required_keys are present
        - Each phase contains all schema.required_phase_keys
        - Environment fields in schema.environment.required are present
        - Profile field matches one of the tail profile names
        """

    def get_tail_gates(self, profile: str) -> dict:
        """
        Return the tail gate thresholds for the given profile as a flat dict.
        Runner uses these to construct external verification commands.
        
        Example return for profile="scisim":
        {
            "build.exit_code": 0,
            "build.warnings_as_errors": True,
            "test.exit_code": 0,
            "test.min_coverage_core": 80,
            "test.min_coverage_adapter": 60,
            "test.min_coverage_numerics": 100,
            "numerical.min_ensemble_central": 500,
            "numerical.min_ensemble_variance": 1000,
            "numerical.min_ensemble_tail": 10000,
            "numerical.mcse_required": True,
            "numerical.convergence_check": True,
            "numerical.default_precision": "float64",
            "validation.min_analytical": 1,
            "reproducibility.config_required": True,
            "reproducibility.metadata_logged": True,
            "reproducibility.seed_logged": True,
            ...
        }
        """

    def get_runner_params(self) -> dict:
        """Return the [runner] section as a dict for Runner configuration."""
```

## 4. CLAUDE.md Rendering Rules

The `render_claudemd()` method MUST:

1. Start with `header.bootstrap_preamble` verbatim.
2. For each tag in `syntax.section_tags`, emit `<tag>` and `</tag>` delimiters.
3. Inside each tag, emit rules from `format.<tag>.rules` in order.
4. Prefix each rule with its enforcement level: `- MUST:` or `- SHOULD:` or `- MAY:`.
5. Do NOT include `ref`, `note`, or any metadata in the rendered output — these waste token budget.
6. Count lines of the final output. If > `header.max_rendered_lines`, raise `CCCSRenderError` with the overflow count.
7. Return the rendered string with a trailing newline.

Example output fragment:
```
You are operating under CCCS v1.0 (Claude Code C# Standards for Scientific Simulation).
This document uses <section_name> XML tags to delimit constraint categories.
Rules marked MUST are non-negotiable (RFC 2119). Rules marked SHOULD are strong defaults.
When in doubt, prefer correctness over performance, explicitness over brevity.

<architecture>
- MUST: Simulation core has ZERO external dependencies — references only System, System.Collections.Generic, System.Numerics, System.Linq.
- MUST: All engine/UI/IO interaction flows through port interfaces (e.g. IRenderer, IDataSink). Core never calls concrete implementations directly.
...
</architecture>

<numerical_standards>
...
</numerical_standards>
```

## 5. Schema Validation Rules

The validator returned by `get_schema_validator()` MUST check:

1. **Required top-level keys**: All keys listed in `schema.required_keys` must exist in the YAML dict, using dot-notation path traversal (e.g. `"project.name"` means `yaml["project"]["name"]`).
2. **Phase structure**: Each element in the `phases` array must contain all `schema.required_phase_keys`.
3. **Profile validity**: The value of `project.profile` must match a key under `tail.*` in the TOML.
4. **Environment fields**: All keys in `schema.environment.required` must exist under the YAML's environment section.
5. Return a `list[str]` of human-readable error messages. Empty list = valid.

## 6. Error Handling

Define two exception classes:
- `CCCSParseError(Exception)` — raised when the .cccs.toml file is malformed or missing required sections.
- `CCCSRenderError(Exception)` — raised when the rendered CLAUDE.md exceeds line limits.

Both must include the problematic field path in the error message.

## 7. Testing Requirements

- Unit tests in `test_cccs_parser.py` using pytest.
- Test cases:
  - Load the actual `cccs-v1.0.cccs.toml` and verify it parses without error.
  - Render CLAUDE.md and verify line count ≤ 150.
  - Render CLAUDE.md and verify all section_tags appear as `<tag>` / `</tag>` pairs.
  - Validate a minimal valid project YAML → empty error list.
  - Validate a YAML missing `project.name` → error list contains "project.name".
  - Validate a YAML with unknown profile → error list contains "profile".
  - `get_tail_gates("scisim")` returns dict with `"numerical.min_ensemble_central"` = 500.
  - `get_tail_gates("engineering")` returns dict WITHOUT `"numerical.*"` keys.
  - `get_runner_params()` returns dict with `"compact_trigger_pct"` = 55.

## 8. File Placement in Repository

```
claude-runner/
├── runner/
│   ├── cccs_parser.py          # this module
│   └── presets/
│       └── cccs-v1.0.cccs.toml # the spec file
├── tests/
│   └── test_cccs_parser.py     # tests
└── references/
    └── cccs-v1.0.bib           # bibliography
```

## 9. Integration Point

Runner's session init sequence becomes:

```python
# In runner's session_init():
from runner.cccs_parser import CCCSSpec

spec = CCCSSpec.from_file("runner/presets/cccs-v1.0.cccs.toml")

# 1. Validate project YAML
errors = spec.get_schema_validator()(project_yaml)
if errors:
    raise ProjectYAMLError(errors)

# 2. Render and inject CLAUDE.md fragment
profile = project_yaml["project"]["profile"]
fragment = spec.render_claudemd(profile=profile)
inject_into_claudemd(fragment)

# 3. Get tail gates for external verification
gates = spec.get_tail_gates(profile)
store_gates_for_phase_verification(gates)

# 4. Configure runner parameters
runner_params = spec.get_runner_params()
apply_runner_config(runner_params)
```
