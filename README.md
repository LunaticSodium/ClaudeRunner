# claude-runner · v2.0

`claude-runner` is a Windows CLI tool that orchestrates
[Claude Code](https://docs.anthropic.com/claude/docs/claude-code) as a fully
autonomous subprocess.  You describe a project in a YAML **project book**,
point the runner at it, and it drives Claude Code end-to-end: spinning up an
isolated sandbox, feeding Claude its instructions, monitoring for rate-limit
pauses, managing context length, routing notifications, and optionally
switching models as the task progresses through phases.

**v2.0** adds a two-tier adversarial supervision system: a Marathon Runner
(supervisor) manages N Dash Runner (worker) subprocesses with budget-gated
interventions, dual-channel enforcement, and two-track analytical reasoning.
It also adds bidirectional ntfy messaging, a pending.md inbox lifecycle, and
CLI launch flags for protocol selection.

The result is an unattended, long-running automation loop suitable for
overnight coding sessions, multi-hour pipelines, and any task too large to
supervise manually.

---

## Table of Contents

1. [Download](#download)
2. [Install](#install)
3. [Prerequisites](#prerequisites)
4. [Quick Start](#quick-start)
5. [Supervisor Protocol](#supervisor-protocol)
6. [CCCS — C# Standards Preset](#cccs--c-standards-preset)
7. [Phase-Aware Model Switching](#phase-aware-model-switching)
8. [Project Book Reference](#project-book-reference)
9. [CLI Reference](#cli-reference)
10. [ntfy Messaging](#ntfy-messaging)
11. [Sandbox Modes](#sandbox-modes)
12. [Notifications](#notifications)
13. [Acceptance Criteria](#acceptance-criteria)
14. [Configuration](#configuration)
15. [Development](#development)
16. [License](#license)

---

## Download

Pre-built Windows executable — no Python required:

**[→ Releases on GitHub](https://github.com/LunaticSodium/ClaudeRunner/releases)**

Download `claude-runner.exe` from the latest release, place it anywhere on your `PATH`, and skip to [Prerequisites](#prerequisites).

To build from source instead, see [Install](#install).

---

## Install

```cmd
git clone https://github.com/LunaticSodium/ClaudeRunner.git
cd ClaudeRunner/claude-runner
pip install -e ".[dev]"
```

---

## Prerequisites

### Node.js + Claude Code

```cmd
winget install OpenJS.NodeJS.LTS
```

Reopen your terminal, then:

```cmd
npm install -g @anthropic-ai/claude-code
claude
```

The final command opens a one-time login flow.  If you have a **Claude.ai Pro
or Max** subscription, log in with your account — no API key required.
Otherwise an Anthropic API key is prompted during login.

### Docker Desktop *(docker sandbox only)*

Download from https://www.docker.com/products/docker-desktop/

Enable auto-start: system tray → **Settings → General → "Start Docker Desktop
when you log in"**.  Docker must be running before launching claude-runner.

---

## Quick Start

### 1. First run — auto-configure

On first launch the setup wizard runs automatically.  Run any command:

```cmd
claude-runner configure
```

The wizard covers four steps:
1. **Claude Code authentication** — OAuth session is detected automatically; API key as fallback
2. **Notifications** — ntfy.sh (recommended, free, no account), or email
3. **Default feature settings** — CCCS protocol on/off, marathon runway on/off
4. **Credential storage** — `secrets.yaml` or Windows Credential Manager

Re-run at any time to update settings:

```cmd
claude-runner configure
```

### 2. Write a project book

```yaml
# my-task.yaml
name: my-task
description: Refactor auth module to use JWTs.

prompt: |
  Refactor auth/session.py and auth/middleware.py to issue and verify
  JWT tokens using PyJWT.  Remove the legacy cookie-based session store.
  Add unit tests.  Output ##RUNNER:COMPLETE## when done.

sandbox:
  working_dir: C:/Projects/my-app
  backend: docker
```

### 3. Run

```cmd
claude-runner run my-task.yaml
```

claude-runner will:
1. Start a Docker container for the working directory
2. Launch Claude Code with the prompt
3. Stream output to your terminal
4. Handle rate-limit pauses, context overflows, and retries automatically
5. Send a desktop notification on completion or error

---

## Supervisor Protocol

The supervisor protocol enables two-tier adversarial supervision.  A Marathon
Runner (supervisor) manages N Dash Runner (worker) subprocesses, monitoring
their KPIs, diagnosing underperformance, and intervening when needed.

Enable it with a CLI flag — the project book describes the task, not the
execution mode:

```cmd
claude-runner run my-task.yaml --supervisor
claude-runner run my-task.yaml --supervisor --supervisor-model claude-sonnet-4-6
```

### What happens at launch

1. **Intake validation** — supervisor LLM checks the project book for
   completeness (design space, objectives, constraints, output spec, domain
   anchors).  "Fail" blocks launch.
2. **Analytical pre-flight** — two-track reasoning (Track 1 creative +
   Track 2 controlled) surfaces unknown unknowns before any worker starts.
   Findings written to `audit/preflight_findings.md`.

### Budget system

The supervisor starts with 10 points.  Failed interventions deduct points;
correct pre-flight predictions earn points back.  At 0 points, interventions
are blocked (hard gate).

| Event | Points |
|---|---|
| Worker crash (L3 restart) | -3 |
| Worker crash (L1 re-describe) | -1 |
| Misdiagnosis | -2 |
| False flag | -4 |
| Correct pre-flight | +1 |

Budget status is injected into the worker's `pending.md` at each checkpoint
(soft channel) and enforced in Python before any intervention (hard channel).

### Intervention levels

1. **L1 Re-describe** — rewrite worker's project YAML with clearer target
2. **L2 Split** — decompose stalled task into two smaller project YAMLs
3. **L3 Restart** — kill and relaunch with adjusted parameters

All interventions pass a 5-gate check: budget > 0, process not actively
computing, 30-min cooldown, max 3 per worker, and cause is not
rate-limit/environment.

### Audit files

| File | Purpose |
|---|---|
| `audit/supervisor_log.md` | All events timestamped |
| `audit/supervisor_budget.md` | Budget state (LLM cannot modify) |
| `audit/preflight_findings.md` | Pre-flight analysis results |
| `audit/self_check_log.md` | Post-Dash self-check results |
| `audit/accident_snapshots/` | Frozen supervisor thinking at each failure |

---

## CCCS — C# Standards Preset

Each project book has two independent toggles:
- **`cccs`** — injects citation-backed C# coding standards into `CLAUDE.md` before the session starts.  Omit for no standards injection.
- **`marathon_mode`** — keeps a single model for the whole session and survives server restarts/outages.  Omit (or `false`) for phase-aware model switching.

**CCCS** (Claude Code C# Standards for Scientific Simulation) is the built-in
standards preset.  Before the first `claude -p` call it:

1. **Validates the project YAML** against a structural schema.
2. **Injects a CLAUDE.md fragment** — citation-backed rules that Claude sees
   as authoritative constraints throughout the session.

### Enabling

```yaml
cccs:
  preset: cccs-v1.0       # built-in preset (default)
  profile: scisim         # scisim | engineering — omit for preset default
```

Temporarily revert to universal without removing the block:

```yaml
cccs:
  enabled: false
```

### Profiles

| Profile | Use case |
|---|---|
| `scisim` | Scientific simulation — full rigour: MCSE reporting, analytical validation, convergence checks, structured output columns |
| `engineering` | Build and test gates only; no numerical simulation requirements |

### Rule sections injected into CLAUDE.md

Each rule is prefixed `MUST` / `SHOULD` / `MAY` (RFC 2119).  The fragment
stays under 150 lines to maintain >92% adherence.

| Section | Summary |
|---|---|
| `<architecture>` | Hexagonal core/adapter split; no static state; injectable dependencies; `IRng` interface; named parameters with units |
| `<numerical_standards>` | `float64` default; MCSE alongside every estimate; convergence N vs 2N; matched RNG seeds |
| `<validation>` | At least one analytical validation; MMS for PDEs/ODEs; KS/chi² for stochastic distributions |
| `<reproducibility>` | Config file for all params; runtime metadata logged (version, seed, git hash); structured results directory |
| `<testing>` | xUnit/NUnit; regression baseline on every commit; every bug becomes a test; `dotnet test` after every phase |
| `<coding_standards>` | XML docs; `Nullable enable`; no `var` for non-obvious types; `async Task` not `async void` |
| `<delivery>` | Phased commits with `PHASE-N:` prefix; never commit with failing tests; final V&V gate before delivery |
| `<compact_instructions>` | What to preserve during context compaction so phase progress survives `/compact` |

### Acceptance gates (`scisim` profile)

| Gate | Value |
|---|---|
| `dotnet build` exit code | 0 |
| Warnings as errors | yes |
| `dotnet test` exit code | 0 |
| Line coverage — core namespace | ≥ 80 % |
| Line coverage — numerics namespace | 100 % |
| Min ensemble — central tendency | 500 repetitions |
| Min ensemble — variance | 1 000 repetitions |
| Min ensemble — tail statistics | 10 000 repetitions |
| MCSE reported | yes |
| Convergence check (N vs 2N) | required |
| Analytical validation | ≥ 1 |
| Runtime metadata logged | yes |
| Seed logged | yes |

The preset file lives at `claude_runner/presets/cccs-v1.0.cccs.toml` and can
be forked for custom domain standards.

---

## Phase-Aware Model Switching

Available on the **dash** runway.  A `ModelWatchdog` background thread polls
`git log` every `poll_interval_seconds` and switches Claude Code to a
different model when phase or context triggers fire.

### How it works

1. Runner injects a phase-contract block into `CLAUDE.md`: tells Claude to
   prefix milestone commits with `PHASE-{N}: `.
2. Watchdog polls `git log --format=%s -50`, parsing the highest `PHASE-N:`
   commit number.
3. When a rule's triggers match, the watchdog fires: checkpoint context →
   stop Claude Code process → re-launch with new `model_id` set via
   `ANTHROPIC_MODEL` + `CLAUDE_CODE_SUBAGENT_MODEL`.
4. Each rule fires at most once per session.  Phase number never goes
   backwards (monotonic).

### Configuration

```yaml
model_schedule:
  poll_interval_seconds: 15
  rules:
    # Haiku for early scaffolding (phases 1–2)
    - triggers:
        - phase_gte: 1
          phase_lte: 2
      action:
        model_id: claude-haiku-4-5-20251001
        message: "Haiku for early scaffolding"

    # Sonnet from phase 3 onwards (complex logic)
    - triggers:
        - phase_gte: 3
      action:
        model_id: claude-sonnet-4-6
        message: "Sonnet for complex logic"

    # Also switch if context is nearly full regardless of phase
    - triggers:
        - token_pct_gte: 0.85
      action:
        model_id: claude-sonnet-4-6
        message: "Context nearly full"
```

### Trigger conditions

Multiple triggers within one rule use **OR** logic.
Multiple conditions within one trigger use **AND** logic.

| Field | Type | Meaning |
|---|---|---|
| `phase_gte` | int | Current phase ≥ value |
| `phase_lte` | int | Current phase ≤ value |
| `token_pct_gte` | float 0–1 | Context utilisation ≥ fraction |
| `token_pct_lte` | float 0–1 | Context utilisation ≤ fraction |

### Model switch notification

```yaml
notify:
  on: [start, complete, error, model_switch]
```

---

## Project Book Reference

```yaml
# ── Required ──────────────────────────────────────────────────────────────

name: my-task                    # short identifier, used on the CLI
prompt: |
  Full task description here.
  Print ##RUNNER:COMPLETE## when finished.

# ── Optional identity ─────────────────────────────────────────────────────

description: "One-line summary shown in status output."

# Injected into the resume prompt after rate-limit pauses / context trims.
# Use to keep Claude oriented on very long tasks.
context_anchors: |
  Key decisions: X over Y because Z.

# ── Feature machine ───────────────────────────────────────────────────────

# Runway: false (default) = dash, true = marathon
marathon_mode: false

# Protocol: omit = universal, set = cccs
cccs:
  preset: cccs-v1.0              # built-in preset name
  profile: scisim                # scisim | engineering
  enabled: true                  # false = revert to universal temporarily

# Phase-aware model schedule (dash runway only — ignored when marathon_mode: true)
model_schedule:
  poll_interval_seconds: 15
  rules:
    - triggers:
        - phase_gte: 2
      action:
        model_id: claude-sonnet-4-6
        message: "Switch to Sonnet from phase 2"

# ── Sandbox ───────────────────────────────────────────────────────────────

sandbox:
  backend: docker                # auto | docker | native
  working_dir: C:/Projects/my-app

  # Host paths exposed inside the container as read-only bind mounts
  readonly_mounts:
    - host_path: C:/Shared/libs
      mount_as: /mnt/libs

  # Network control (docker only)
  network:
    disabled: false              # true = no outbound network
    allow: []                    # allowlist when disabled: false

  # Environment variables injected into the sandbox
  env:
    NODE_ENV: test
    LOG_LEVEL: debug

  # Allow Claude to modify the runner's own source (use with care)
  allow_self_modification: false

# ── Execution ─────────────────────────────────────────────────────────────

execution:
  timeout_hours: 4               # wall-clock limit (0 = no limit)
  max_rate_limit_waits: 20       # consecutive rate limits before failing
  skip_permissions: false        # pass --dangerously-skip-permissions

  # Resume strategy after interruption: continue | restate | summarize
  resume_strategy: restate

  # Abort if Claude produces no output for this many minutes
  silence_timeout_minutes: 10

  # Named milestones — logged and notified when detected in Claude's output
  milestones:
    - pattern: "Phase 1 complete"
      message: "Phase 1 done"

  # Context window management
  context:
    checkpoint_threshold_tokens: 80000

# ── Output ────────────────────────────────────────────────────────────────

output:
  git:
    enabled: true
    auto_push: false             # push to remote on completion
    remote_url: https://github.com/my-org/my-app.git
    branch: main

# ── Notifications ─────────────────────────────────────────────────────────

notify:
  on: [start, complete, error, rate_limit, model_switch]
  channels:
    - type: desktop
    - type: webhook
      url: https://ntfy.sh/my-topic
    - type: email
      to: you@gmail.com

# ── Acceptance criteria ───────────────────────────────────────────────────

acceptance_criteria:
  on_failure: retry              # retry | fail | notify
  max_retries: 2
  checks:
    - type: file_exists
      path: output/results.csv
    - type: file_contains
      path: output/results.csv
      pattern: "convergence"
    - type: command
      run: dotnet test
```

---

## CLI Reference

| Command | Description |
|---|---|
| `run <project>` | Start a project run.  `<project>` is a YAML path or a name matched against `projects/*.yaml`. |
| `run <project> --dry-run` | Validate the project book and print the prompt without starting Claude. |
| `run <project> --marathon` | Run with marathon runway (single model, survives restarts). |
| `run <project> --cccs` | Run with CCCS protocol enabled. |
| `run <project> --supervisor` | Run with supervisor protocol (intake, pre-flight, budget). |
| `run <project> --supervisor-model <id>` | Override model for supervisor LLM calls. |
| `validate <project>` | Validate a project book's YAML schema.  Exits 0 on success. |
| `status` | Show all active and recently completed runs. |
| `status <project>` | Detailed status for a single run. |
| `abort <project>` | Gracefully stop a running project (SIGTERM). |
| `abort <project> --force` | Kill immediately (SIGKILL). |
| `logs <project>` | Stream the live log or print the saved log. |
| `logs <project> --tail 50` | Last 50 lines only. |
| `configure` | Interactive setup wizard (auth, notifications, ntfy channels, features). |
| `configure --show` | Print current configuration (API key masked). |
| `ntfy send "message"` | Send a message to the human via ntfy out channel. |
| `ntfy poll` | Poll for new inbound messages on the cmd channel. |
| `ntfy listen` | Long-poll the cmd channel (blocks until Ctrl-C or sentinel). |
| `ntfy set-channels --out <name> --cmd <name>` | Configure ntfy channel names directly. |
| `ntfy show-channels` | Display configured ntfy channel names. |
| `docker pull` | Pull the latest claude-runner Docker base image. |
| `docker build` | Rebuild the local Docker image from `docker/Dockerfile`. |
| `docker prune` | Remove stopped containers and dangling images. |
| `marathon start` | Start the persistent marathon daemon. |
| `marathon stop` | Stop the marathon daemon. |

---

## ntfy Messaging

v2.0 adds bidirectional messaging between the runner and a human operator via
[ntfy.sh](https://ntfy.sh).  Two channels are used:

| Channel | Direction | Purpose |
|---|---|---|
| Out channel | runner → human | Notifications, alerts, LLM responses |
| Cmd channel | human → runner | Commands, overrides, questions |

### Setup

```cmd
claude-runner ntfy set-channels --out my-topic --cmd my-topic-cmd
```

Or configure during `claude-runner configure`.  Channel names are stored in
Windows Credential Manager.

### How messages flow

**Inbound** (human → LLM):
1. Human sends a message to the cmd channel (via ntfy app or CLI)
2. Auto-poll script writes it to `~/.claude-runner/inbox/pending.md`
3. Runner injects "read pending.md" at next natural pause
4. LLM reads and responds

**Outbound** (LLM → human):
1. After drain, `processing_pending_message` flag activates response capture
2. LLM output is buffered until an end marker or 50 lines
3. Captured response is auto-forwarded to the ntfy out channel

### Standalone usage

The ntfy client works outside the runner — any Claude Code instance or script
can use it:

```bash
# Send
claude-runner ntfy send "Build complete, 0 failures"
python -m claude_runner.ntfy_client send out "message"

# Receive
claude-runner ntfy poll
claude-runner ntfy listen    # long-poll, stops on Ctrl-C or ntfy.stop file

# Direct file access (outsider Claude Code)
cat ~/.claude-runner/inbox/pending.md
```

### pending.md lifecycle

- Hard size limit: 32 KB — oldest entries trimmed automatically
- Two flags: `has_pending_messages` (unread) + `processing_pending_message` (capturing response)
- Truncated after LLM consumes content

---

## Sandbox Modes

### Docker *(recommended)*

Each run gets a fresh container.  The host filesystem is not mounted.  Network
access is configurable per project.

**When to use:** untrusted codebases, tasks that install system packages,
production pipelines, any situation requiring a clean reproducible environment.

**How it works:**
1. Builds or pulls `claude-runner-base:latest`.
2. Starts a container with env vars injected and `working_dir` bind-mounted.
3. Launches Claude Code inside the container.
4. On completion, logs are extracted and the container is stopped.

### Native

Runs Claude Code directly on the host.  Working directory is on the host
filesystem.

**When to use:** rapid iteration on trusted projects, environments without
Docker, tasks that need host GPU or licensed software.

**Caveat:** Claude Code has full access to your host filesystem and network.

---

## Notifications

Three channels: **desktop** (Windows toast), **ntfy.sh** (free push,
recommended), **email** (SMTP).

```yaml
notify:
  on: [start, complete, error, rate_limit, model_switch]
  channels:
    - type: desktop
    - type: webhook
      url: https://ntfy.sh/your-topic
    - type: email
      to: you@gmail.com
```

### ntfy.sh setup (30 seconds)

1. Install the [ntfy app](https://ntfy.sh) (iOS / Android) or open ntfy.sh in
   your browser.
2. Subscribe to a private topic name (treat it like a password — anyone who
   knows it can send you messages).
3. Run `claude-runner configure` → select ntfy → enter topic → test
   notification fires immediately.

### Event reference

| Event | When it fires |
|---|---|
| `start` | Session launched |
| `complete` | `##RUNNER:COMPLETE##` detected |
| `error` | Unrecoverable failure |
| `rate_limit` | API rate limit hit |
| `model_switch` | ModelWatchdog fired (dash runway) |
| `supervisor_accident` | Supervisor budget points deducted |
| `intake_pass` / `intake_fail` | After intake validation |
| `preflight_finding` | Thinking Manual finding surfaced |
| `kpi_warning` | Worker underperformance detected |
| `intervention` | Supervisor intervention executed |
| `escalate_to_human` | Intervention limit reached — human needed |

---

## Acceptance Criteria

After `##RUNNER:COMPLETE##` is detected, claude-runner can run a set of
checks to verify the output.  On failure it retries, notifies, or fails the
run per `on_failure`.

```yaml
acceptance_criteria:
  on_failure: retry              # retry | fail | notify
  max_retries: 2
  checks:
    - type: file_exists
      path: results.csv

    - type: file_contains
      path: results.csv
      pattern: "MCSE"

    - type: command
      run: dotnet test PrisonersDilemma.csproj
      expected_exit: 0
```

| Check type | Required fields | Passes when |
|---|---|---|
| `file_exists` | `path` | File exists in working dir |
| `file_contains` | `path`, `pattern` | File content matches pattern (regex) |
| `command` | `run` | Shell command exits `expected_exit` (default 0) |
| `llm_judge` | `prompt` | LLM judge call returns a passing verdict |

---

## Configuration

`~/.claude-runner/config.yaml` — written by `claude-runner configure`,
editable by hand.  Secrets (API key, SMTP password, ntfy URL) are stored
separately in `secrets.yaml` or Windows Credential Manager and never appear
here.

```yaml
# Authentication
# API key — can also be set via ANTHROPIC_API_KEY env var.
# Omit if using Claude.ai OAuth (detected automatically from ~/.claude/).
api_key: ""

# Sandbox
sandbox_backend: docker          # docker | native

# Docker settings
docker_base_image: claude-runner-base:latest
docker_socket: "npipe:////./pipe/docker_engine"

# Session behaviour
resume_strategy: continue        # continue | restate | summarize
max_rate_limit_waits: 20

# UI
tui: true                        # false = plain log lines (CI-friendly)

# Feature defaults — project books always override these
cccs_enabled: false              # enable cccs protocol for all projects by default
marathon_mode_default: false     # enable marathon runway for all projects by default

# Marathon daemon
marathon:
  enabled: false
  poll_interval_minutes: 5

# Storage (defaults shown — empty string uses default location)
log_dir: ""                      # default: ~/.claude-runner/logs/
state_dir: ""                    # default: ~/.claude-runner/state/
```

---

## Development

### Install

```cmd
git clone https://github.com/LunaticSodium/ClaudeRunner.git
cd ClaudeRunner/claude-runner
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
```

### Tests

```cmd
pytest tests/                    # all 523 tests
pytest tests/ -k cccs            # CCCS parser (32 tests)
pytest tests/ -k watchdog        # ModelWatchdog (23 tests)
pytest tests/ -k configure       # configure wizard (15 tests)
pytest tests/ -k supervisor      # supervisor protocol + worker supervisor
pytest tests/ -k inbox           # pending.md inbox lifecycle
pytest tests/ -k ntfy            # ntfy client + CLI
```

### Lint / type-check

```cmd
ruff check claude_runner/
mypy claude_runner/
```

### Build exe

```cmd
python build_exe.py --clean
# output: dist/claude-runner.exe
```

### Project layout

```
claude-runner/
  claude_runner/
    __main__.py            python -m claude_runner entry point
    main.py                CLI entry point (Click)
    runner.py              Core orchestration loop
    config.py              Config loading (config.yaml + secrets)
    project.py             Project book schema (Pydantic)
    model_watchdog.py      Phase-aware model-switch background thread
    cccs_parser.py         CCCS preset loader and CLAUDE.md renderer
    context_manager.py     Token counting and context trimming
    acceptance_runner.py   Post-completion acceptance checks
    persistence.py         Task state and checkpoint management
    notify.py              Notification dispatch
    ntfy_client.py         ntfy.sh client + CLI (send/poll/listen)
    inbox.py               pending.md lifecycle (two-flag system)
    rate_limit.py          Rate-limit detection and backoff
    tui.py                 Rich terminal UI
    daemon.py              Marathon persistent daemon + worker dispatch
    autostart.py           Windows Task Scheduler registration
    pipeline.py            Inbound message pipeline
    git_inbox.py           Git-based message injection
    supervisor_protocol.py Budget, protocol enforcement, call_supervisor_llm()
    worker_supervisor.py   5-gate intervention engine (L1/L2/L3)
    kpi_collector.py       Worker metrics, progress rate, peer ranking
    thinking_manual.py     Two-track reasoning (creative + controlled)
    supervisor_audit.py    Structured audit file writing
    supervisor_lib.md      LLM spellbook — supervisor tool reference
    sandbox/
      docker_sandbox.py
      native_sandbox.py
    presets/
      cccs-v1.0.cccs.toml  Bundled C# scientific simulation standard
  docs/
    CCCS_SPEC.md           CCCS parser implementation specification
    IMPLEMENTATION_LOG_v2.0.md  Detailed changelog from v1.1 to v2.0
  projects/
    examples.yaml          Furina ASCII art (example task)
    self-test.yaml         Runner self-diagnostic
    bto_runner.yaml        BTO modulator simulation project
  tests/                   523 tests
  docker/
    Dockerfile
  watchdog.py              Standalone process watchdog (restart on crash)
  build_exe.py
  pyproject.toml
```

### Contributing

1. Fork the repository.
2. Create a branch: `git checkout -b feature/my-change`.
3. Make your changes and add tests.
4. Run `ruff check`, `mypy`, and `pytest` until all pass.
5. Open a pull request against `main`.

---

## License

MIT License.  See [LICENSE](LICENSE) for the full text.
