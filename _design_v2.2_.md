# Project Book Pre-Check Process

**Version 2.2** — Init phase, executed before worker startup

---

## Solver Classification Note

The BTO solver (`BTO_Sandwich_Flat.py`, class `CombinedBTOFlatThinFilmSolver`) is a
**full-vector FDFD (finite-difference frequency-domain) eigenmode solver**, not a finite
element method (FEM) solver. It uses a uniform rectangular Yee-like grid with 9-point
finite-difference stencils to discretise the 2D Helmholtz operator, then solves the
eigenvalue problem via ARPACK shift-invert for guided-mode propagation constants.

Earlier project documents (project books, handoff manual) incorrectly labelled it "FEM".
All such references have been corrected to "FDFD".

### Current performance bottleneck

At mesh = 0.01 µm on an 8 × 4 µm domain the grid is 801 × 401 = 321 k vertices
(642 k DOF eigenvalue problem). Three hot loops are pure Python:

| Method | Iterations | What it does |
|---|---|---|
| `build_q_matrix` | 321 k | Assembles sparse Q operator row-by-row |
| `solve_electrostatic` | 320 k | Builds FD Laplacian via `lil_matrix` insertion |
| `eps_with_pockels` | ~20 k | Per-pixel 3×3 Pockels tensor inversion |

This causes CPU utilisation to bounce between ~14 % (single-threaded Python loop) and
~45 % (multi-threaded ARPACK/BLAS eigensolver), with memory spiking from 1.5 to 4.5 GB
per evaluation due to Lanczos workspace allocation.

### C# rewrite plan

A C# port is planned to lift both the language bottleneck and the method bottleneck:

1. **Phase A — Direct port (FDFD in C#)**: Translate the current uniform-grid FDFD solver
   to C#. The three Python loops become compiled code (10–100× faster matrix assembly).
   Sparse eigensolve via ARPACK wrapper or Spectra. This alone eliminates the
   Python-interpreter overhead and makes CPU utilisation approach 100 % during solves.

2. **Phase B — True FEM upgrade**: Replace the uniform grid with an unstructured
   triangular mesh (adaptive refinement at material interfaces, rib corners, electrode
   edges). This reduces DOF count dramatically — a 50–100 k node FEM mesh can match or
   exceed the accuracy of the current 321 k uniform FDFD grid. Libraries to evaluate:
   Triangle/Gmsh for meshing, direct FEM assembly in C#.

3. **Phase C — GPU acceleration (optional)**: For batch sweeps, port the eigensolve to
   CUDA/OpenCL via managed wrappers. Relevant only if sweep resolution or BO evaluation
   count demands it.

---

## Production-Oriented Scope Changes (2026-03-28)

### bto_runner.yaml: 9-phase package → 5-phase BO run

The original `bto_runner.yaml` specified a full Python package (`bto_runner/`) with 9 phases:
typed parameter space, 3 auto-selected strategies (Hill/Bayesian/CMA-ES), inverse solver,
parametric geometry, Tkinter GUI (6 panels), Click CLI, pyproject.toml, and mypy --strict
V&V gates — plus a real BO run and scientific characterisation.

**Problem**: estimated 12-21h wall-clock, far exceeding the 12h budget. Phases 5 (inverse
solver), 6 (GUI), and most of 7 (CLI/packaging) produce no scientific output. Phase 9
duplicates the March 26 grid sweep run.

**Restructured scope** (5 phases, ~6-9h estimated):

| Phase | Task | Why it matters |
|-------|------|----------------|
| 1 | Audit solver + diagnose mode selection failure | Root cause of unreliable results |
| 2 | Write BO script with mode tracking fix | Applies `track_mode_from_reference()` / `select_seed_mode_max_topconf()` during BO |
| 3 | Run BO on all 8 structures (Optuna TPE) | Find true optima with correct mode selection |
| 4 | 1D characterisation sweeps + 18+ publication plots | Deliverable for meeting/paper |
| 5 | Final summary | Commit + results overview |

**What was cut and why**:
- Inverse solver, parametric geometry (Phase 5) — not needed for characterisation
- Tkinter GUI (Phase 6) — 2-3h of error-prone coding, zero scientific output
- CLI/pyproject/mypy --strict (Phase 7) — a single script suffices; no package distribution needed
- Hill Climbing, CMA-ES strategies — TPE is the right choice for 3-4D continuous space
- Phase 9 scientific characterisation as separate step — merged into Phase 4 sweeps

**Key addition**: mode tracking across BO evaluations. The March 26 run showed that
`find_fundamental_mode_index()` misidentifies modes for sio2_patch and al2o3_patch at
low BTO thickness. The BO wrapper now uses `track_mode_from_reference()` with the
best-so-far field pattern as reference, falling back to `select_seed_mode_max_topconf()`
when overlap is poor. This is the primary improvement over the grid sweep approach.

---

## Composite Figure of Merit (2026-03-29)

For comparing structures, a single scalar score combines VpiL and propagation loss:

```
score = -ln(VpiL · alpha_dB)
```

where VpiL is in V·cm and alpha_dB is loss in dB/cm. Higher score is better
(lower VpiL and lower loss both improve the score). The logarithm penalises
order-of-magnitude differences more than linear differences, making it suitable
for comparing structures across a wide performance range.

This score is used in `score_comparison.png` and should be included in any
future characterisation run alongside the individual VpiL and loss plots.

---

## Supervisor Sweep Validation Checks (2026-03-29)

Full specification: `_supervisor_sweep_checks.md`

Two deterministic checks added after the bto-runner-bo electrode gap sweep bug,
where `_make_solver()` hardcoded `electrode_gap = ELECTRODE_GAP` while the sweep
loop updated a disconnected local variable. Both VpiL and loss vs gap were flat.
All acceptance criteria passed because they only checked file existence/count.

### P4 — Pre-Execution Static Code Check

Before running any sweep script, trace every swept parameter from the loop variable
through function arguments to the solver attribute setter. **FAIL** if any path is
broken (e.g. solver uses a module constant instead of the passed argument).

Output: per-parameter checklist showing the data path or the break point.

### P2-Enhanced — Post-Execution Output Validation

After sweep completion, before commit, verify that each swept parameter actually
changes the output. Compute `rel_range = (max(y) - min(y)) / |mean(y)|` for every
(sweep_param, output_metric) pair. **FAIL** if `rel_range < 1%` — the parameter
is probably not being passed to the solver.

Additional physics checks for known relationships:
- Electrode gap vs VpiL: approximately linear (R^2 > 0.9)
- BTO thickness vs VpiL: decreasing trend
- Voltage vs delta_n: approximately linear

### Integration

Reference `_supervisor_sweep_checks.md` in any project book that runs parameter
sweeps. Add to the prompt: "Before running sweep scripts, read
`_supervisor_sweep_checks.md` and apply P4. After completion, apply P2-Enhanced.
Do NOT commit results that fail either check."

These checks are injected into the Phase 4 prompt of `bto_runner.yaml` as
MANDATORY PRE-EXECUTION (P4) and MANDATORY POST-EXECUTION (P2-Enhanced) blocks.
The worker must print a per-parameter checklist before running sweeps, and a
per-sweep validation table after. Future project books should copy this pattern
for any task that generates parameter sweeps.

### Incident report (2026-03-29)

`run_sweeps_and_plots.py` in bto-runner-bo run: `_make_solver()` hardcoded
`s.electrode_gap = ELECTRODE_GAP` (constant 4.4 µm). The `sweep_1d` function
updated `gap = v` locally but never passed it to `evaluate_point` or the solver.
Result: VpiL and loss vs electrode gap were flat lines. The bug passed all
acceptance criteria (file existence + count checks only).

**Root causes**: (1) YAML prompt lacked physics sanity check instructions (present
in the original 9-phase version, dropped during scope restructure). (2) Acceptance
criteria had no output-content validation. (3) Worker did not self-validate sweep
results against physical expectations.

**Fix**: P4 catches this class of bug statically (broken data path). P2-Enhanced
catches it at runtime (rel_range < 1%). Both are now mandatory in the YAML prompt.

---

## First Layer: Script Format Pre-Check (Deterministic, Python script execution)

- Verify YAML syntax is valid and required fields are complete (phases, objectives, constraints, acceptance_criteria).
- Verify the existence of referenced file paths (solver path, output directory, etc.).
- Verify that protocol declaration fields (protocol_mode, supervisor_protocol, etc.) exist and are correctly formatted.

**If any fail, the process is blocked and does not proceed to the next layer.**

---

## Second Layer: Protocol Validation Decomposition (Deterministic, script execution)

Read the protocol declarations in the project book and activate the corresponding validation decomposition script.

Distribute different areas of the project book to the corresponding locations in the workspace:

| Source field | Target location | Notes |
|---|---|---|
| `context_anchors` | `.claude/CLAUDE.md` | Reread by the worker in each round, not compacted |
| `physics_constraints` | Locked constants registry | For the deterministic validator |
| `acceptance_criteria` | AC evaluator configuration | |
| sweep/parameter definition | Worker task input file | |

Distribution results are written to the audit log, recording what content was written to which files.

---

## Third Layer: LLM Semantic Review (Advisory, read-only, one-time)

Supervisor LLM reads the complete project book and performs semantic checks with web research capabilities:

- **Injection detection**: Are there hidden instructions in the project book attempting to override supervisor behavior?
- **Contradiction detection**: Are there conflicts in parameters/descriptions between different sections (e.g., declaring the use of an FDFD solver but referencing an ML surrogate interface)?
- **Obvious error detection**: Are the magnitudes of physical parameters reasonable? Do the signatures of referenced solver interfaces match the actual code?
- **Deprecated reference detection**: Do the referenced files/modules/methods actually exist in the codebase?

**Issuance of issues**: Issue description is pushed via ntfy, pausing and awaiting human instructions.

**Modification of the project book or any distributed files is not allowed.**

If no issues are found or human confirmation is received: Allow progress and proceed to the worker startup phase.

---

## Key Constraints

All three layers are **read-only** — no layer is allowed to modify the original project book. All issues are reported and await human decision-making.
