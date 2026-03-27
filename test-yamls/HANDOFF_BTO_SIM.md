# BTO Structure Simulation — Handoff Manual

**Date:** 2026-03-27
**Branch:** `testtemp` on `LunaticSodium/ClaudeRunner`

## What This Is

A lean, standalone Python script (`run_bto_lean.py`) that characterizes 8 BTO EO
modulator structures using the FEM solver. Produces 18+ publication plots.

Replaces the ClaudeRunner-driven approach which had two critical problems:
1. LLM worker autonomously reduced grid/mesh (5x5 -> 3x3, mesh 0.01 -> 0.05)
2. Worker used brute-force all-scan (920 solves) instead of targeted sweeps

This script is human-written, deterministic, no LLM in the loop.

## Quick Start

```bash
# 1. Clone or pull
git clone -b testtemp https://github.com/LunaticSodium/ClaudeRunner.git
cd ClaudeRunner/test-yamls

# 2. Check solver package exists
ls bto-sim/py/solvers/BTO_Sandwich_Flat.py   # must exist

# 3. Run (default: mesh=0.02, ~1 hour)
python run_bto_lean.py

# 4. Results appear in output/plots/
```

## Solver Package

The `bto-sim/` folder must be in the working directory. It's already committed
to the `testtemp` branch. Contains:
- `bto-sim/py/solvers/BTO_Sandwich_Flat.py` — the FEM solver class

## Run Options

| Command | Mesh | Est. Time | Quality |
|---|---|---|---|
| `python run_bto_lean.py` | 0.02 | ~1 h | Meeting-ready |
| `python run_bto_lean.py --fine` | 0.01 | ~10 h | Publication |
| `python run_bto_lean.py --mesh 0.015` | 0.015 | ~3 h | Compromise |
| `python run_bto_lean.py --workers 8` | 0.02 | ~30 min | If 8+ cores |

## What It Does

| Step | Task | Solves |
|---|---|---|
| 0 | 5x5 grid sweep to find sweetspot per structure | 200 |
| 1 | 5 x 1D sweeps from each sweetspot (d_bto, w_rib, gap, phi, voltage) | 720 |
| 2 | Literature comparison (analytical, no solves) | 0 |
| 3 | Generate 18+ plots | 0 |
| **Total** | | **920** |

All 920 solves use the same mesh. No BO/ML optimizer — just direct grid + sweeps.
With 4 workers at mesh=0.02, each solve takes ~1-3 seconds.
With mesh=0.01, each solve takes ~12+ minutes (25x more FEM elements).

## Output Files

```
output/plots/
  sweetspot_results.json          # Grid results + best (d*, w*) per structure
  literature_comparison.json      # VpiL comparison with LNOI, SOH, literature
  step0_raw.json                  # Raw solver output for all grid points
  step1_raw.json                  # Raw solver output for all sweeps
  sweetspot_vpil.png              # Bar chart of best VpiL per structure
  literature_comparison.png       # Scatter + literature bands
  vpiL_vs_bto_thickness.png       # 1D sweep
  loss_vs_bto_thickness.png       # 1D sweep
  vpiL_vs_wg_width.png            # 1D sweep
  loss_vs_wg_width.png            # 1D sweep
  vpiL_vs_electrode_gap.png       # 1D sweep
  vpiL_vs_crystal_angle.png       # 1D sweep
  delta_neff_vs_voltage.png       # 1D sweep
  neff_vs_loss.png                # Scatter colored by VpiL
  geometry_sensitivity_<id>.png   # 8 heatmaps (one per structure)
  geometry_sensitivity.png        # Copy of sin_patch for general use
  run_bto_lean.log                # Full log with timestamps
```

## Fixed Parameters (MANDATORY — do not change)

```python
D_BTO_VALS = [0.05, 0.15, 0.25, 0.35, 0.50]   # 5 BTO thicknesses (um)
W_RIB_VALS = [0.5, 0.75, 1.0, 1.5, 2.0]        # 5 waveguide widths (um)
ELECTRODE_GAP = 4.4   # um
VOLTAGE       = 5.0   # V
```

## Previous Run Results (UNRELIABLE)

Three prior runs gave wildly inconsistent sweetspots:

| Run | Mesh | sin_patch VpiL | Typical sweetspot |
|---|---|---|---|
| v1.1.0 (Mar 20) | ~0.05? | 1.81 V*cm | varied per structure |
| v2.0.0 (Mar 23) | unknown | 0.237 V*cm | ALL at d=0.50, w=2.00 |
| v2.0-redo (Mar 24) | unknown | 0.169 V*cm | ALL ~0.16 (suspicious) |

The disagreement is likely due to different mesh sizes and possibly different
VpiL calculation paths (solver `v_pi_cm_V` key vs fallback formula).
This lean script logs which path is used so you can verify.

## Windows Notes

- Script handles cp1252 encoding automatically (no Unicode crash)
- Tested on Python 3.13 / Windows 11
- numpy and matplotlib required (standard Anaconda has both)

## If Something Goes Wrong

1. **Import error for solver**: Check `bto-sim/py/solvers/` path relative to where you run
2. **UnicodeEncodeError**: Should not happen (cp1252 fix included), but run with `PYTHONIOENCODING=utf-8` as fallback
3. **MemoryError**: Reduce `--workers` to 2. Each worker uses 1-4 GB at fine mesh.
4. **All VpiL = NaN**: Solver failed — check `step0_raw.json` for error messages

## What the Meeting Plots Show

- **sweetspot_vpil.png**: Which structure gives lowest VpiL (best modulation efficiency)
- **geometry_sensitivity**: How sensitive VpiL is to fabrication tolerances (d_BTO, w_rib)
- **1D sweeps**: Trade-offs — thicker BTO helps VpiL but may hurt loss
- **literature_comparison**: Are our numbers in the right ballpark vs published results?
- **neff_vs_loss**: Pareto front — which structures balance confinement and loss?
