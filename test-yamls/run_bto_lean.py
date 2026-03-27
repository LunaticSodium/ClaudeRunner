#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_bto_lean.py — Lean BTO structure characterization
Skips brute-force all-scan. Does:
  Step 0: 5x5 grid at COARSE mesh (0.02) to find sweetspots  (~13 min)
  Step 1: 1D sweeps from sweetspot at COARSE mesh             (~45 min)
  Step 2: Literature comparison (analytical, instant)
  Step 3: Generate all 11+ plots

Total runtime: ~1 hour with 4 workers.

Usage:
  python run_bto_lean.py                    # mesh=0.02 (default, ~1h)
  python run_bto_lean.py --fine             # mesh=0.01 (~10h, publication)
  python run_bto_lean.py --mesh 0.015       # custom mesh

Requirements:
  - bto-sim/ solver package in working directory (or parent)
  - numpy, matplotlib
"""
import sys
import os
import json
import math
import time
import argparse
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import shutil

# ── Windows cp1252 safety ─────────────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ── CLI ───────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description='BTO structure characterization')
parser.add_argument('--fine', action='store_true', help='Use fine mesh 0.01 (slow)')
parser.add_argument('--mesh', type=float, default=None, help='Custom mesh size')
parser.add_argument('--workers', type=int, default=4, help='Parallel workers')
parser.add_argument('--outdir', default='output/plots', help='Output directory')
args = parser.parse_args()

MESH = args.mesh or (0.01 if args.fine else 0.02)
MAX_WORKERS = args.workers
OUT_DIR = args.outdir
LOG_PATH = os.path.join(OUT_DIR, 'run_bto_lean.log')

# ── Structures ────────────────────────────────────────────────────────────────
STRUCTURES = [
    {"id": "sin_patch",    "top_core": "sin",   "spacer": "air",   "spacer_thick": 0.0,  "r42_pm_V": 923},
    {"id": "sio2_patch",   "top_core": "sio2",  "spacer": "air",   "spacer_thick": 0.0,  "r42_pm_V": 923},
    {"id": "al2o3_patch",  "top_core": "al2o3", "spacer": "air",   "spacer_thick": 0.0,  "r42_pm_V": 923},
    {"id": "sin_sio2",     "top_core": "sio2",  "spacer": "sin",   "spacer_thick": 0.05, "r42_pm_V": 923},
    {"id": "sin_al2o3",    "top_core": "al2o3", "spacer": "sin",   "spacer_thick": 0.05, "r42_pm_V": 923},
    {"id": "sio2_al2o3",   "top_core": "al2o3", "spacer": "sio2",  "spacer_thick": 0.05, "r42_pm_V": 923},
    {"id": "al2o3_sio2",   "top_core": "sio2",  "spacer": "al2o3", "spacer_thick": 0.05, "r42_pm_V": 923},
    {"id": "sin_patch_hq", "top_core": "sin",   "spacer": "air",   "spacer_thick": 0.0,  "r42_pm_V": 1340},
]

# MANDATORY grid — do NOT reduce
D_BTO_VALS = [0.05, 0.15, 0.25, 0.35, 0.50]
W_RIB_VALS = [0.5, 0.75, 1.0, 1.5, 2.0]

ELECTRODE_GAP = 4.4   # um
VOLTAGE       = 5.0   # V

COLORS = plt.cm.tab10(np.linspace(0, 1, len(STRUCTURES)))

_log_fh = None
_t0 = None

def log(msg):
    elapsed = f"[{time.time() - _t0:7.1f}s]" if _t0 else ""
    line = f"{elapsed} {msg}"
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + '\n')
        _log_fh.flush()


# ── Metric helpers ────────────────────────────────────────────────────────────
def calc_vpil(metrics, r42_pm_V, electrode_gap_um=ELECTRODE_GAP):
    if not metrics or not metrics.get('success', False):
        return float('nan')
    if 'v_pi_cm_V' in metrics:
        return float(metrics['v_pi_cm_V'])
    n_eff = metrics.get('n_eff_base', 0.0)
    if not n_eff or n_eff <= 0:
        return float('nan')
    return (100.0 * 1.55 * electrode_gap_um) / (n_eff**3 * r42_pm_V * 0.35)

def get_neff(m):
    return float(m.get('n_eff_base', float('nan'))) if m and m.get('success') else float('nan')

def get_loss(m):
    return float(m.get('loss_base_db_cm', float('nan'))) if m and m.get('success') else float('nan')

def get_delta_neff(m):
    return float(m.get('delta_n', float('nan'))) if m and m.get('success') else float('nan')

def _label(struct):
    return f"{struct['id']} (r42={struct['r42_pm_V']})"


# ── Solver worker (module-level for spawn pickling) ───────────────────────────
def _find_solver_path():
    """Find bto-sim/py/solvers relative to cwd or parent dirs."""
    for base in ['.', '..', '../..']:
        p = os.path.join(base, 'bto-sim', 'py', 'solvers')
        if os.path.isdir(p):
            return p
    return 'bto-sim/py/solvers'  # fallback

_SOLVER_PATH = _find_solver_path()

def _run_solver(args_tuple):
    (top_core, spacer_mat, spacer_thick, d_bto, w_rib,
     electrode_gap, voltage, phi_deg, r42_SI, mesh_res) = args_tuple
    try:
        sys.path.insert(0, _SOLVER_PATH)
        from BTO_Sandwich_Flat import CombinedBTOFlatThinFilmSolver

        solver = CombinedBTOFlatThinFilmSolver(verbose=False)
        solver.dx = mesh_res
        solver.dy = mesh_res
        solver.top_core_material = top_core
        solver.spacer_material   = spacer_mat
        solver.spacer_thickness  = spacer_thick
        solver.bto_thickness     = d_bto
        solver.sin_rib_width     = w_rib
        solver.electrode_gap     = electrode_gap
        solver.r42               = r42_SI
        solver.r51               = r42_SI
        solver.orientation       = 'a-axis'
        solver.phi_deg           = phi_deg
        metrics = solver.extract_key_metrics(voltage=voltage)
        return metrics
    except Exception as e:
        return {"success": False, "reason": str(e), "traceback": traceback.format_exc()}


def _submit_pool(tasks, label=""):
    results = [None] * len(tasks)
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_map = {pool.submit(_run_solver, t): i for i, t in enumerate(tasks)}
        done = 0
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                results[idx] = {"success": False, "reason": str(exc)}
            done += 1
            if done % max(1, len(tasks)//5) == 0 or done == len(tasks):
                log(f"    {label} {done}/{len(tasks)} done ({time.time()-t0:.0f}s)")
    return results


# ── Step 0: Sweetspot via 5x5 grid ───────────────────────────────────────────
def run_step0():
    log(f"=== Step 0: 5x5 sweetspot grid (mesh={MESH}) ===")

    tasks, keys = [], []
    for struct in STRUCTURES:
        r42_SI = struct['r42_pm_V'] * 1e-12
        for d in D_BTO_VALS:
            for w in W_RIB_VALS:
                tasks.append((
                    struct['top_core'], struct['spacer'], struct['spacer_thick'],
                    d, w, ELECTRODE_GAP, VOLTAGE, 0.0, r42_SI, MESH
                ))
                keys.append((struct['id'], d, w))

    log(f"  {len(tasks)} tasks, {MAX_WORKERS} workers")
    results_list = _submit_pool(tasks, "grid")
    results_map = {keys[i]: results_list[i] for i in range(len(tasks))}

    sweetspots = {}
    for struct in STRUCTURES:
        sid = struct['id']
        best_vpil, best_d, best_w, best_m = float('inf'), D_BTO_VALS[2], W_RIB_VALS[2], None
        for d in D_BTO_VALS:
            for w in W_RIB_VALS:
                m = results_map.get((sid, d, w), {"success": False})
                v = calc_vpil(m, struct['r42_pm_V'])
                if math.isfinite(v) and v < best_vpil:
                    best_vpil, best_d, best_w, best_m = v, d, w, m

        sweetspots[sid] = {
            "d_bto_um": best_d, "w_rib_um": best_w,
            "vpil": best_vpil if math.isfinite(best_vpil) else None,
            "metrics": best_m or {"success": False},
            "r42_pm_V": struct['r42_pm_V'],
        }

    # Save sweetspot JSON
    json_out = {"mesh_um": MESH}
    for sid, ss in sweetspots.items():
        grid = {}
        for d in D_BTO_VALS:
            for w in W_RIB_VALS:
                m = results_map.get((sid, d, w), {"success": False})
                grid[f"{d}_{w}"] = {
                    "vpil": calc_vpil(m, ss['r42_pm_V']),
                    "n_eff": get_neff(m), "loss": get_loss(m),
                }
        json_out[sid] = {
            "d_bto_um": ss["d_bto_um"], "w_rib_um": ss["w_rib_um"],
            "vpil_V_cm": ss["vpil"], "r42_pm_V": ss["r42_pm_V"],
            "grid": grid,
        }
    with open(os.path.join(OUT_DIR, 'sweetspot_results.json'), 'w', encoding='utf-8') as f:
        json.dump(json_out, f, indent=2)

    log("\nSweet-spot table:")
    log(f"  {'ID':<20} {'d_BTO':<8} {'w_rib':<8} {'VpiL V*cm':<14} r42")
    for struct in STRUCTURES:
        sid = struct['id']
        ss = sweetspots[sid]
        v = f"{ss['vpil']:.4f}" if ss['vpil'] else "FAIL"
        log(f"  {sid:<20} {ss['d_bto_um']:<8.3f} {ss['w_rib_um']:<8.3f} {v:<14} {ss['r42_pm_V']}")

    return sweetspots, results_map


# ── Step 1: 1D sweeps from sweetspot ─────────────────────────────────────────
def run_step1(sweetspots):
    log(f"\n=== Step 1: 1D sweeps (mesh={MESH}) ===")

    d_arr   = np.linspace(0.05, 0.50, 20).tolist()
    w_arr   = np.linspace(0.5,  2.0,  20).tolist()
    gap_arr = np.linspace(2.5, 10.0,  16).tolist()
    phi_arr = np.linspace(0,   90,    19).tolist()
    v_arr   = np.linspace(1,   15,    15).tolist()

    sweep_results = {}
    for struct in STRUCTURES:
        sid = struct['id']
        d_star = sweetspots[sid]['d_bto_um']
        w_star = sweetspots[sid]['w_rib_um']
        r42_SI = struct['r42_pm_V'] * 1e-12
        log(f"  {sid}: d*={d_star}, w*={w_star}")

        sweep_data = {}
        for pname, pvals in [('d_bto', d_arr), ('w_rib', w_arr), ('gap', gap_arr),
                              ('phi', phi_arr), ('voltage', v_arr)]:
            tasks = []
            for val in pvals:
                d, w, gap, V, phi = d_star, w_star, ELECTRODE_GAP, VOLTAGE, 0.0
                r42 = r42_SI
                if pname == 'd_bto':    d = val
                elif pname == 'w_rib':  w = val
                elif pname == 'gap':    gap = val
                elif pname == 'phi':
                    phi = val
                    r42 = struct['r42_pm_V'] * math.cos(math.radians(val))**2 * 1e-12
                elif pname == 'voltage': V = val
                tasks.append((struct['top_core'], struct['spacer'], struct['spacer_thick'],
                              d, w, gap, V, phi, r42, MESH))

            res = _submit_pool(tasks, f"{sid}/{pname}")
            sweep_data[pname] = {'vals': pvals, 'results': res}

        sweep_results[sid] = sweep_data
    return sweep_results


# ── Step 2: Literature comparison ─────────────────────────────────────────────
def run_step2(sweetspots):
    log("\n=== Step 2: Literature comparison ===")
    VpiL_LNOI = (100.0 * 1.55 * 4.4) / (2.14**3 * 27.0  * 0.35)
    VpiL_SOH  = (100.0 * 1.55 * 4.4) / (1.65**3 * 150.0 * 0.45)

    BTO_LIT = {
        'sin_patch':    {'ref': 'Eltes APL 2019',       'vmin': 0.05, 'vmax': 0.50},
        'sin_patch_hq': {'ref': 'Eltes Nat.Mat. 2020',  'vmin': 0.03, 'vmax': 0.30},
    }

    lit_out = []
    for struct in STRUCTURES:
        sid = struct['id']
        vpil = sweetspots[sid]['vpil']
        entry = {"structure": sid, "vpil_V_cm": vpil, "r42_pm_V": struct['r42_pm_V']}
        if sid in BTO_LIT:
            lref = BTO_LIT[sid]
            entry["reference"]  = lref['ref']
            entry["target_min"] = lref['vmin']
            entry["target_max"] = lref['vmax']
            if vpil and math.isfinite(vpil):
                entry["within_order"] = (lref['vmin']/10 <= vpil <= lref['vmax']*10)
            else:
                entry["within_order"] = False
        lit_out.append(entry)

    lit_out.append({"structure": "LNOI", "reference": "Wang Nat.Photon. 2018",
                    "vpil_analytical_V_cm": VpiL_LNOI, "within_order": True, "note": "analytical"})
    lit_out.append({"structure": "SOH", "reference": "Lauermann Optica 2016",
                    "vpil_analytical_V_cm": VpiL_SOH, "within_order": True, "note": "analytical"})

    with open(os.path.join(OUT_DIR, 'literature_comparison.json'), 'w', encoding='utf-8') as f:
        json.dump(lit_out, f, indent=2)

    log(f"  LNOI analytical VpiL = {VpiL_LNOI:.3f} V*cm")
    log(f"  SOH  analytical VpiL = {VpiL_SOH:.3f} V*cm")
    return lit_out, VpiL_LNOI, VpiL_SOH


# ── Step 3: Plots ─────────────────────────────────────────────────────────────
def _savefig(fname):
    path = os.path.join(OUT_DIR, fname)
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()
    log(f"  saved {fname}")

def _sweep_series(sweep_results, param_key, y_fn):
    series = []
    for struct in STRUCTURES:
        sw = sweep_results.get(struct['id'], {}).get(param_key)
        if not sw:
            series.append(([], []))
            continue
        series.append((sw['vals'], [y_fn(struct, m, x) for m, x in zip(sw['results'], sw['vals'])]))
    return series

def _plot_1d(series, xlabel, ylabel, title, fname, log_y=False):
    fig, ax = plt.subplots(figsize=(10, 6))
    for i, (struct, (xs, ys)) in enumerate(zip(STRUCTURES, series)):
        ax.plot(xs, ys, '-o', color=COLORS[i], label=_label(struct), markersize=3, linewidth=1.5)
    if log_y: ax.set_yscale('log')
    ax.set_xlabel(xlabel); ax.set_ylabel(ylabel); ax.set_title(title)
    ax.legend(fontsize=7); ax.grid(True, which='both' if log_y else 'major', alpha=0.3)
    _savefig(fname)

def run_step3(sweetspots, grid_map, sweep_results, lit_out, VpiL_LNOI, VpiL_SOH):
    log("\n=== Step 3: Generating plots ===")

    # 1 — sweetspot bar chart
    fig, ax = plt.subplots(figsize=(10, 6))
    ids = [s['id'] for s in STRUCTURES]
    vpils = [sweetspots[sid]['vpil'] or 1e-6 for sid in ids]
    bars = ax.bar(ids, vpils, color=COLORS)
    ax.set_yscale('log'); ax.set_xlabel('Structure'); ax.set_ylabel('VpiL (V*cm)')
    ax.set_title('Sweet-spot VpiL per structure')
    ax.set_xticklabels(ids, rotation=30, ha='right'); ax.grid(True, which='both', alpha=0.3)
    for bar, v in zip(bars, vpils):
        ax.text(bar.get_x()+bar.get_width()/2, v*1.1, f'{v:.3f}', ha='center', va='bottom', fontsize=7)
    _savefig('sweetspot_vpil.png')

    # 2 — literature comparison
    BTO_LIT = {
        'sin_patch':    {'ref': 'Eltes APL 2019',      'vmin': 0.05, 'vmax': 0.50},
        'sin_patch_hq': {'ref': 'Eltes Nat.Mat. 2020', 'vmin': 0.03, 'vmax': 0.30},
    }
    fig, ax = plt.subplots(figsize=(10, 6))
    for i, (struct, v) in enumerate(zip(STRUCTURES, vpils)):
        if math.isfinite(v):
            ax.scatter([i], [v], s=80, color=COLORS[i], zorder=5, label=_label(struct))
    for sid, lr in BTO_LIT.items():
        ax.axhspan(lr['vmin'], lr['vmax'], alpha=0.08, color='gray',
                   label=f"{lr['ref']} [{lr['vmin']}-{lr['vmax']}]")
    ax.axhline(VpiL_LNOI, color='blue', ls='--', lw=1.5, label=f'LNOI ({VpiL_LNOI:.2f})')
    ax.axhline(VpiL_SOH,  color='green', ls='--', lw=1.5, label=f'SOH ({VpiL_SOH:.3f})')
    ax.set_yscale('log'); ax.set_xticks(range(len(ids))); ax.set_xticklabels(ids, rotation=30, ha='right')
    ax.set_ylabel('VpiL (V*cm)'); ax.set_title('Simulation vs Literature VpiL')
    ax.legend(fontsize=7, loc='upper right'); ax.grid(True, which='both', alpha=0.3)
    _savefig('literature_comparison.png')

    # 3-8 — 1D sweep plots
    _plot_1d(_sweep_series(sweep_results, 'd_bto', lambda s,m,x: calc_vpil(m, s['r42_pm_V'])),
             'BTO thickness (um)', 'VpiL (V*cm)', 'VpiL vs BTO Thickness', 'vpiL_vs_bto_thickness.png', True)
    _plot_1d(_sweep_series(sweep_results, 'd_bto', lambda s,m,x: get_loss(m)),
             'BTO thickness (um)', 'Loss (dB/cm)', 'Loss vs BTO Thickness', 'loss_vs_bto_thickness.png')
    _plot_1d(_sweep_series(sweep_results, 'w_rib', lambda s,m,x: calc_vpil(m, s['r42_pm_V'])),
             'WG width (um)', 'VpiL (V*cm)', 'VpiL vs WG Width', 'vpiL_vs_wg_width.png', True)
    _plot_1d(_sweep_series(sweep_results, 'w_rib', lambda s,m,x: get_loss(m)),
             'WG width (um)', 'Loss (dB/cm)', 'Loss vs WG Width', 'loss_vs_wg_width.png')
    _plot_1d(_sweep_series(sweep_results, 'gap', lambda s,m,x: calc_vpil(m, s['r42_pm_V'], x)),
             'Electrode gap (um)', 'VpiL (V*cm)', 'VpiL vs Electrode Gap', 'vpiL_vs_electrode_gap.png', True)

    def _vpil_phi(s, m, phi):
        r42_eff = s['r42_pm_V'] * math.cos(math.radians(phi))**2
        return calc_vpil(m, r42_eff) if r42_eff > 1e-6 else float('nan')
    _plot_1d(_sweep_series(sweep_results, 'phi', _vpil_phi),
             'Crystal angle phi (deg)', 'VpiL (V*cm)', 'VpiL vs Crystal Angle', 'vpiL_vs_crystal_angle.png', True)

    # 9 — delta_neff vs voltage
    _plot_1d(_sweep_series(sweep_results, 'voltage', lambda s,m,x: abs(get_delta_neff(m))),
             'Voltage (V)', '|delta n_eff|', 'Delta n_eff vs Voltage', 'delta_neff_vs_voltage.png')

    # 10 — neff vs loss scatter
    fig, ax = plt.subplots(figsize=(10, 6))
    ns, ls, vs, lbls = [], [], [], []
    for struct in STRUCTURES:
        m = sweetspots[struct['id']]['metrics']
        n, l, v = get_neff(m), get_loss(m), sweetspots[struct['id']]['vpil'] or float('nan')
        if all(math.isfinite(x) for x in (n, l, v)):
            ns.append(n); ls.append(l); vs.append(v); lbls.append(struct['id'])
    if ns:
        sc = ax.scatter(ns, ls, c=vs, cmap='viridis', s=120, zorder=5,
                        norm=mcolors.LogNorm(vmin=min(vs), vmax=max(vs)))
        plt.colorbar(sc, ax=ax, label='VpiL (V*cm)')
        for x, y, lbl in zip(ns, ls, lbls):
            ax.annotate(lbl, (x,y), fontsize=7, ha='left', va='bottom')
    ax.set_xlabel('n_eff'); ax.set_ylabel('Loss (dB/cm)'); ax.set_title('n_eff vs Loss (colour=VpiL)')
    ax.grid(True, alpha=0.3); _savefig('neff_vs_loss.png')

    # 11+ — geometry heatmaps per structure
    for struct in STRUCTURES:
        sid = struct['id']
        Z = np.full((len(D_BTO_VALS), len(W_RIB_VALS)), float('nan'))
        for i, d in enumerate(D_BTO_VALS):
            for j, w in enumerate(W_RIB_VALS):
                m = grid_map.get((sid, d, w), {"success": False})
                Z[i,j] = calc_vpil(m, struct['r42_pm_V'])
        fig, ax = plt.subplots(figsize=(10, 6))
        W_g, D_g = np.meshgrid(W_RIB_VALS, D_BTO_VALS)
        valid = Z[np.isfinite(Z)]
        if valid.size > 0:
            im = ax.pcolormesh(W_g, D_g, Z, norm=mcolors.LogNorm(vmin=valid.min(), vmax=valid.max()),
                               cmap='viridis_r', shading='auto')
            plt.colorbar(im, ax=ax, label='VpiL (V*cm)')
        ss = sweetspots[sid]
        ax.scatter([ss['w_rib_um']], [ss['d_bto_um']], s=200, marker='*', color='red', zorder=5, label='sweet-spot')
        ax.set_xlabel('WG width (um)'); ax.set_ylabel('BTO thickness (um)')
        ax.set_title(f'Geometry sensitivity - {sid}'); ax.legend(); ax.grid(True, alpha=0.3)
        _savefig(f'geometry_sensitivity_{sid}.png')

    src = os.path.join(OUT_DIR, 'geometry_sensitivity_sin_patch.png')
    dst = os.path.join(OUT_DIR, 'geometry_sensitivity.png')
    if os.path.exists(src): shutil.copy(src, dst); log("  geometry_sensitivity.png (copy of sin_patch)")


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    os.makedirs(OUT_DIR, exist_ok=True)
    _log_fh = open(LOG_PATH, 'w', encoding='utf-8')
    _t0 = time.time()

    log(f"BTO Structure Characterization (lean)")
    log(f"Python {sys.version}")
    log(f"Mesh: {MESH} um | Workers: {MAX_WORKERS} | Output: {OUT_DIR}")
    log(f"Grid: {len(D_BTO_VALS)}x{len(W_RIB_VALS)} = {len(D_BTO_VALS)*len(W_RIB_VALS)} pts/structure")
    log(f"Structures: {len(STRUCTURES)}")
    log(f"Total Step 0 solves: {len(STRUCTURES)*len(D_BTO_VALS)*len(W_RIB_VALS)}")
    log("")

    sweetspots, grid_map = run_step0()

    # Save raw step0
    s0 = {f"{k[0]}|{k[1]}|{k[2]}": v for k,v in grid_map.items()}
    with open(os.path.join(OUT_DIR, 'step0_raw.json'), 'w', encoding='utf-8') as f:
        json.dump(s0, f, indent=2, default=str)

    sweep_results = run_step1(sweetspots)

    # Save raw step1
    s1 = {sid: {p: {'vals': d['vals'], 'results': d['results']} for p,d in sw.items()}
           for sid, sw in sweep_results.items()}
    with open(os.path.join(OUT_DIR, 'step1_raw.json'), 'w', encoding='utf-8') as f:
        json.dump(s1, f, indent=2, default=str)

    lit_out, VpiL_LNOI, VpiL_SOH = run_step2(sweetspots)
    run_step3(sweetspots, grid_map, sweep_results, lit_out, VpiL_LNOI, VpiL_SOH)

    elapsed = time.time() - _t0
    log(f"\nDone in {elapsed/60:.1f} min ({elapsed/3600:.2f} h)")
    _log_fh.close()
    print(f"\n{'='*60}")
    print(f"COMPLETE - {len(os.listdir(OUT_DIR))} files in {OUT_DIR}/")
    print(f"Elapsed: {elapsed/60:.1f} min")
    print(f"{'='*60}")
