# claude-runner — Addendum Requirements
# Derived from design conversation, 2026-03-16
# Status: concept-level. No acceptance criteria defined unless noted.
# These supplement but do not override the original SPEC.

---

## A1 — Buffer-mediated message injection
**Layer**: Marathon daemon
**Status**: Concept — implementable as v2 extension

The ntfy listener and the active Claude Code session are decoupled via a
buffer file. Messages arriving while Claude is working are queued, not
immediately injected.

Components:
- `pending.md` (or similar) in `~/.claude-runner/inbox/`: append-only during
  accumulation, truncated after delivery
- A bool flag `has_pending_messages` shared between the ntfy listener and
  the runner communication layer
- **Trigger function** (ntfy side): on any inbound message, append to buffer,
  set flag true. Does not interrupt Claude Code.
- **Restore function** (runner side): when Claude reaches a natural pause
  (e.g. waiting for input, or runner detects idle), inject a single prompt
  "Please read ~/.claude-runner/inbox/pending.md and process its contents",
  wait for acknowledgment, truncate buffer, set flag false. Runner resumes
  its prior behavior unconditionally — no state to restore beyond the flag.

Key property: multiple messages (YAML, free-text instructions, git pointers)
accumulate between delivery windows. Claude reads them all at once, in order.
This preserves uninterrupted deep work while retaining responsiveness.

Note: the bool is the entirety of the "state". Calling this a state machine
is an overstatement. If future complexity warrants it, the flag can be
promoted to an enum, but that is not anticipated now.

---

## A2 — Git repository as file transfer channel
**Layer**: Marathon daemon / pipeline
**Status**: Concept — no implementation changes needed beyond pipeline routing

The ntfy channel is intentionally low-bandwidth (text only, ~4KB limit).
For tasks that require transferring code, configs, or structured data, a
git repository serves as the actual payload channel. ntfy carries only a
pointer.

Convention:
- A designated private GitHub repository (or a dedicated branch of the
  ClaudeRunner repo) acts as the drop zone
- Sender pushes content to a branch with an agreed naming pattern,
  e.g. `task/<name>` or `inbox/<iso-timestamp>`
- ntfy message carries only the branch reference:
    `fetch task/refactor-auth-module`
  or implicitly, `run <name>` where the pipeline knows to look for
  `task/<name>` in the configured drop-zone repo

Pipeline handling (extends A3/Feature 4):
- `fetch <branch>`: pull the specified branch into a temp working dir,
  treat any `.yaml` files found as project books, queue them
- Authentication: use a stored credential (Credential Manager, same pattern
  as API key and ntfy keys). Never hardcode.
- Rate limit: at ~1 pull/minute this is well within GitHub authenticated
  limits (5000 req/hour)

Legal/technical notes:
- Requires private repo if code is sensitive
- No third-party services involved beyond GitHub + ntfy
- Git history provides a natural audit log of all submitted tasks

---

## A3 — Optimization-driven idle behavior
**Layer**: Marathon daemon
**Status**: Concept — requires design before implementation

Current behavior: daemon completes a task → waits for next external command.
Desired behavior (long-term): daemon completes a task → reviews its own
outputs → self-generates a follow-up task if improvement is warranted.

Possible idle strategies (not mutually exclusive):
- After every run: scan `self-test-report.md` for FAIL entries, auto-queue
  a fix task
- Periodically: review `progress.log` and git history for incomplete items,
  open TODOs, or failed acceptance checks from previous runs
- On explicit opt-in: a "continuous improvement" mode where the daemon
  treats the codebase itself as the perpetual task

This is architecturally distinct from the current "fire and forget" model.
It requires the daemon to have a goal representation that persists across
task boundaries — not just a queue of externally-submitted work.

Not planned for v2. Noted here as a directional intent.

---

## A4 — Marathon broadcast: Claude Code output passthrough
**Layer**: Marathon daemon / notify
**Status**: Ready — low implementation cost, high value

Runner broadcast is structured and log-oriented:
  `task=xxx, event=complete, duration=0:18:13`

This is appropriate for machine consumption but loses the natural language
summary that Claude Code often produces at task completion.

Marathon broadcast should support a second content layer:
- On `##RUNNER:COMPLETE##` detection, scan backwards in the output buffer
  for the last coherent natural-language block produced by Claude Code
  (heuristic: lines after the last tool invocation, before the marker)
- Alternatively, use the last `[DONE]` entry in `progress.log`
- Append this content to the ntfy completion notification as the message
  body, with the structured fields as a header

Example of desired output (approximating what was received 2026-03-16):
  Title: [claude-runner] complete — claude-runner-v2-marathon
  Body:  v2 marathon mode implemented and validated. 6 features: autostart,
         daemon, ntfy client, pipeline, trash log, self-test yaml.
         265 tests passing. Pushed to origin/marathon.

The structured event data (`duration`, `rate_limit_cycles`) is retained
as a secondary line or omitted from the ntfy body but written to the log.

ntfy message length limit (~4KB) is sufficient for typical Claude summaries.
Truncate gracefully if exceeded, with a note that the full summary is in
the run log.

---

## A5 — ntfy→YAML belongs to marathon layer, not runner
**Layer**: Architecture clarification
**Status**: Confirmed design decision — no implementation action needed

The ntfy inbound channel (cmd channel) and the YAML conversion pipeline
(Feature 4 of v2) are marathon-layer concerns. The small runner has no
inbound channel and no awareness of ntfy topology.

Rationale: the small runner is a one-shot executor. It starts, runs a task,
exits. There is no persistent receiver to accept inbound messages. The
marathon daemon is the persistent receiver; it owns the translation from
ntfy message to project book.

This means: if someone wants to trigger a runner task remotely, the path is
always `ntfy → marathon daemon → runner`, never `ntfy → runner` directly.

---

## Notes on this document

These requirements emerged from a design conversation rather than a formal
spec session. They lack:
- Precise acceptance criteria
- Implementation order relative to each other
- Dependency mapping

Before any of these are scheduled for implementation, the relevant items
should be promoted to the main SPEC (for architectural decisions) or to
a project book (for implementable features). This document is the holding
area.

Priority order (rough):
  A4 (output passthrough) — small, high value, fits naturally in v2
  A1 (buffer injection)   — medium effort, extends v2 ntfy listener
  A2 (git channel)        — small pipeline extension, depends on A1
  A3 (optimization loop)  — large, deferred post-v2
  A5 (architecture note)  — already decided, no action
