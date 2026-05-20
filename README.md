# claude-runner · v2.0

`claude-runner` is a CLI tool that orchestrates
[Claude Code](https://docs.anthropic.com/claude/docs/claude-code) as a fully
autonomous subprocess.  You describe a project in a YAML **project book**,
point the runner at it, and it drives Claude Code end-to-end: spinning up an
isolated sandbox, feeding Claude its instructions, monitoring for rate-limit
pauses, managing context length, routing notifications, and optionally
switching models as the task progresses through phases.

**Supported platforms:** Windows 10 / 11 natively, plus Linux / WSL2 via the
editable Python install (see [Install on Linux / WSL](#install-on-linux--wsl)).
The pre-built `claude-runner.exe` is Windows-only; on Linux the runner is
invoked via the `claude-runner` console script installed into a venv or
conda env.

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
6. [NSuicide Principle (NSP)](#nsuicide-principle-nsp)
7. [CCCS — C# Standards Preset](#cccs--c-standards-preset)
8. [Agent-free fallback launch *(optional pattern)*](#agent-free-fallback-launch-optional-pattern)
9. [Phase-Aware Model Switching](#phase-aware-model-switching)
10. [Project Book Reference](#project-book-reference)
11. [CLI Reference](#cli-reference)
12. [ntfy Messaging](#ntfy-messaging)
13. [Sandbox Modes](#sandbox-modes)
14. [Notifications](#notifications)
15. [Acceptance Criteria](#acceptance-criteria)
16. [Configuration](#configuration)
17. [Development](#development)
18. [License](#license)

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

## Install on Linux / WSL

Tested on WSL2 + Ubuntu 26.04 with Python 3.11 via miniconda. Should work on
any glibc-based distribution.

```bash
# 1. Ensure Node.js + Claude Code are installed inside the Linux env:
sudo apt install -y nodejs npm     # or use nvm / your distro's installer
npm install -g @anthropic-ai/claude-code
claude                              # one-time OAuth login (browser handoff
                                    # or paste-the-URL-into-Windows-browser)

# 2. Install claude-runner editable in a venv or conda env:
git clone https://github.com/LunaticSodium/ClaudeRunner.git
cd ClaudeRunner
/path/to/env/bin/pip install -e ".[dev]"

# 3. Verify:
/path/to/env/bin/claude-runner --version
```

The `pyproject.toml` uses platform markers so the Windows-only `pywinpty`
dependency is skipped on Linux (the runtime uses `ptyprocess` + plain
`subprocess.Popen` instead). The native sandbox path does not require a PTY
layer on Linux because Claude Code's `-p` stream-json mode is pipe-friendly.

A shell-script wrapper analogous to `run.ps1` is straightforward to write
on Linux; the three-stage pattern (env bootstrap → preflight → `claude-runner
run <project>.yaml`) ports directly. If your acceptance commands use names
like `pytest` / `python` / `ruff`, ensure the wrapper exports `PATH` so the
env's `bin/` is first — otherwise the runner's subprocess shell-out will
fail with `/bin/sh: 1: <cmd>: not found` (exit 127).

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

## NSuicide Principle (NSP)

claude-runner's failure-handling design follows the **NSuicide Principle**
(NSP): *no part of the runner voluntarily kills itself under any condition
that could equivalently be handled by notifying the human and continuing.*
Self-termination is reserved for genuine impossibilities (uncaught
exceptions in core orchestration, missing required dependencies, manual
SIGTERM/SIGINT). Everything else stays alive.

### Motivation

The cost asymmetry is large. An orchestrator self-killing in the middle
of a multi-hour scientific simulation costs the human a relaunch + state
reset + context rebuild + supervision time. In bad cases it also severs
the only process tracking a detached long-running worker, forcing manual
re-attachment via PID files. **Notifying-and-staying-alive costs roughly
nothing comparable.** A runner that hangs around uselessly for an extra
ten minutes is strictly better than one that died ten minutes before
the worker finished a 3-hour compute.

### What NSP enforces

1. **`acceptance_criteria` failures default to `notify`, not `retry`.**
   The runner evaluates all checks, reports which gates passed/failed,
   dispatches the configured notification, and remains running. Retries
   are opt-in (`on_failure: retry, max_retries: N`) and only meaningful
   when the human expects the agent to attempt a fix in-session — never
   the right default for partial-deliverable workflows, where the
   missing-files check is *intentional* and retrying just burns tokens.
2. **Detached workers always outlive the orchestrator.** When a project's
   CLAUDE.md mandates the detached-compute pattern (cf. the FDTD
   grating-coupler example's §5.7), long-running compute is launched via
   `subprocess.Popen(start_new_session=True)` with a PID file. The
   orchestrator dying — rate-limit timeout, OS suspend, manual abort —
   does NOT kill these workers. The next orchestrator launch re-attaches
   to the running PID and resumes monitoring.
3. **Rate-limit waits never abandon mid-sleep.** `RateLimitWaiter` holds
   until the API reset epoch + buffer regardless of orchestrator-side
   interruption signals. The waiter is interruptible by explicit
   cancel(), not by transient host events.
4. **The TUI keeps redrawing on background-task failures.** A failed
   monitor task, dead notification channel, or unreachable ntfy server
   does not abort the supervisor or the worker — those errors are logged
   and surfaced via the audit log, while the main session continues.
5. **Hard errors that DO require exit still write a final-state
   checkpoint before exiting**, so the next orchestrator launch picks up
   exactly where the previous one died. NSP is not "never exit"; it's
   "exit only when there's no alternative, and exit in a way that the
   next session can recover."

### Project-book pattern under NSP

For the common case (partial-deliverable workflows, multi-day overnight
sweeps, anything where the agent might *legitimately* leave acceptance
gates unmet), set:

```yaml
acceptance_criteria:
  on_failure: notify              # NSP default
  max_retries: 0
  checks:
    - type: command
      run: pytest tests/ -q
    - type: file_exists
      path: output/final_result.json
    # ...
```

The runner now reports the gate status as part of the completion
notification rather than reading "5/10 fail" as a fatal condition.

### How to break NSP (when you actually want to)

If the human wants the runner to exit-fail on acceptance failure (e.g.
for CI usage, where a non-zero exit is the only signal a build system
respects), set `on_failure: fail` explicitly. NSP is a default, not a
hard constraint — it's the answer to "what should the runner do when
it has the option to live or die?".

### Where NSP is currently NOT enforced (known gaps)

- **`max_rate_limit_waits`** in `execution:` still exits the orchestrator
  when exhausted, instead of notifying + waiting for human resume. This
  is a known historical default that pre-dates NSP. Future revision: if
  the worker is using OAuth (no API key budget concern), the count can
  default to effectively-unlimited; if API-key-based, the human can
  raise the ceiling. A `--no-self-kill` flag is planned to honour NSP
  across all such legacy ceilings without per-project YAML overrides.
- **Acceptance-criteria evaluation** currently happens unconditionally
  after `##RUNNER:COMPLETE##` even when the agent has explicitly emitted
  a partial-deliverable signal (`output/phase{N}_skipped.md`). A future
  revision will treat the presence of `output/phase{N}_acceptance_partial.md`
  as a notify-only branch rather than a hard gate.

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

## Agent-free fallback launch *(optional pattern)*

A pattern the agent **may** apply when a project's work needs to keep running
after the agent or the operator's Claude subscription becomes unavailable:
ship the project with a fully scripted launch path that does not depend on
claude-runner or any LLM.

### When the agent should consider this pattern

- The project is a long-running compute pipeline (multi-hour to multi-day
  sweeps, training loops, simulations).
- A non-coder researcher will operate the project after hand-off.
- The operator's Claude subscription has a known expiry window inside the
  project's runtime budget.
- The agent's own rate-limit recovery has been unreliable on this host (in
  which case shifting from agent-driven to scripted execution removes a
  fragile dependency).

### What "agent-free launch" means in practice

A single entry-point script the operator can invoke directly, paired with a
user-editable config file. Concretely:

```
project_root/
├── drive_<phase>.sh          # idempotent driver: env check, run, commit
├── drive_config.sh           # user-editable parameters (sourced by driver)
├── finalize.sh               # produces a partial deliverable at any point
└── ... (the project's own code)
```

Properties the driver should hold:

- **Idempotent.** Re-running picks up where it left off; completed work is
  detected and skipped. No re-doing committed phases.
- **Detachable.** Designed to be wrapped in `setsid nohup … &` so the work
  survives the operator walking away from the terminal.
- **No agent dependency.** Pure bash / python / make. Does not need
  `claude`, `claude-runner`, or network access to Anthropic to function.
- **Partial-deliverable safety net.** A separate `finalize.sh` can produce
  a closeout artefact (best result, plots, README) from whatever state
  exists, so an interrupted run still ships something.
- **User-editable parameters.** All knobs (rank counts, target metrics,
  sweep slices, BO toggles) live in a config file the researcher can
  edit, not buried in the driver.

### Mandatory under the `scisim` CCCS profile

For projects using `cccs.profile: scisim` the agent-free launch path is
**mandatory** rather than optional. Science simulations are exactly the
class of work where full operator autonomy and full customisability matter
most: the researcher must be able to run, restart, and re-parameterise the
simulation without LLM mediation. See `claude_runner/presets/cccs-v1.0.cccs.toml`
section `[tail.scisim.deliverability_gate]` for the checked criteria.

For other profiles (`engineering`, custom) the pattern is recommended but
not gated. The agent decides per project whether the operational risk
profile justifies the extra scripting.

### Why this co-exists with claude-runner rather than replacing it

The agent-led path (`run.sh` + claude-runner) and the agent-free path
(`drive_*.sh`) are two views of the same project, sharing the same git
state and output directory. Either path can resume the other's work
without re-running completed phases. The agent path is the productive
default while an agent is available; the agent-free path is the route
home if it is not.

This composes naturally with the [NSuicide Principle](#nsuicide-principle-nsp):
NSP keeps the agent from voluntarily exiting; the agent-free path is what
the operator runs after the agent has been forced to exit anyway.

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

### Adding literature / paper anchors as conditions from the human

When the project is a research or design task where the agent's
intermediate numbers cannot be independently verified by you (no
alternative tool, no domain expertise, no oracle), inject
**authoritative paper citations and expected result ranges** directly
into the project book — usually in `context_anchors` or near the
relevant phase in `prompt`. The agent uses these as ground-truth
comparison: a result that disagrees with the literature range by more
than the stated tolerance is treated as a **likely pipeline bug**, not
a real physical or empirical result.

Why this matters: an autonomous agent under budget pressure tends to
optimise for "the acceptance check passes" rather than "the result is
correct". Without an anchor, a metric bug (sign convention,
normalisation, wrong units) can return a plausible-looking number and
slip through. With an anchor, the same bug shows up as an unambiguous
out-of-range result the agent (or supervisor) must surface.

```yaml
context_anchors: |
  === Literature benchmarks (anchor data from the human) ===
  Expected coupling efficiency at the literature baseline (uniform
  single-etch SOI grating coupler, 220 nm Si, 70 nm etch, 1550 nm TE):

  Plausible range:     -5 to -10 dB
  Theoretical ceiling: ~-1 dB (Gaussian-fibre 80% overlap limit)
  Implausible:         < -15 dB  -->  metric pipeline bug

  Papers:
    * Taillaert et al., IEEE JQE 38(7), 949-955 (2002).
      DOI: 10.1109/JQE.2002.1017613.  Canonical uniform-grating
      result: ~-7 dB at 1550 nm.
    * Yang et al., Sci. Rep. 13:18101 (2023).
      DOI: 10.1038/s41598-023-45168-2.  220 nm / 70 nm-etch AGC:
      simulated -3 dB, measured -5.86 dB.
    * Vermeulen et al., Opt. Express 18(17), 18278 (2010).
      DOI: 10.1364/OE.18.018278.  Upper-bound reference (multi-layer
      overlay, -1.6 dB).

  When a simulated value disagrees with this range by >5 dB, the FIRST
  hypothesis is a post-processing bug (sign of k_x, DFT convention,
  alignment search range, missing polarisation component) — NOT a
  poor physical design.
```

Pair this with a CLAUDE.md rule like:

```
A result that disagrees with the context_anchors literature range by
more than 5 dB MUST be surfaced as `output/<deviation_name>.md` and
treated as a probable bug. Never silently report it as the deliverable.
```

The pattern generalises beyond optics — any domain with established
benchmarks (e.g. reaction rate constants from NIST, free-energy values
from CCCBDB, RANS turbulence-model validation cases) is a candidate.
**Cite specific DOIs/URLs; don't trust the agent to remember them.**
The agent reads the project book and the spec; it does not browse the
web mid-run unless you give it `WebSearch` tool access AND a research
mandate.

This also gives the supervisor protocol (`--supervisor`) a concrete
disagreement criterion to act on: a worker reporting a value outside
the human-supplied range is automatic justification for an L1
re-describe or a `[BLOCK]` event, even if all the acceptance_criteria
file_exists checks pass.

### Default resource-utilisation policy (CPU / RAM / GPU)

Without an explicit override in the project book, the worker agent
SHOULD target the following utilisation on the host machine:

| Resource | Target | Peak ceiling | Rationale |
|---|---|---|---|
| CPU | ~75% of cores | — | Leave ~4 cores or ~20% (whichever is greater) for the OS, the orchestrator process, and the user's interactive session. |
| RAM | 65% steady-state | 75% peak | Above ~85% of system RAM the OS thrashes (paging / swap); the wall-clock penalty cancels the gain from using the extra RAM. |
| GPU compute | full when used | — | GPU compute is effectively binary — one kernel uses the whole device until done; capping by percentage has no meaningful semantics. |
| GPU VRAM | ≤ 85% per device | — | CUDA OOM is brutal: no graceful degradation. Stay below the ceiling, fail loudly rather than swap. |

These are sweet-spot defaults for **unattended overnight runs on a
typical shared workstation**. They keep the box responsive enough that
the human can still log in and inspect mid-run without choking the
worker, and they leave headroom for incidental OS / antivirus / backup
spikes that would otherwise stall the simulation.

The numbers translate to concrete env vars / arg defaults in the
worker entry point — for example a 20-core box at 75% CPU becomes:

```python
# At the top of every worker entrypoint, BEFORE numpy/meep imports:
N_THREADS = max(1, total_cores - max(4, total_cores // 5))   # ≈ 75% of cores
for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS",
            "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS",
            "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(var, str(N_THREADS))
```

For a parallel sweep (e.g. PHASE-5 in the FDTD example), the same
budget is split: `N_workers × threads_per_worker ≈ 75% of cores`.

#### Overriding the default in a project book

Two patterns are supported.

**(1) Whole-machine override** — when the host is a dedicated
workstation and the user explicitly grants full utilisation:

```yaml
context_anchors: |
  Resource policy (overrides the runner default 75%/65%/85%):
  This host is a dedicated workstation; the human is not actively
  using it during long runs. Use the FULL compute budget — saturate
  all 20 CPU cores at 100%, allow RAM peaks up to ~90%, no headroom
  reservation needed. GPU policy unchanged (compute full / VRAM ≤ 85%).
```

**(2) Per-phase fine-tuning** — when different phases want different
budgets (e.g. exploratory probes vs. final sweeps):

```yaml
context_anchors: |
  Resource policy:
    PHASE 0..3: low compute (small probes, code work); 4 threads max.
    PHASE 4   : single 3D solve, OMP_NUM_THREADS = total_cores - 4.
    PHASE 5   : 4 worker processes × OMP_NUM_THREADS=4 = 16 active.
    PHASE 6   : single BO probe, OMP_NUM_THREADS = total_cores - 4.
```

The runner has **no built-in resource enforcement** — these are soft
conventions the agent honours via env-var pinning at the worker
entrypoint. If hard enforcement is required (shared HPC, multi-tenant
hosts), use the Docker sandbox with `--cpus` / `--memory` flags or
host-level `cgroups` / `taskset` outside the runner.

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
# Probe interval during a rate-limit wait, in seconds. When > 0, runs a
# tiny `claude -p` probe every N seconds; if the API answers, the waiter
# exits early instead of sleeping the full retry-after window. Set to 0
# to disable (legacy behaviour). Min 30 s when enabled. Default 300 s.
rate_limit_probe_interval_s: 300

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
