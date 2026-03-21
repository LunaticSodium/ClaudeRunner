# Supervisor Spellbook — v2.0

Read this file when you need to find a tool or function for a specific task.
This is the authoritative reference for supervisor capabilities.

---

## 1. Worker Management

| Function | Module | What it does | When to use |
|---|---|---|---|
| `daemon.dispatch_worker(project_book_path)` | daemon.py | Launch a new Dash worker subprocess for a project book | Starting a new worker task |
| `daemon.terminate_worker(worker_id, reason)` | daemon.py | Send SIGTERM to a worker, mark completed | Intervention L3 — costs budget points |
| `daemon.list_workers()` | daemon.py | Return status dict of all workers | Monitoring, before intervention decisions |
| `daemon.pause_project(project_id)` | daemon.py | Set pause_requested flag in worker state file | Graceful pause before intervention |
| `daemon.resume_project(project_id)` | daemon.py | Resume a paused worker (spawns new subprocess) | After successful intervention |

**Key rule**: The supervisor writes project books to control workers. It NEVER executes worker scripts directly.

---

## 2. Budget System (Dual-Channel Enforcement)

| Function | Module | What it does | When to use |
|---|---|---|---|
| `SupervisorBudget(audit_dir, initial_points)` | supervisor_protocol.py | Create budget tracker | At supervisor init |
| `budget.remaining_points` | supervisor_protocol.py | Read current point balance | Before any intervention |
| `budget.can_intervene` | supervisor_protocol.py | Hard gate: True if points > 0 | Gate check before intervention |
| `budget.deduct_points(failure_type, detail, thinking)` | supervisor_protocol.py | Deduct points, write accident snapshot | After a failed intervention |
| `budget.credit_points(reason)` | supervisor_protocol.py | Credit for correct preflight | After preflight prediction confirmed |
| `budget.get_faux_alarm_message()` | supervisor_protocol.py | Get escalation message for current threshold | Injected into pending.md |
| `budget.format_budget_status()` | supervisor_protocol.py | Format for pending.md injection (soft channel) | Periodic checkpoint |

**Point costs**: worker_crash_l3=3, worker_crash_l1=1, misdiagnosis=2, false_flag=4, correct_preflight=-1

**Faux-alarm thresholds**: 3 (notice), 6 (warning + predecessor story), 9 (critical), 10 (review/suspended)

---

## 3. KPI Collection & Assessment

| Function | Module | What it does | When to use |
|---|---|---|---|
| `KPICollector()` | kpi_collector.py | Create metrics collector | At supervisor init |
| `collector.assess(metrics)` | kpi_collector.py | Assess single worker KPI | Per-worker monitoring |
| `collector.assess_peer_group(all_metrics)` | kpi_collector.py | Assess all workers with peer ranking | When multiple workers running |
| `WorkerMetrics(worker_id, ...)` | kpi_collector.py | Raw metrics dataclass | Populate from worker state |

**Derived signals**:
- `progress_rate`: phases_completed / elapsed normalised against expected duration
- `silence_ratio`: last_commit_age / expected_commit_interval
- `peer_rank`: percentile among parallel workers (0.0 = worst, 1.0 = best)

**Severity levels**: normal → warning → critical

---

## 4. Worker Supervision & Intervention

| Function | Module | What it does | When to use |
|---|---|---|---|
| `WorkerSupervisor(config, budget, audit_dir)` | worker_supervisor.py | Create supervision engine | At supervisor init |
| `supervisor.register_worker(worker_id, pid)` | worker_supervisor.py | Register worker for monitoring | After dispatch_worker |
| `supervisor.diagnose(worker_id, assessment)` | worker_supervisor.py | Root-cause diagnosis | When KPI shows underperformance |
| `supervisor.should_intervene(worker_id, diagnosis)` | worker_supervisor.py | 5-gate intervention check | Before any intervention |
| `supervisor.should_escalate_to_human(worker_id)` | worker_supervisor.py | True if intervention limit reached | After should_intervene returns False |
| `supervisor.record_intervention(worker_id, level, cause, action)` | worker_supervisor.py | Log intervention in history | After executing intervention |
| `supervisor.record_accident(worker_id, failure_type, detail, thinking)` | worker_supervisor.py | Record failure, deduct budget | When intervention fails |
| `supervisor.format_escalation_message(worker_id, diagnosis)` | worker_supervisor.py | Format human escalation message | When escalation needed |

**Intervention levels**:
1. Re-describe — rewrite worker's project YAML with clearer target
2. Split — decompose stalled task into two smaller project YAMLs
3. Restart + reconfig — kill and relaunch with adjusted parameters

**5 gates** (ALL must pass before intervention):
1. Budget check (points remaining)
2. Process resource check (CPU/mem — F4 hard gate)
3. Cooldown (30 min between interventions on same worker)
4. Intervention limit (3 per worker before human escalation)
5. Cause check (rate_limited/environment → do NOT intervene)

**Cause → level mapping**:
- unclear_requirements → L1 (re-describe)
- task_too_large → L2 (split)
- wrong_model, stuck_in_loop → L3 (restart)
- rate_limited, environment_issue → L0 (wait, no intervention)

---

## 5. Thinking Manual (Two-Track Reasoning)

| Function | Module | What it does | When to use |
|---|---|---|---|
| `ThinkingManual()` | thinking_manual.py | Create reasoning engine | At supervisor init |
| `manual.build_prompt(context, stage)` | thinking_manual.py | Build Track 1+2 prompt | Before supervisor LLM call |
| `manual.parse_response(response, stage)` | thinking_manual.py | Parse LLM response to findings | After supervisor LLM responds |
| `manual.format_for_audit(result)` | thinking_manual.py | Format for audit log | Logging results |

**Stages and which tracks run**:
- `intake`: Track 2 only (completeness check)
- `preflight`: Track 1 + Track 2
- `self_check`: Track 1 + Track 2

**Track 1** (Creative): 4 adversarial questions — surfaces unknown unknowns
**Track 2** (Controlled): 5 systematic categories — units, design space, domain grounding, dependency chain, output spec

**Synthesis rule**: Track 1 takes precedence when tracks disagree on priority.

---

## 6. Supervisor Protocol (Channel Discipline)

| Function | Module | What it does | When to use |
|---|---|---|---|
| `SupervisorProtocol(config, project_id, ntfy_client, working_dir)` | supervisor_protocol.py | Create protocol enforcer | At marathon init |
| `protocol.validate_channel_write(channel, caller)` | supervisor_protocol.py | Enforce write-channel discipline | Before any ntfy publish |
| `protocol.trigger_self_check(dash_n)` | supervisor_protocol.py | Trigger post-Dash self-check | After each Dash completes |
| `protocol.wait_for_confirm(intent_message)` | supervisor_protocol.py | Human confirm gate (with timeout) | Before destructive actions |
| `protocol.log_event(event_type, detail)` | supervisor_protocol.py | Append to audit log | Any significant event |
| `protocol.handle_violation(detail)` | supervisor_protocol.py | Log violation + set halt flag | Protocol breach detected |

---

## 7. Intake Validation (§8)

| Function | Module | What it does | When to use |
|---|---|---|---|
| `build_intake_prompt(project_yaml, preset_content)` | supervisor_protocol.py | Build LLM intake prompt | Before launching workers |
| `parse_intake_response(response_text)` | supervisor_protocol.py | Parse LLM intake result | After LLM responds |

**Checklist items** (from preset file):
1. Design space clearly defined
2. Objectives unambiguous with numerical target
3. Known constraints stated
4. Output specification explicit
5. At least one domain anchor with numerical targets
6. Key parameters sourced with context
7. No critical solver parameters left at defaults

**Outcomes**: pass / partial / fail (only "fail" blocks launch)

---

## 8. Process Resource Check (F4 Hard Gate)

| Function | Module | What it does | When to use |
|---|---|---|---|
| `check_worker_process_alive(pid)` | supervisor_protocol.py | Check CPU/memory of worker process | BEFORE any intervention decision |

Returns `{cpu_percent, memory_mb, is_active}`. If `is_active=True`, the worker is computing — silence is expected. **Do NOT intervene on a computing worker.**

Requires `psutil`. Falls back to `is_active=True` (safe default) if psutil unavailable.

---

## 9. Notification Events (v2.0)

| Event | When fired | Channels |
|---|---|---|
| `supervisor_accident` | Budget points deducted | desktop, webhook |
| `intake_pass` / `intake_partial` / `intake_fail` | After intake validation | desktop, webhook |
| `preflight_finding` | Thinking Manual finding | desktop, webhook |
| `preflight_action` | Action taken on finding | desktop, webhook |
| `kpi_warning` | Worker underperformance detected | desktop, webhook |
| `intervention` | Intervention executed | desktop, webhook |
| `escalate_to_human` | Intervention limit reached | desktop, webhook, ntfy |

---

## 10. ntfy Channels

| Channel | Direction | Purpose |
|---|---|---|
| `claude-runner-honacoo` | supervisor → human | Notifications, alerts, status updates |
| `claude-runner-honacoo-cmd` | human → supervisor | Commands, responses, overrides |

---

## 11. Audit Files

| File | Location | Written by | Purpose |
|---|---|---|---|
| `supervisor_log.md` | `audit/` | Python (supervisor_audit.py) | All events timestamped |
| `supervisor_budget.md` | `audit/` | Python (supervisor_protocol.py) | Budget state — LLM cannot modify |
| `supervisor_history.md` | `audit/` | Python (supervisor_protocol.py) | Predecessor failure story (faux-alarm) |
| `self_check_log.md` | `audit/` | Python (supervisor_audit.py) | Self-check results per Dash |
| `accident_snapshots/` | `audit/` | Python (supervisor_protocol.py) | Frozen supervisor thinking at each accident |

---

## Quick Reference: Intervention Flow

```
1. Collect metrics → WorkerMetrics
2. Assess KPI     → KPICollector.assess() → KPIAssessment
3. If underperforming:
   a. Diagnose    → WorkerSupervisor.diagnose() → Diagnosis
   b. Gate check  → WorkerSupervisor.should_intervene()
   c. If gated out → check should_escalate_to_human()
   d. If passed   → execute intervention (L1/L2/L3)
   e. Record      → record_intervention() or record_accident()
4. Inject budget  → budget.format_budget_status() → inbox.append_message()
```
