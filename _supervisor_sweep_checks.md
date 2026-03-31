# Supervisor Sweep Validation Checks

These checks apply to any runner task that generates parameter sweeps and plots.
The supervisor (or worker self-check) MUST apply them at the indicated stages.

---

## P4 — Pre-Execution Static Code Check

**When**: Before running any sweep script (e.g. `run_sweeps_and_plots.py`).

**What**: Deterministically verify that every swept variable is actually passed
through to the solver. This catches the most obvious omission class: the sweep
loop updates a local variable but never feeds it to the function that constructs
the solver.

**Procedure** (can be done by reading the code, no execution needed):

1. Identify all sweep parameter names (e.g. `bto_thickness_um`, `electrode_gap`,
   `phi_deg`, `voltage`, `spacer_thickness`).
2. For each parameter, trace the data path:
   ```
   sweep loop variable
     → function argument (e.g. evaluate_point)
       → solver constructor / attribute setter (e.g. solver.electrode_gap = gap)
   ```
3. **FAIL** if any parameter's trace is broken — i.e. the sweep value is assigned
   to a local variable but that variable is never passed to the solver constructor
   or setter. Common failure patterns:
   - Solver constructor uses a module-level constant instead of the function argument
   - Function signature omits the parameter entirely
   - Parameter is passed but the solver attribute is set from a different variable
4. **FAIL** if any solver attribute relevant to the sweep is set from a hardcoded
   constant rather than the function argument.
5. Print a checklist before proceeding:
   ```
   [PASS] bto_thickness_um → _make_solver(d_bto=...) → solver.bto_thickness = d_bto
   [PASS] sin_rib_width    → _make_solver(w_rib=...) → solver.sin_rib_width = w_rib
   [FAIL] electrode_gap    → gap=v (local only) → solver.electrode_gap = ELECTRODE_GAP (constant!)
   ```

**Incident that motivated this check**: bto-runner-bo 2026-03-29 — `run_sweeps_and_plots.py`
updated `gap = v` in the sweep loop but `_make_solver()` hardcoded
`s.electrode_gap = ELECTRODE_GAP`. Both VpiL and loss vs electrode gap were flat.
The bug passed all acceptance criteria because those only checked file existence and
count, not physics content.

---

## P2-Enhanced — Post-Execution Output Validation

**When**: After every sweep script completes, before committing results.

**What**: Deterministically verify that varying each sweep parameter actually
changes the output. This catches runtime-level parameter passing errors that
P4 might miss (e.g. the code looks correct but a default argument shadows
the passed value, or unit conversion zeroes out the effect).

**Procedure**:

1. For each sweep result set (e.g. `vpiL_vs_electrode_gap` data):
   a. Load the (x, y) arrays from the sweep output.
   b. Compute `max(y) - min(y)`.
   c. **FAIL** if the range is zero or negligibly small relative to the mean:
      ```python
      rel_range = (max(y) - min(y)) / (abs(mean(y)) + 1e-15)
      if rel_range < 0.01:  # less than 1% variation
          FAIL(f"{sweep_name}: output varies by only {rel_range*100:.2f}% — "
               f"parameter is probably not being passed to the solver")
      ```
2. For sweeps with known physics expectations, apply additional checks:
   - **Electrode gap vs VpiL**: should be approximately linear (R^2 > 0.9 for
     linear fit), VpiL should increase with gap
   - **Crystal angle vs VpiL**: should show cos^2-like dependence (minimum at 0 deg)
   - **BTO thickness vs VpiL**: should decrease with increasing thickness
   - **Voltage vs delta_n**: should be approximately linear
3. Print a validation table:
   ```
   [PASS] vpiL_vs_bto_thickness:  rel_range=85.3%  (expected: decreasing trend)
   [PASS] vpiL_vs_wg_width:       rel_range=42.1%
   [FAIL] vpiL_vs_electrode_gap:  rel_range=0.00%  *** PARAMETER NOT VARYING ***
   [FAIL] loss_vs_electrode_gap:  rel_range=0.00%  *** PARAMETER NOT VARYING ***
   ```
4. **Do NOT commit Phase 4** if any sweep fails validation. Fix the code first.

**Allowed exceptions**: Loss vs electrode gap may legitimately have low variation
if the solver does not model metal absorption (flat im(n_eff)). In this case,
the validation should note: "Loss flat — expected if solver uses real metal
permittivity." But VpiL vs gap must ALWAYS vary.

---

## Integration with Runner YAML

To activate these checks in a project book, add to the prompt:

```
Before running any sweep or plotting script, read `_supervisor_sweep_checks.md`
and apply the P4 pre-execution check. After the script completes, apply the
P2-Enhanced post-execution check. Do NOT commit results that fail either check.
```

Or add as acceptance criteria:
```yaml
- type: command
  run: "python -c \"...validation script checking rel_range...\""
  expect_exit: 0
```
