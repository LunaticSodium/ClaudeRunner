# claude-runner

`claude-runner` is a Windows CLI tool that orchestrates [Claude Code](https://docs.anthropic.com/claude/docs/claude-code) as a fully autonomous subprocess. You describe a project in a YAML "project book", point the runner at it, and it drives Claude Code end-to-end: spinning up an isolated sandbox, feeding Claude its instructions, monitoring for rate-limit pauses, resuming automatically, routing desktop/email/webhook notifications, and trimming context when the conversation approaches the model's token ceiling. The result is an unattended, long-running automation loop suitable for overnight coding sessions, CI-like pipelines, or any task too large to supervise manually.

---

## Prerequisites

Install the following before running claude-runner.

### Docker Desktop

Download and install from https://www.docker.com/products/docker-desktop/

After installation, enable auto-start so it is always available:
> Right-click the Docker icon in the system tray → **Settings → General** →
> check **"Start Docker Desktop when you log in"**

Docker must be running before launching claude-runner.

### Node.js + Claude Code

Install Node.js LTS:

```cmd
winget install OpenJS.NodeJS.LTS
```

Reopen your terminal, then install and authenticate Claude Code:

```cmd
npm install -g @anthropic-ai/claude-code
claude
```

The final command opens a one-time login flow. Complete it once and Claude Code
will remain authenticated. If you have a **Claude.ai Pro or Max** subscription
you can log in with your account here — no API key required. Otherwise you will
be prompted for an Anthropic API key during the login flow.

### Anthropic API Key *(API key users only)*

If you authenticated Claude Code with an API key rather than a Claude.ai
account, you will need an Anthropic API key from
https://console.anthropic.com/

`claude-runner configure` will guide you through storing it securely in
Windows Credential Manager during first-time setup.

To view or delete stored credentials later:
> Start Menu → search **Credential Manager** → **Windows Credentials**
> → look for **claude-runner** entries.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Project Book Format](#project-book-format)
3. [CLI Reference](#cli-reference)
4. [Sandbox Modes](#sandbox-modes)
5. [Rate Limit Handling](#rate-limit-handling)
6. [Notifications](#notifications)
7. [Context Length Management](#context-length-management)
8. [Configuration](#configuration)
9. [Requirements](#requirements)
10. [Development](#development)
11. [License](#license)

---

## Quick Start

### 1. Install

**From the pre-built executable (recommended for most users)**

Download `claude-runner.exe` from the releases page and place it somewhere on your `PATH` (e.g., `C:\Users\<you>\bin\`). No Python installation is required on the target machine.

**From source**

```cmd
git clone https://github.com/your-org/claude-runner.git
cd claude-runner
pip install -e ".[dev]"
```

### 2. Configure

Run the interactive setup wizard on first use:

```cmd
claude-runner configure
```

The wizard asks for:
- Your Anthropic API key (stored in `~/.claude-runner/config.yaml`, never echoed)
- Default sandbox mode (`docker` or `native`)
- Optional notification channels (desktop, email, webhook)

The resulting config file is plain YAML and can be edited by hand at any time (see [Configuration](#configuration)).

### 3. Write a project book

Create a YAML file describing your task. See [Project Book Format](#project-book-format) for the full schema.

```yaml
# projects/my-project.yaml
name: my-project
description: Refactor the authentication module to use JWTs.
repo: https://github.com/my-org/my-app.git
branch: main
sandbox: docker
goal: |
  Refactor auth/session.py and auth/middleware.py to issue and verify
  JWT tokens using PyJWT. Remove the legacy cookie-based session store.
  Add unit tests. Do not touch any file outside the auth/ directory.
```

### 4. Run

```cmd
claude-runner run my-project
```

claude-runner will:
1. Clone the repository into a fresh sandbox.
2. Start Claude Code with the project goal as its initial prompt.
3. Stream Claude's output to your terminal.
4. Handle rate-limit pauses, context overflows, and retries automatically.
5. Send a desktop notification (and optionally email/webhook) when done.

---

## Project Book Format

Project books live in the `projects/` directory (relative to the executable) or in any path you pass directly: `claude-runner run path/to/my-project.yaml`.

```yaml
# ------------------------------------------------------------------
# Required fields
# ------------------------------------------------------------------

# Unique identifier used on the command line.
name: my-project

# Human-readable description shown in `claude-runner status` output.
description: "Refactor the auth module to use JWTs."

# Git repository to clone into the sandbox.
# Accepts HTTPS or SSH URLs.
repo: https://github.com/my-org/my-app.git

# Branch (or tag/commit SHA) to check out.
branch: main

# The task prompt fed to Claude Code as the first message.
# Multi-line YAML block scalars are recommended for long goals.
goal: |
  Refactor auth/session.py and auth/middleware.py to issue and verify
  JWT tokens using PyJWT. Remove the legacy cookie-based session store.
  Add unit tests. Do not touch any file outside the auth/ directory.

# ------------------------------------------------------------------
# Optional fields
# ------------------------------------------------------------------

# Sandbox mode: "docker" (isolated container) or "native" (host Python).
# Overrides the global default in config.yaml.
# Default: value of sandbox.default in config.yaml
sandbox: docker

# Maximum wall-clock time for the entire run, in minutes.
# claude-runner will abort the run and notify you if this is exceeded.
# Default: 0 (no limit)
timeout_minutes: 120

# Strategy to use when the conversation approaches the context limit.
# Options:
#   continue   -- append "continue from where you left off" (default)
#   restate    -- resend the full goal + a summary of progress so far
#   summarize  -- ask Claude to summarize the conversation, then continue
# Default: continue
context_strategy: restate

# Rate-limit resume strategy (see "Rate Limit Handling" section).
# Options: continue | restate | summarize
# Default: continue
rate_limit_strategy: continue

# Environment variables to inject into the sandbox at runtime.
# Values are plain strings; do not store secrets here -- use config.yaml
# or a .env file referenced via env_file below.
env:
  NODE_ENV: test
  LOG_LEVEL: debug

# Path to a .env file whose contents are injected into the sandbox.
# Relative paths are resolved from the project book's directory.
env_file: .env.sandbox

# Notification overrides for this project specifically.
# These merge with (and take precedence over) config.yaml notifications.
notifications:
  on_complete:
    desktop: true
    email: false
    webhook: true
  on_error:
    desktop: true
    email: true
    webhook: true
  on_rate_limit:
    desktop: true
    email: false
    webhook: false
```

---

## CLI Reference

All commands accept `--help` for inline documentation.

| Command | Description |
|---|---|
| `run <project>` | Start a project run. `<project>` is a project name (matched against `projects/*.yaml`) or a path to a YAML file. |
| `run <project> --sandbox docker` | Override the sandbox mode for this run only. |
| `run <project> --dry-run` | Validate the project book and print the prompt that would be sent, without starting Claude Code. |
| `validate <project>` | Validate a project book's YAML schema without running. Exits 0 on success. |
| `status` | Show a table of all active and recently completed runs: project name, status, elapsed time, last event. |
| `status <project>` | Show detailed status for a single named run. |
| `abort <project>` | Gracefully stop a running project. Claude Code is sent SIGTERM; the sandbox is preserved for inspection. |
| `abort <project> --force` | Send SIGKILL and destroy the sandbox immediately. |
| `logs <project>` | Stream the live log for a running project, or print the saved log for a completed one. |
| `logs <project> --tail 50` | Print only the last 50 lines of the log. |
| `configure` | Launch the interactive configuration wizard. Writes `~/.claude-runner/config.yaml`. |
| `configure --show` | Print the current configuration (API key is masked). |
| `docker pull` | Pull the latest claude-runner Docker base image. |
| `docker build` | Rebuild the local Docker image from `docker/Dockerfile`. |
| `docker prune` | Remove all stopped claude-runner containers and dangling images. |

---

## Sandbox Modes

### Docker (hard sandbox) -- recommended

When `sandbox: docker` is set, claude-runner spins up a fresh Docker container for each run using the bundled `docker/Dockerfile`. Claude Code and the cloned repository live entirely inside the container. The host filesystem is not mounted. Network access can be further restricted via `config.yaml`.

**When to use Docker:**
- Untrusted or unfamiliar codebases.
- Tasks that install system packages or run arbitrary shell commands.
- Any situation where you want a clean, reproducible environment.
- Production or CI-like pipelines.

**Prerequisites:** Docker Desktop must be running. See [Requirements](#requirements).

**How it works:**

1. `claude-runner` builds or pulls the base image (`claude-runner-base:latest`).
2. It starts a container with the project's environment variables injected.
3. Inside the container, it clones the repo, installs dependencies, and launches Claude Code.
4. On completion (success or error), the container is stopped. Logs are extracted before destruction.
5. The container is removed automatically unless `sandbox.keep_on_error: true` is set in `config.yaml`.

### Native (soft sandbox)

When `sandbox: native` is set, claude-runner runs Claude Code directly on the host machine using the host Python and Node.js installations. The repository is cloned into a temporary directory under `%TEMP%\claude-runner\<project>\`.

**When to use native:**
- Rapid iteration on trusted personal projects.
- Environments where Docker Desktop is unavailable.
- Tasks that need direct access to host GPU, hardware, or licensed software.

**Caveats:** Claude Code has full access to your host filesystem and network. Use with care.

---

## Rate Limit Handling

Claude Code communicates rate-limit events to stdout using a structured JSON line format. claude-runner parses this stream in real time.

**What happens when a rate limit is hit:**

1. claude-runner detects the `rate_limit_exceeded` event.
2. It reads the `retry_after` field from the event (seconds until the quota resets).
3. It sends an `on_rate_limit` notification (see [Notifications](#notifications)).
4. It sleeps until the reset time, printing a countdown to the terminal.
5. On waking, it resumes using the strategy specified by `rate_limit_strategy` in the project book (or `config.yaml`):

| Strategy | Behaviour |
|---|---|
| `continue` | Append a single line: `"Continue from where you left off."` This keeps conversation history intact and works well for most tasks. |
| `restate` | Resend the full `goal` prompt followed by a short progress summary extracted from Claude's last assistant message. Use this when `continue` causes Claude to lose track of the objective. |
| `summarize` | Ask Claude to produce a structured summary of completed and remaining work, then start a fresh conversation seeded with that summary. This costs extra tokens but is reliable for very long tasks. |

**Exponential backoff:** If the API returns a rate-limit error before a `retry_after` field is available (e.g., a 429 with no body), claude-runner applies exponential backoff starting at 60 seconds, capped at 30 minutes, with jitter.

---

## Notifications

claude-runner supports three notification channels: desktop toast, ntfy.sh push, and email. Channels are configured per-project in the project book and can be set up interactively with `claude-runner configure`.

### ntfy.sh *(recommended)*

[ntfy.sh](https://ntfy.sh) is a free, open-source push notification service — no account or sign-up required. Install the ntfy app on your phone, subscribe to a topic, and receive real-time alerts wherever you are.

**Setup (30 seconds):**
1. Install the [ntfy app](https://ntfy.sh/#subscribe) on iOS or Android (or use the web UI at ntfy.sh)
2. Subscribe to a topic name that only you know — e.g. `claude-runner-abc123`
3. Run `claude-runner configure` and select **ntfy.sh** — it will send a test message instantly

```yaml
# In your project book:
notify:
  on: [complete, error, rate_limit]
  channels:
    - type: webhook
      url: https://ntfy.sh/your-topic-name
```

### Desktop

Uses the Windows system notification API via `apprise`. Works on Windows 10/11 with no external dependencies (bundled in the .exe).

```yaml
notify:
  channels:
    - type: desktop
```

### Email

Sends plain-text email via SMTP. Useful for detailed completion reports with the git diff summary. Gmail users must use an [App Password](https://support.google.com/accounts/answer/185833).

`claude-runner configure` walks through the Gmail App Password flow step by step.

```yaml
notify:
  channels:
    - type: email
      to: you@gmail.com
```

### Event routing table

| Event | Default desktop | Default email | Default webhook |
|---|---|---|---|
| `on_complete` (success) | yes | no | no |
| `on_complete` (with changes) | yes | no | no |
| `on_error` | yes | yes | no |
| `on_rate_limit` | yes | no | no |
| `on_abort` | yes | no | no |
| `on_timeout` | yes | yes | no |
| `on_context_trim` | no | no | no |

All defaults can be overridden in `config.yaml` under `notifications.routing` or per-project under the `notifications` key of the project book.

---

## Context Length Management

Claude Code conversations grow with every tool call, file read, and assistant message. When the accumulated token count approaches the model's context window, the quality of responses degrades and the API eventually returns a `context_length_exceeded` error.

claude-runner tracks approximate token usage by counting characters in the raw conversation stream and applying a conservative characters-per-token estimate. When usage crosses the configured threshold (default: 80% of the model's context window), it applies the strategy from `context_strategy` in the project book:

| Strategy | Token cost | Reliability |
|---|---|---|
| `continue` | Minimal | Good for short tasks or tasks with a clear continuation point |
| `restate` | Low | Better for multi-step tasks where Claude may drift |
| `summarize` | Medium | Best for very long tasks; uses an extra API call to distill progress |

The threshold and estimation parameters are configurable:

```yaml
context:
  warning_threshold_pct: 80   # Trigger at 80% of window
  chars_per_token: 4           # Used for approximate counting
  default_strategy: restate
```

Trimming events are logged and included in the final run summary. By default they do not trigger notifications; enable them by setting `on_context_trim` in the routing table.

---

## Configuration

The global configuration file lives at `~/.claude-runner/config.yaml`. It is created by `claude-runner configure` and can also be edited by hand.

```yaml
# ~/.claude-runner/config.yaml

# Anthropic API key. Can also be set via the ANTHROPIC_API_KEY env var.
api_key: "sk-ant-..."

# Default sandbox mode for all projects unless overridden in the project book.
# Options: docker | native
sandbox:
  default: docker

  # Docker-specific settings
  docker:
    # Base image to use for sandboxed runs.
    image: claude-runner-base:latest
    # Remove the container after each run (set false to inspect failed runs).
    auto_remove: true
    # Keep the container alive on error for post-mortem debugging.
    keep_on_error: false
    # Memory limit passed to `docker run --memory`.
    memory_limit: "4g"
    # CPU limit passed to `docker run --cpus`.
    cpu_limit: "2.0"
    # Disable network access inside the container.
    # Useful when the task must not make outbound requests.
    network_disabled: false

# Context length management defaults (can be overridden per project book).
context:
  warning_threshold_pct: 80
  chars_per_token: 4
  default_strategy: continue

# Rate limit defaults (can be overridden per project book).
rate_limit:
  default_strategy: continue
  max_backoff_seconds: 1800

# Notification channel configuration.
notifications:
  desktop:
    enabled: true

  email:
    enabled: false
    smtp_host: smtp.gmail.com
    smtp_port: 587
    smtp_starttls: true
    username: ""
    password: ""          # Or set CLAUDE_RUNNER_SMTP_PASSWORD env var.
    from_address: ""
    to_addresses: []

  webhook:
    enabled: false
    url: ""
    method: POST
    headers: {}
    body_template: ""

  # Override which events trigger which channels.
  routing:
    on_complete:
      desktop: true
      email: false
      webhook: false
    on_error:
      desktop: true
      email: true
      webhook: false
    on_rate_limit:
      desktop: true
      email: false
      webhook: false
    on_abort:
      desktop: true
      email: false
      webhook: false
    on_timeout:
      desktop: true
      email: true
      webhook: false
    on_context_trim:
      desktop: false
      email: false
      webhook: false

# Directory where run logs are stored.
# Defaults to ~/.claude-runner/logs/
log_dir: ""

# Log retention in days. Logs older than this are pruned on startup.
# Set to 0 to disable pruning.
log_retention_days: 30
```

---

## Requirements

| Requirement | Minimum version | Notes |
|---|---|---|
| Python | 3.11 | Only needed when installing from source. Not required for the .exe. |
| Claude Code CLI | Latest | Must be installed and authenticated before running claude-runner. Install with `npm install -g @anthropic-ai/claude-code`. |
| Node.js | 18 LTS | Required by Claude Code CLI. |
| Docker Desktop | 4.x | Required for `sandbox: docker` mode only. Must be running when claude-runner starts. |
| Windows | 10 or 11 (64-bit) | The pre-built .exe targets Windows only. Source installation works on macOS and Linux with `sandbox: native`. |

**Verifying the Claude Code CLI:**

```cmd
claude --version
claude auth status
```

Both commands must succeed before running claude-runner. If `claude auth status` shows unauthenticated, run `claude auth login`.

---

## Development

### Install from source

```cmd
git clone https://github.com/your-org/claude-runner.git
cd claude-runner
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

The `[dev]` extra installs `pytest`, `pytest-asyncio`, `ruff`, `mypy`, and `pyinstaller`.

### Run tests

```cmd
pytest tests/
```

Individual test suites:

```cmd
pytest tests/unit/          # fast, no Docker required
pytest tests/integration/   # requires Docker Desktop running
pytest tests/e2e/           # requires Docker + a valid ANTHROPIC_API_KEY
```

### Lint and type-check

```cmd
ruff check claude_runner/
mypy claude_runner/
```

### Build the executable

```cmd
python build_exe.py --clean
```

The output is `dist/claude-runner.exe`. See `build_exe.py --help` for options.

Alternatively, use the spec file directly:

```cmd
pyinstaller claude-runner.spec
```

### Project layout

```
claude-runner/
  claude_runner/          Python package
    main.py               Entry point (also used by PyInstaller)
    cli.py                Click command definitions
    runner.py             Core orchestration loop
    config.py             Config loading and validation
    project.py            Project book schema (Pydantic)
    sandbox/
      docker_sandbox.py   Docker-based isolation
      native_sandbox.py   Host-based execution
    rate_limit.py         Rate-limit detection and backoff
    notifications/
      desktop.py          plyer desktop toasts
      email.py            SMTP notifications
      webhook.py          HTTP webhook dispatch
    context.py            Token counting and trimming
    status.py             Run status tracking
    logs.py               Log streaming and storage
  projects/               Bundled example project books
  docker/
    Dockerfile            Base image definition
  assets/
    claude-runner.ico     Application icon
  tests/
  build_exe.py            PyInstaller build script
  claude-runner.spec      PyInstaller spec file
  pyproject.toml
  README.md
```

### Contributing

1. Fork the repository.
2. Create a branch: `git checkout -b feature/my-change`.
3. Make your changes and add tests.
4. Run `ruff check`, `mypy`, and `pytest` until all pass.
5. Open a pull request against `main`.

---

## License

MIT License. See [LICENSE](LICENSE) for the full text.
