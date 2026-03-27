#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared utility module for the 4-file BTO sandwich workflow.

Contents
--------
- Path helpers: sanitize_path, ensure_dir
- Tee wrapper: run_and_tee + CLI subcommand
- Report summary: summarize_report_dir + CLI subcommand
- Visualization from report JSON: visualize_best_from_report + CLI subcommand
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List


def sanitize_path(p: os.PathLike[str] | str) -> Path:
    s = str(p).strip().strip("'").strip('"')
    s = os.path.expandvars(os.path.expanduser(s))
    return Path(s).resolve()


def ensure_dir(p: os.PathLike[str] | str) -> Path:
    out = sanitize_path(p)
    out.mkdir(parents=True, exist_ok=True)
    return out


# ---------------------------------------------------------------------------
# tee helpers
# ---------------------------------------------------------------------------

def _clean_path(p: str) -> str:
    return p.strip().strip('"').strip("'")


def run_and_tee(log_path: str, cmd: List[str], cwd: str = "") -> int:
    log_path = _clean_path(log_path)
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)

    if cwd:
        os.chdir(cwd)

    with open(log_path, "a", encoding="utf-8", buffering=1) as f:
        try:
            p = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            msg = f"[tee] failed to start: {e}\n"
            sys.stdout.write(msg)
            f.write(msg)
            return 1

        assert p.stdout is not None
        for line in p.stdout:
            sys.stdout.write(line)
            f.write(line)
        p.wait()
        return p.returncode


# ---------------------------------------------------------------------------
# report summary helpers
# ---------------------------------------------------------------------------

def collect_reports(root: Path) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for p in sorted(root.glob("*.json")):
        try:
            with p.open("r", encoding="utf-8") as f:
                report = json.load(f)
            best = report.get("best_result") or {}
            cfg = report.get("config") or {}
            pt = best.get("point") or {}
            items.append({
                "name": p.stem,
                "structure_family": report.get("structure_family"),
                "top_core_material": cfg.get("top_core_material"),
                "spacer_material": cfg.get("spacer_material"),
                "elapsed_time_sec": report.get("elapsed_time_sec"),
                "score": best.get("score"),
                "loss_db_cm": best.get("loss_db_cm"),
                "delta_n": best.get("delta_n"),
                "delta_n_scaled": best.get("delta_n_scaled"),
                "gamma": best.get("gamma"),
                "selected_mode_neff_guess": best.get("selected_mode_neff_guess"),
                "te_fraction": best.get("te_fraction"),
                "top_conf_base": best.get("top_conf_base"),
                "al2o3_thickness_um": pt.get("al2o3_thickness_um"),
                "bto_thickness_um": pt.get("bto_thickness_um"),
                "spacer_thickness_um": pt.get("spacer_thickness_um"),
                "top_width_um": pt.get("top_width_um"),
                "top_height_um": pt.get("top_height_um"),
                "electrode_gap_um": pt.get("electrode_gap_um"),
                "json_path": str(p),
            })
        except Exception:
            continue
    return items


def write_summary(root: Path, items: List[Dict[str, Any]]) -> None:
    out_json = root / "summary_report.json"
    out_csv = root / "summary_report.csv"

    with out_json.open("w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)

    cols: List[str] = list(items[0].keys()) if items else []
    lines: List[str] = []
    if cols:
        lines.append(",".join(cols))
        for it in items:
            row: List[str] = []
            for c in cols:
                v = it.get(c, "")
                if isinstance(v, float):
                    row.append(f"{v:.6f}")
                else:
                    row.append(str(v))
            lines.append(",".join(row))
    out_csv.write_text("\n".join(lines), encoding="utf-8")


def summarize_report_dir(root: os.PathLike[str] | str) -> List[Dict[str, Any]]:
    root_path = sanitize_path(root)
    items = collect_reports(root_path)
    write_summary(root_path, items)
    return items


# ---------------------------------------------------------------------------
# visualization helpers
# ---------------------------------------------------------------------------

def save_plot_call(out_path: Path, fn, *args, **kwargs):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    orig = plt.show

    def _save():
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            plt.savefig(out_path, dpi=200)
        finally:
            plt.close()

    plt.show = _save
    try:
        return fn(*args, **kwargs)
    finally:
        plt.show = orig


def route_png(out_root: Path, stem: str, kind: str) -> Path:
    d = out_root / "picture_output" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{stem}.png"


def build_solver_from_report(report: Dict[str, Any]):
    from BTO_Sandwich_Flat import CombinedBTOFlatThinFilmSolver

    cfg = report.get("config") or {}
    best = report.get("best_result") or {}
    pt = best.get("point") or {}

    solver = CombinedBTOFlatThinFilmSolver(
        wavelength_um=cfg.get("wavelength_um", 1.55),
        dx=cfg.get("dx_um", 0.02),
        dy=cfg.get("dy_um", 0.02),
        verbose=False,
    )
    solver.orientation = cfg.get("orientation", "a-axis")
    solver.phi_deg = cfg.get("phi_deg", 45.0)
    solver.top_core_material = cfg.get("top_core_material", "sio2")
    solver.spacer_material = cfg.get("spacer_material", "air")

    solver.al2o3_thickness = pt.get("al2o3_thickness_um", solver.al2o3_thickness)
    solver.bto_thickness = pt.get("bto_thickness_um", solver.bto_thickness)
    solver.spacer_thickness = pt.get("spacer_thickness_um", solver.spacer_thickness)
    solver.sin_rib_width = pt.get("top_width_um", solver.sin_rib_width)
    solver.sin_rib_height = pt.get("top_height_um", solver.sin_rib_height)
    solver.electrode_gap = pt.get("electrode_gap_um", solver.electrode_gap)
    return solver, cfg, best


def visualize_best_from_report(
    json_path: os.PathLike[str] | str,
    out_dir: os.PathLike[str] | str | None = None,
    suffix: str = "",
    only_delta: bool = False,
    delta_kind: str = "",
    delta_raw: bool = False,
) -> Dict[str, Any]:
    jpath = sanitize_path(json_path)
    with jpath.open("r", encoding="utf-8") as f:
        report = json.load(f)

    solver, cfg, best = build_solver_from_report(report)
    out_root = sanitize_path(out_dir) if out_dir else jpath.parent
    ensure_dir(out_root)

    vapp = cfg.get("voltage_v", 3.0)
    n_modes = cfg.get("n_modes", 8)
    guess = best.get("selected_mode_neff_guess", 2.1) or 2.1
    selected_idx = int(best.get("selected_mode_index", -1) or -1)
    min_te_frac = cfg.get("min_te_fraction", 0.8)
    mode_ref = best.get("mode_ref") or {}
    ref_neff = float(mode_ref.get("n_eff")) if "n_eff" in mode_ref else None
    ref_conf = float(mode_ref.get("top_conf")) if "top_conf" in mode_ref else None

    def pick_by_ref(modes_list):
        if modes_list and ref_neff is not None and ref_conf is not None:
            best_i = None
            best_d = None
            for i, m in enumerate(modes_list):
                te = float(getattr(m, "te_fraction", 0.0))
                if te < float(min_te_frac):
                    continue
                ne = float(getattr(m, "n_eff", 0.0).real)
                tc = float(getattr(m, "confinement_factor", 0.0))
                d = abs(ne - ref_neff) + 0.5 * abs(tc - ref_conf)
                if best_d is None or d < best_d:
                    best_d = d
                    best_i = i
            if best_i is not None:
                return modes_list[best_i]
        return None

    modes = solver.analyze_all_modes(n_modes=n_modes, n_eff_guess=guess, voltage=0.0, show_plots=False)
    modes_eo = solver.analyze_all_modes(n_modes=n_modes, n_eff_guess=guess, voltage=vapp, show_plots=False)

    if modes:
        fund = pick_by_ref(modes)
        if fund is None and 0 <= selected_idx < len(modes):
            fund = modes[selected_idx]
        if fund is None:
            n2 = max(int(n_modes), int(selected_idx + 1 if selected_idx >= 0 else 0), 16)
            if n2 > n_modes:
                modes = solver.analyze_all_modes(n_modes=n2, n_eff_guess=guess, voltage=0.0, show_plots=False)
                fund = pick_by_ref(modes)
                if fund is None and 0 <= selected_idx < len(modes):
                    fund = modes[selected_idx]
        if fund is None:
            fund = modes[solver.find_fundamental_mode_index(modes)]
    else:
        fund = None

    if modes_eo:
        fund_eo = pick_by_ref(modes_eo)
        if fund_eo is None and 0 <= selected_idx < len(modes_eo):
            fund_eo = modes_eo[selected_idx]
        if fund_eo is None:
            n2 = max(int(n_modes), int(selected_idx + 1 if selected_idx >= 0 else 0), 16)
            if n2 > n_modes:
                modes_eo = solver.analyze_all_modes(n_modes=n2, n_eff_guess=guess, voltage=vapp, show_plots=False)
                fund_eo = pick_by_ref(modes_eo)
                if fund_eo is None and 0 <= selected_idx < len(modes_eo):
                    fund_eo = modes_eo[selected_idx]
        if fund_eo is None:
            fund_eo = modes_eo[solver.find_fundamental_mode_index(modes_eo)]
    else:
        fund_eo = None

    if not only_delta:
        if fund:
            try:
                print(f"[visualize] base selected: type={getattr(fund,'mode_type','')}, n_eff={float(getattr(fund,'n_eff',0.0).real):.6f}")
            except Exception:
                pass
            save_plot_call(route_png(out_root, jpath.stem + suffix, "base_mode_vectors"), solver.plot_mode_with_vectors, fund, "(base)", 4)
            save_plot_call(route_png(out_root, jpath.stem + suffix, "base_mode_intensity"), solver.plot_mode, fund, "(base)")
        if fund_eo:
            try:
                print(f"[visualize] eo selected: type={getattr(fund_eo,'mode_type','')}, n_eff={float(getattr(fund_eo,'n_eff',0.0).real):.6f}")
            except Exception:
                pass
            save_plot_call(route_png(out_root, jpath.stem + suffix, "eo_mode_vectors"), solver.plot_mode_with_vectors, fund_eo, "(eo)", 4)
            save_plot_call(route_png(out_root, jpath.stem + suffix, "eo_mode_intensity"), solver.plot_mode, fund_eo, "(eo)")
        save_plot_call(route_png(out_root, jpath.stem + suffix, "electrostatic"), solver.plot_electrostatic_field, vapp, True)
    if fund:
        kind_dir = delta_kind if delta_kind else "bto_index_delta"
        if delta_raw:
            save_plot_call(route_png(out_root, jpath.stem + suffix, kind_dir), solver.plot_bto_index_distribution_raw, vapp)
        else:
            save_plot_call(route_png(out_root, jpath.stem + suffix, kind_dir), solver.plot_bto_index_distribution, vapp, fund)

    metrics = solver.extract_key_metrics(voltage=vapp, n_modes=n_modes, n_eff_guess=guess)
    try:
        reff = solver.extract_numerical_r_eff(voltage=vapp, n_eff_guess=guess)
        metrics["r_eff"] = float(reff.get("r_eff")) if isinstance(reff, dict) else None
    except Exception:
        metrics["r_eff"] = None

    metrics_path = out_root / f"{jpath.stem}_metrics.json"
    with metrics_path.open("w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    return metrics


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Utility entrypoint for the 4-file BTO workflow.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_tee = sub.add_parser("tee", help="Run a command and tee stdout/stderr to console + log file.")
    p_tee.add_argument("--log", required=True)
    p_tee.add_argument("--cwd", default="")

    p_sum = sub.add_parser("summarize", help="Summarize all JSON reports in a directory.")
    p_sum.add_argument("--dir", required=True)

    p_vis = sub.add_parser("visualize", help="Render plots and metrics from one report JSON.")
    p_vis.add_argument("--json", required=True)
    p_vis.add_argument("--out", default="")
    p_vis.add_argument("--suffix", default="")
    p_vis.add_argument("--only-delta-n", action="store_true")
    p_vis.add_argument("--delta-dir", default="")
    p_vis.add_argument("--delta-raw", action="store_true")

    args, unknown = parser.parse_known_args()

    if args.cmd == "tee":
        if "--" in sys.argv:
            sep = sys.argv.index("--")
            cmd = sys.argv[sep + 1 :]
        else:
            cmd = unknown
        if not cmd:
            sys.stderr.write("[tee] no command provided\n")
            sys.exit(2)
        sys.exit(run_and_tee(args.log, cmd, cwd=args.cwd))

    if args.cmd == "summarize":
        items = summarize_report_dir(args.dir)
        print(f"Wrote summary for {len(items)} report(s) in {sanitize_path(args.dir)}")
        return

    if args.cmd == "visualize":
        metrics = visualize_best_from_report(
            args.json,
            args.out or None,
            args.suffix,
            args.only_delta_n,
            args.delta_dir,
            args.delta_raw,
        )
        print(json.dumps(metrics, indent=2))
        return


if __name__ == "__main__":
    main()
