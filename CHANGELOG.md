# Changelog

All notable changes to `claude-runner` are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — 2026-05-15

This release consolidates a substantial body of work that lived in an
out-of-VCS working snapshot (the BTO project's robocopied `ClaudeRunner/`
folder) and brings the repository back to authoritative status. It also
includes the Linux / WSL2 bring-up patches and a token-counter accuracy
fix discovered during the FDTD grating-coupler project on 2026-05-15.

### Added

- **Supervisor protocol framework** (`claude_runner/supervisor_protocol.py`,
  `supervisor_audit.py`, `worker_supervisor.py`, `thinking_manual.py`).
  Two-tier supervision: a Marathon Runner (supervisor) drives one or more
  Dash Runner (worker) subprocesses with budget-gated interventions and
  dual-channel enforcement (ntfy out + cmd channels). Implements the
  v2.0 design previously outlined in `ADDENDUM.md`.
- **Supervisor spellbook preset** at
  `claude_runner/presets/supervisor-v2.0.spellbook.md` — agent-facing
  reference card for the supervisor protocol.
- **Model resolver** (`claude_runner/model_resolver.py`) — central alias
  map for Claude model IDs. Introduces `_KNOWN_ALIASES` so project books
  can specify human-friendly names like `claude-opus-4-6` and the runner
  resolves them at invocation time.
- **Pre-flight check module** (`claude_runner/preflight.py`) — programmatic
  validation hooks invoked before launch, complementing the user-side
  `prerun_check.ps1` / `prerun_check.sh` wrappers.
- **Constraint checker** (`claude_runner/constraint_checker.py`) — verifies
  project-book invariants beyond pydantic schema validation (e.g. that
  required acceptance commands exist on PATH inside the chosen sandbox).
- **KPI collector** (`claude_runner/kpi_collector.py`) — emits structured
  per-run metrics (token usage, rate-limit cycles, checkpoint count,
  wall-clock) for downstream analysis.
- **Module entry point** (`claude_runner/__main__.py`) — `python -m
  claude_runner ...` now works equivalently to the `claude-runner`
  console script, useful when the entry point is shadowed or absent.
- **Tests for new modules**: `test_constraint_checker.py`,
  `test_milestone_dedup.py`, `test_model_resolver.py`,
  `test_pause_resume.py`, `test_preflight.py`,
  `test_supervisor_protocol.py`.
- **Top-level test fixtures**: `marathon.yaml`, `self-test.yaml`,
  `test-proj.yaml` — sample project books used by the integration tests.
- **Design addendum** (`ADDENDUM.md`) is now part of the tracked tree
  (was an untracked design conversation from 2026-03-16).
- **Git LFS / line-ending hints** (`.gitattributes`).

### Changed

- **Prompt-size handling on Windows.** `NativeSandbox._build_command`
  now returns a `(cmd, stdin_text)` tuple. When the rendered prompt
  exceeds the Windows CLI limit (~7 500 chars after argv overhead),
  the runner invokes `claude -p -` and pipes the prompt through stdin
  instead of as an argv argument; otherwise it uses `-p <prompt>` as
  before. `PipeProcess` accepts and propagates the stdin payload.
- **Type-annotation modernisation.** All public surfaces use PEP 604
  (`T | None`) syntax instead of `Optional[T]`.
- Many updates across `acceptance_runner.py`, `cccs_parser.py`,
  `config.py`, `daemon.py`, `git_inbox.py`, `inbox.py`, `main.py`,
  `model_watchdog.py`, `notify.py`, `ntfy_client.py`, `persistence.py`,
  `pipeline.py`, `project.py`, `rate_limit.py`, `tui.py`, and
  `sandbox/{__init__,docker_sandbox}.py` to integrate with the
  supervisor protocol and adjacent additions.

### Fixed

- **`NativeSandbox._probe_sandbox_flag` false positive on Claude Code
  2.1.x and later.** The probe previously matched the bare word
  "sandbox" anywhere in `claude --help` (via `or "sandbox" in combined`),
  which triggered on the descriptions of
  `--allow-dangerously-skip-permissions` and
  `--dangerously-skip-permissions`. The runner then appended the
  non-existent `--sandbox` flag to the launch command, and Claude Code
  died with `unknown option '--sandbox'` after ~1.6 s. The probe now
  matches strictly on `"--sandbox" in combined`. (Reproduced and
  verified against Claude Code 2.1.138 inside WSL Ubuntu 26.04.)
- **`NativeSandbox.setup()` misleading warning on non-Windows.** The
  `CLAUDE_CODE_GIT_BASH_PATH` auto-detection block ran unconditionally;
  on Linux/macOS the candidate paths can't exist and the user saw a
  spurious "git bash may fail on Windows" warning every launch.
  Block is now gated behind `if sys.platform == "win32":`.
- **Token counter under-counting due to stream-json summary markers.**
  `PipeProcess._process_stream_event` delivers only short markers for
  tool roundtrip volume (`[Tool: <name>]`, `[·]`), so the runner's
  chars-to-tokens estimator was systematically blind to tool-input /
  tool-result bytes — leaving `_token_estimate` pinned at initial
  prompt size through hours of work. Fixed by parsing the
  `result.usage` field (authoritative counts from the Anthropic API)
  and routing it through a new `ContextManager.set_authoritative_tokens`
  setter. Mechanism:
  - `process.py` emits `##RUNNER:USAGE:<N>##` from each `result` event,
    where `N = input_tokens + cache_creation_input_tokens +
    cache_read_input_tokens + output_tokens`.
  - `context_manager.py` adds `set_authoritative_tokens(total)` with
    `_token_estimate = max(current, total)` semantics (high-water mark;
    never decreases).
  - `runner.py` adds `_USAGE_MARKER_RE` and routes matching lines to
    the new setter before falling back to `count_output()`.
- **`pywinpty` install failure on non-Windows platforms.** Moved to
  conditional dependency in `pyproject.toml`:
  `pywinpty>=2.0; sys_platform == 'win32'` and added
  `ptyprocess>=0.7; sys_platform != 'win32'`. Without this the
  `pip install -e .` step on Linux failed because pywinpty has no
  Linux wheel and its build requires the Windows toolchain. The runtime
  did not actually depend on pywinpty on Linux (`PipeProcess` uses pure
  `subprocess.Popen`, never `ClaudeProcess`'s PTY path) — only the
  install path was broken.

### Infrastructure

- `.gitignore` extended with `.vs/`, `.claude/`, `.claude-runner/`,
  `.mypy_cache/`, `.ruff_cache/`.
- This `CHANGELOG.md` introduced.

### Known limitations / not in this release

- Predictive rate-limit pause ("stop at 90% of the 5-hour budget") is
  **not** implemented. The Anthropic API does not expose remaining
  quota for the rolling-window rate limit; only a binary
  `status: "allowed" / rejected` and a `resetsAt` epoch. The runner's
  existing reactive flow (`rate_limit.py`: detect → sleep → resume)
  is the supported model. A heuristic implementation based on
  cumulative `total_cost_usd` from result events is feasible but
  deferred — see the discussion in the Grating-coupler HANDOFF.md
  for the trade-off analysis.

## [1.0.0] — 2026-03-18

See git history for the v1.0.0 release; this was the last commit
before the supervisor-v2.0 work moved into an untracked snapshot.
