# claude-runner — Session Handoff
_Last updated: 2026-03-15_

---

## 1. Current State

### What works
- **CLI skeleton**: `claude-runner run <yaml>`, `claude-runner new <name>`, `configure`, `list`, `status`, `logs`, `cancel`, `clean` all wired and callable.
- **One-YAML-one-folder layout**: YAML filename stem is the unique project ID for all filesystem ops (working dir, logs, state, git branch). `resolve_working_dir()` in `sandbox/__init__.py` is the single authoritative resolver.
- **Project seeding**: on init, the YAML is copied into `./examples/examples.yaml` (or `./pj1/pj1.yaml`) so Claude can read the spec from inside the sandbox.
- **NativeSandbox**: `ClaudeProcess` (pywinpty/ConPTY) now actually calls `.start()` — the PTY spawns. This was the silent-success bug.
- **Async wait**: `ClaudeProcess.wait()` is a proper coroutine wrapping `run_in_executor(_reader_thread.join)`.
- **Race condition fixed**: after `done_task` fires the runner yields `await asyncio.sleep(0)` before judging exit, so a `##RUNNER:COMPLETE##` set via `call_soon_threadsafe` isn't missed.
- **`--sandbox` flag disabled on Windows**: Claude Code's `--sandbox` uses Linux namespaces — adding it on Windows caused exit `0xC000013A` (STATUS_CONTROL_C_EXIT). Now defaults to `False` on `win32`.
- **OAuth detection**: `_detect_oauth_session()` now has two methods — checks `.credentials.json` first, then falls back to `claude auth status` JSON output. Correctly detects claude.ai Pro login stored in Windows Credential Manager.
- **Debug-by-default logging**: log level defaults to `DEBUG` until `~/.claude-runner/.first_success` is written on first successful run, then switches to `WARNING`.
- **Single spec**: `claude-runner.spec` (merged debug+release: `noarchive=True`, `upx=False`, `debug=False`). `claude-runner-debug.spec` deleted.
- **PyInstaller exe**: `dist/claude-runner.exe` + `release/claude-runner.exe` (same file).

### What has NOT been tested end-to-end yet
- A full successful Claude Code run (furina-ascii or any task) — the previous session fixed the last known blocker (`.start()` missing + `--sandbox` crash) but a green run has not been confirmed.
- Rate-limit handling, resume/checkpoint, context-window injection.
- Docker sandbox backend.
- Notifications (desktop + ntfy.sh webhook).

---

## 2. Next Immediate Task — First Green Run

Run the furina-ascii example and confirm it completes:

```
cd release\
claude-runner.exe run examples.yaml
```

Expected happy path:
1. `_ensure_initialized()` — no warnings (OAuth detected, claude on PATH).
2. `NativeSandbox.setup()` — working dir `./examples/` created, YAML seeded.
3. `ClaudeProcess.start()` — PTY spawns `claude --dangerously-skip-permissions -p <prompt>`.
4. Claude Code runs, writes `furina.py` + packages `dist/furina.exe`.
5. `##RUNNER:COMPLETE##` marker fires → `_handle_completion()` → report written.
6. `~/.claude-runner/.first_success` created → subsequent runs quiet.

If it still fails, check `~/.claude-runner/logs/examples_progress.log` for Claude's actual output — that's the first thing to read.

---

## 3. Open Bugs / Known Issues

| # | Symptom | Likely cause | Status |
|---|---------|-------------|--------|
| 1 | Exit `-1` treated as clean exit | pywinpty can't read ConPTY exit status | Workaround in runner: `-1` → `_handle_completion()`. Real fix: use winpty's `waitForExit()` if available. |
| 2 | `on_exit=None` passed to ClaudeProcess | Runner uses `wait()` instead of callback for exit detection | Harmless (None-guarded), but inelegant. |
| 3 | Prompt length on CLI | `_list_to_cmdline` puts full prompt as a CLI arg — Windows limit is 32767 chars. Long prompts will silently truncate or fail. | Not yet hit; mitigate by writing prompt to a temp file and using `--file` if Claude Code supports it. |
| 4 | `##RUNNER:COMPLETE##` race still possible | `asyncio.sleep(0)` yields once but only drains one iteration of the event loop | Low probability; if seen again, increase sleep or use a dedicated done-event. |
| 5 | `_detect_oauth_session()` runs `claude auth status` subprocess on every API key resolution | Slow (~200 ms) and fails if `claude` is slow to start | Cache result for process lifetime (simple module-level `_oauth_detected` flag). |

---

## 4. Key Architectural Decisions This Session

### One-YAML-one-folder invariant
YAML filename stem (OS-enforced unique) is the single key for everything:
- Working dir: `./examples/` derived from `examples.yaml`
- Log/report prefix: `examples_*`
- Git branch: `claude-task/examples-<timestamp>`
- State file: `examples.json`
`name:` field inside the YAML is display-only. Centralised in `resolve_working_dir()`.

### `ClaudeProcess` interface contract
- `start()` must be called by the sandbox (not the runner) after construction.
- `wait()` is an async coroutine (wraps `run_in_executor(_reader_thread.join)`).
- `on_exit=None` is valid — runner uses `wait()` exclusively for exit detection.
- Exit code `-1` means pywinpty couldn't read the ConPTY exit status; treat as clean if no error markers were detected.

### Auth resolution order (5-source chain, main.py `_resolve_api_key`)
1. `ANTHROPIC_API_KEY` env var
2. Windows registry (`HKCU\Environment`, `HKLM\...\Environment`)
3. `~/.claude-runner/secrets.yaml` → `api_key` field
4. Windows Credential Manager (keyring)
5. Claude Code OAuth session (`_detect_oauth_session()` — checks `.credentials.json` then `claude auth status`)

If OAuth sentinel returned → `ANTHROPIC_API_KEY` is NOT injected into subprocess env; Claude Code authenticates itself.

### `--sandbox` flag
Disabled by default on Windows (`sys.platform == "win32"`). Claude Code's sandbox uses Linux/macOS OS primitives. Enabling it on Windows causes `STATUS_CONTROL_C_EXIT` (0xC000013A) immediately on launch.

### Debug-by-default
`logging.basicConfig` uses `DEBUG` level until `~/.claude-runner/.first_success` exists. Written by `main.py` after first `result.status == "complete"`. After that, defaults to `WARNING` (suppressible per-run with `--verbose`).
