# ADDENDUM Implementation Report
Date: 2026-03-16T15:15:00Z
Branch: addendum
Features: A4, A1, A2
Tests: 281 passing / 281 total

## A4 summary
Extended `claude_runner/notify.py` with:
- `extract_completion_summary(output_lines)` — scans the output buffer
  backwards past tool-invocation lines to find the last natural-language
  block produced by Claude Code; caps at 3 KB.
- `NotificationManager.build_completion_ntfy_message()` — builds the ntfy
  message body prefixed with "Task | Duration | RL cycles" and truncates to
  4000 chars with "…[truncated]" if needed.
- Module-level `_TOOL_LINE_PATTERN` heuristic (spinner chars, tool call
  prefixes, ##RUNNER: markers).

Wired into `runner.py`:
- `_handle_completion()` computes `duration_str` and calls
  `build_completion_ntfy_message()`; passes the result as
  `_ntfy_message_override` in the event data dict.
- `_dispatch()` pops `_ntfy_message_override` and uses it as the ntfy
  message body when present, falling back to the structured default.

New tests: `tests/test_notify_passthrough.py` — 14 tests.

## A1 summary
New module `claude_runner/inbox.py`:
- `append_message(text)` — appends text with timestamp header to
  `~/.claude-runner/inbox/pending.md`; sets `has_pending_messages = True`.
- `drain(process, timeout_s=60)` — if flag is set, sends the inject prompt,
  waits for acknowledgement via duck-typed polling, truncates pending.md,
  clears flag.
- `is_pending()`, `reset()` helpers.

Wired into `pipeline.py` (A1 routing):
- `_convert()` no longer trashes YAML parse errors or "not a mapping" bodies
  — returns `None` silently so `process()` can route to inbox.
- `_convert()` raises `_PipelineError` (after trashing) for size-limit and
  pydantic validation failures.
- `process()` calls `_route_to_inbox(message.message)` when `_convert()`
  returns `None`.
- `_route_to_inbox()` calls `inbox.append_message()` and publishes an
  acknowledgement to the out channel.

Wired into `runner.py`:
- `_drain_inbox()` helper method; called after rate-limit resume, after
  context checkpoint injection, and at the silence probe point.

Updated: `tests/test_pipeline.py` — 5 existing tests updated.
New tests: `tests/test_inbox.py` — 15 tests.

## A2 summary
New module `claude_runner/git_inbox.py`:
- `fetch_branch(branch_ref, daemon)` — reads GitHub token and repo URL from
  keyring (`claude-runner-github-token`), shallow-clones the branch, scans
  for `*.yaml`, validates each as a ProjectBook, enqueues valid ones via
  `daemon.enqueue()`, logs warnings for invalid files, cleans up temp dir.
- Credentials exclusively from keyring — never hardcoded or env-based for
  the token; repo URL also supports `CLAUDE_RUNNER_GITHUB_REPO_URL` env var
  as fallback.
- Graceful skip with `logger.error` when token or URL is missing; handles
  clone failure without raising.

Extended `pipeline.py`:
- `"fetch"` added to `CONTROL_COMMANDS`.
- `_FETCH_BRANCH_PATTERN` regex enforces `task/<name>` or
  `inbox/<iso-timestamp>` format.
- `_cmd_fetch()` validates pattern, trashes bad refs, calls
  `git_inbox.fetch_branch()`.
- `_handle_control()` dispatches `fetch` keyword to `_cmd_fetch()`.

New tests: `tests/test_git_inbox.py` — 17 tests.

## Overall status: PASS
All 281 tests pass (excluding test_rate_limit.py per task instructions).
Baseline was 235 tests; 46 new tests added across the three features.
