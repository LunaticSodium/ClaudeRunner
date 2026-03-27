#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Sandwich_Autosweeper.py

Time-budgeted multi-start local optimizer for the flat-BTO thin-film solver.

Current intent
--------------
- Keep all EM / EO physics inside BTO_Sandwich_Flat.py.
- Keep the optimizer fully external and reusable.
- Stay TE-only for the current stage.
- Use branch-aware mode tracking across nearby geometry points.
- Support explicit structure families including pure patch and sandwich variants:
    * "sio2_patch" / "patch_sio2"
    * "patch_al2o3"
    * "patch_sin"
    * "sio2_air_bto"
    * "al2o3_sio2_bto" / "sandwich_al2o3_sio2"
    * "sandwich_sio2_al2o3"
    * "custom"

Notes
-----
1) The optimizer objective is now modulation-oriented rather than pure loss-only:
   score ~= loss / (|delta_n| / delta_n_norm), plus any optional soft penalty.
2) This is still a geometry screening tool, not yet a full device-level VpiL*alpha engine.
3) Only a reduced subset of geometry variables is optimized per structure family.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from util import ensure_dir, sanitize_path
from BTO_Sandwich_Flat import CombinedBTOFlatThinFilmSolver


# ============================================================================
# Data models
# ============================================================================

@dataclass(frozen=True)
class CandidatePoint:
    """Full geometry payload used by the optimizer and by JSON output."""
    al2o3_thickness_um: float
    bto_thickness_um: float
    h_total_um: float
    f_spacer: float
    spacer_thickness_um: float
    top_width_um: float
    top_height_um: float
    electrode_gap_um: float


@dataclass(frozen=True)
class ModeReference:
    """Lightweight branch signature used for local TE-branch tracking."""
    n_eff: float
    top_conf: float
    te_fraction: float


@dataclass
class EvaluationResult:
    """Single evaluated point result."""
    success: bool
    message: str
    point: CandidatePoint
    score: float

    # Optional soft-penalty bookkeeping
    height_penalty_factor: float = 0.0
    height_penalty_value: float = 0.0

    # Objective bookkeeping
    delta_n_scaled: float = 0.0
    vpi_l_cm: float = 0.0
    alpha_max_db_cm: float = 0.0
    penalty_loss: float = 0.0

    # Selected mode summary
    selected_mode_index: int = -1
    selected_mode_neff_guess: float = 0.0
    mode_type_base: str = ""
    mode_type_eo: str = ""

    # Physical metrics
    loss_db_cm: float = math.inf
    loss_eo_db_cm: float = math.inf
    te_fraction: float = 0.0
    tm_fraction: float = 0.0
    n_eff_base: float = 0.0
    n_eff_eo: float = 0.0
    delta_n: float = 0.0
    gamma: float = 0.0
    top_conf_base: float = 0.0
    top_conf_eo: float = 0.0

    # Branch tracking signature
    mode_ref: Optional[ModeReference] = None

    # Bookkeeping
    evaluation_time_sec: float = 0.0

    def is_better_than(self, other: "EvaluationResult") -> bool:
        if not self.success:
            return False
        if not other.success:
            return True
        return self.score < other.score


@dataclass
class OptimizerConfig:
    """Static solver settings + search settings."""
    # Structure family selector
    structure_family: str = "sio2_patch"

    # Static solver settings
    wavelength_um: float = 1.55
    dx_um: float = 0.02
    dy_um: float = 0.02
    voltage_v: float = 3.0
    orientation: str = "a-axis"
    phi_deg: float = 45.0

    # Static materials (may be overwritten by structure family)
    top_core_material: str = "sio2"
    spacer_material: str = "air"

    # Mode solve settings
    n_modes: int = 8
    n_eff_guesses: Tuple[float, ...] = (1.7, 1.9, 2.1)
    min_te_fraction: float = 0.85
    delta_n_min: float = 1.0e-7
    loss_soft_db_cm: float = 0.03
    loss_hard_db_cm: float = 0.20

    # Runtime
    time_limit_sec: float = 60.0
    top_k: int = 3
    random_seed: Optional[int] = None

    # Whether electrode gap is an active optimization variable.
    # If False, gap stays fixed at fixed_point.electrode_gap_um / --fixed-gap.
    opt_gap: bool = False

    # --- Live monitoring / progress printing ---
    print_each_eval: bool = False          # print every evaluated point
    progress_every_sec: float = 60.0       # periodic progress print (seconds); 0 disables
    progress_every_evals: int = 0          # periodic progress print (by eval count); 0 disables

    # Family-dependent search subset and baseline point
    active_fields: Tuple[str, ...] = ("h_total_um", "f_spacer", "top_width_um", "electrode_gap_um")
    fixed_point: CandidatePoint = field(default_factory=lambda: CandidatePoint(
        al2o3_thickness_um=0.020,
        bto_thickness_um=0.160,
        h_total_um=0.500,
        f_spacer=0.20,
        spacer_thickness_um=0.100,
        top_width_um=1.000,
        top_height_um=0.400,
        electrode_gap_um=4.400,
    ))

    # Step sizes
    step_sizes: Dict[str, float] = field(default_factory=lambda: {
        "al2o3_thickness_um": 0.002,
        "bto_thickness_um": 0.02,
        "h_total_um": 0.02,
        "f_spacer": 0.05,
        "top_width_um": 0.10,
        "electrode_gap_um": 0.20,
    })

    # Broad but modulation-oriented engineering bounds (family-specific presets tighten these)
    bounds: Dict[str, Tuple[float, float]] = field(default_factory=lambda: {
        "al2o3_thickness_um": (0.002, 0.050),
        "bto_thickness_um": (0.080, 0.350),
        "h_total_um": (0.400, 0.600),
        "f_spacer": (0.0, 1.0),
        "top_width_um": (0.500, 2.500),
        "electrode_gap_um": (3.0, 8.0),
    })

    # Distinct-basin thresholds
    dedup_thresholds: Dict[str, float] = field(default_factory=lambda: {
        "al2o3_thickness_um": 0.002,
        "bto_thickness_um": 0.02,
        "h_total_um": 0.02,
        "f_spacer": 0.05,
        "top_width_um": 0.10,
        "electrode_gap_um": 0.20,
    })


# ============================================================================
# Helpers
# ============================================================================

class TopKLocalOptimaStore:
    """Keep the best K distinct basins."""

    def __init__(self, top_k: int, dedup_thresholds: Dict[str, float]) -> None:
        self.top_k = int(top_k)
        self.dedup_thresholds = dict(dedup_thresholds)
        self._results: List[EvaluationResult] = []

    def _same_basin(self, a: CandidatePoint, b: CandidatePoint) -> bool:
        for field_name, thr in self.dedup_thresholds.items():
            if abs(getattr(a, field_name) - getattr(b, field_name)) > thr:
                return False
        return True

    def try_add(self, result: EvaluationResult) -> None:
        if not result.success:
            return

        for i, old in enumerate(self._results):
            if self._same_basin(result.point, old.point):
                if result.is_better_than(old):
                    self._results[i] = result
                    self._sort_trim()
                return

        self._results.append(result)
        self._sort_trim()

    def _sort_trim(self) -> None:
        self._results.sort(key=lambda r: r.score)
        if len(self._results) > self.top_k:
            self._results = self._results[:self.top_k]

    def results(self) -> List[EvaluationResult]:
        return list(self._results)

    def best(self) -> Optional[EvaluationResult]:
        return self._results[0] if self._results else None


class GreedyStepper:
    """Choose the best strictly improving neighbor."""

    def __init__(self, improve_epsilon: float = 1e-12) -> None:
        self.improve_epsilon = float(improve_epsilon)

    def choose_next(
        self,
        current: EvaluationResult,
        neighbors: List[EvaluationResult],
    ) -> Optional[EvaluationResult]:
        better: List[EvaluationResult] = []
        for r in neighbors:
            if not r.success:
                continue
            if not current.success:
                better.append(r)
            elif r.score < (current.score - self.improve_epsilon):
                better.append(r)

        if not better:
            return None

        better.sort(key=lambda r: r.score)
        return better[0]


# ============================================================================
# Main optimizer
# ============================================================================

class SandwichAutosweeper:
    """Time-budgeted TE-only branch-tracking optimizer."""

    VALID_FAMILIES = (
        "sio2_patch",
        "sio2_air_bto",
        "al2o3_sio2_bto",
        "patch_sio2",
        "patch_al2o3",
        "patch_sin",
        "sandwich_al2o3_sio2",
        "sandwich_sio2_al2o3",
        "custom",
    )

    def __init__(self, config: OptimizerConfig) -> None:
        self.config = config
        self._apply_structure_family_preset()
        self.rng = random.Random(config.random_seed)

        self.store = TopKLocalOptimaStore(
            top_k=config.top_k,
            dedup_thresholds=config.dedup_thresholds,
        )
        self.stepper = GreedyStepper()

        self.evaluations_total = 0
        self.successful_evaluations = 0
        self.local_optima_found = 0
        self.boundary_stopped = 0
        self._deadline_monotonic = 0.0

        self._last_progress_print_t = 0.0
        self._last_progress_print_eval = 0

    # ------------------------------------------------------------------
    # Structure family preset
    # ------------------------------------------------------------------
    def _apply_structure_family_preset(self) -> None:
        """
        Apply structure-family-specific presets.

        Families:
        - sio2_patch
        - sio2_air_bto
        - al2o3_sio2_bto
        - patch_sio2 / patch_al2o3 / patch_sin
        - sandwich_al2o3_sio2 / sandwich_sio2_al2o3
        - custom

        In custom mode:
        - keep top_core_material from config
        - keep spacer_material from config
        - choose active_fields automatically from current fixed_point / spacer thickness
        """
        def with_optional_gap(*fields: str) -> Tuple[str, ...]:
            out = list(fields)
            if self.config.opt_gap:
                out.append("electrode_gap_um")
            return tuple(out)

        fam = self.config.structure_family.strip().lower()

        # Work on a mutable dict, then write back to CandidatePoint
        base = asdict(self.config.fixed_point)

        if fam in ("sio2_patch", "patch_sio2"):
            self.config.top_core_material = "sio2"
            self.config.spacer_material = "air"
            base["f_spacer"] = 0.0
            self.config.active_fields = with_optional_gap("h_total_um", "top_width_um")

        elif fam == "patch_al2o3":
            self.config.top_core_material = "al2o3"
            self.config.spacer_material = "air"
            base["f_spacer"] = 0.0
            self.config.active_fields = with_optional_gap("h_total_um", "top_width_um")

        elif fam == "patch_sin":
            self.config.top_core_material = "sin"
            self.config.spacer_material = "air"
            base["f_spacer"] = 0.0
            self.config.active_fields = with_optional_gap("h_total_um", "top_width_um")

        elif fam == "sio2_air_bto":
            self.config.top_core_material = "sio2"
            self.config.spacer_material = "air"
            self.config.active_fields = with_optional_gap("h_total_um", "f_spacer", "top_width_um")

        elif fam in ("al2o3_sio2_bto", "sandwich_al2o3_sio2"):
            self.config.top_core_material = "al2o3"
            self.config.spacer_material = "sio2"
            self.config.active_fields = with_optional_gap("h_total_um", "f_spacer", "top_width_um")

        elif fam == "sandwich_sio2_al2o3":
            self.config.top_core_material = "sio2"
            self.config.spacer_material = "al2o3"
            self.config.active_fields = with_optional_gap("h_total_um", "f_spacer", "top_width_um")

        elif fam == "custom":
            self.config.top_core_material = self.config.top_core_material.strip().lower()
            self.config.spacer_material = self.config.spacer_material.strip().lower()

            if self.config.top_core_material not in ("sio2", "al2o3", "sin"):
                raise ValueError(
                    f"Unsupported top_core_material for current solver: {self.config.top_core_material}"
                )
            if self.config.spacer_material not in ("air", "sio2", "al2o3", "water", "sin"):
                raise ValueError(
                    f"Unsupported spacer_material for current solver: {self.config.spacer_material}"
                )

            self.config.active_fields = with_optional_gap("h_total_um", "f_spacer", "top_width_um")

        else:
            raise ValueError(f"Unknown structure_family: {self.config.structure_family}")

        base = self._normalize_height_fields(base)
        for field_name, (lo, hi) in self.config.bounds.items():
            if field_name in base:
                base[field_name] = self._snap_value_to_grid(field_name, min(hi, max(lo, base[field_name])))
        self.config.fixed_point = CandidatePoint(**base)

    def _fmt_point_short(self, p: CandidatePoint) -> str:
        return (
            f"al2o3={p.al2o3_thickness_um:.3f} "
            f"bto={p.bto_thickness_um:.3f} "
            f"H={p.h_total_um:.3f} "
            f"f={p.f_spacer:.2f} "
            f"sp={p.spacer_thickness_um:.3f} "
            f"w={p.top_width_um:.3f} "
            f"h={p.top_height_um:.3f} "
            f"gap={p.electrode_gap_um:.3f}"
        )

    def _print_progress(self, t0: float, prefix: str = "[Progress]") -> None:
        elapsed = time.monotonic() - t0
        left = self._time_left()
        best = self.store.best()

        if best and best.success:
            best_str = (
                f"best_score={best.score:.6f} loss={best.loss_db_cm:.6f} "
                f"dN={best.delta_n:.3e} gamma={best.gamma:.4f} "
                f"type={best.mode_type_base} guess={best.selected_mode_neff_guess:.3f} "
                f"TE={best.te_fraction:.3f} topC={best.top_conf_base:.3f}"
            )
        else:
            best_str = "best=None"

        print(
            f"{prefix} elapsed={elapsed:.1f}s left={left:.1f}s "
            f"evals={self.evaluations_total} ok={self.successful_evaluations} "
            f"local_opt={self.local_optima_found} boundary={self.boundary_stopped} | {best_str}",
            flush=True,
        )

    # ------------------------------------------------------------------
    # Point utilities
    # ------------------------------------------------------------------
    def _normalize_height_fields(self, d: Dict[str, float]) -> Dict[str, float]:
        h_total = float(d.get("h_total_um", 0.0))
        if h_total <= 0:
            h_total = float(d.get("spacer_thickness_um", 0.0)) + float(d.get("top_height_um", 0.0))
        if h_total <= 0:
            h_total = 0.5

        f_spacer = float(d.get("f_spacer", 0.0))
        if f_spacer < 0 or f_spacer > 1:
            base_spacer = float(d.get("spacer_thickness_um", 0.0))
            f_spacer = base_spacer / h_total if h_total > 0 else 0.0

        f_spacer = min(1.0, max(0.0, f_spacer))
        h_spacer = h_total * f_spacer
        h_rib = h_total * (1.0 - f_spacer)

        dx = float(self.config.dx_um)
        dy = float(self.config.dy_um)

        def q(v: float, step: float) -> float:
            return round(round(v / step) * step, 6)

        h_total_q = q(h_total, dy)
        h_spacer_q = q(h_spacer, dy)
        h_rib_q = round(h_total_q - h_spacer_q, 6)

        d["h_total_um"] = h_total_q
        d["f_spacer"] = round(f_spacer, 6)
        d["spacer_thickness_um"] = h_spacer_q
        d["top_height_um"] = h_rib_q

        if "al2o3_thickness_um" in d:
            d["al2o3_thickness_um"] = q(float(d["al2o3_thickness_um"]), dy)
        if "bto_thickness_um" in d:
            d["bto_thickness_um"] = q(float(d["bto_thickness_um"]), dy)
        if "top_width_um" in d:
            d["top_width_um"] = q(float(d["top_width_um"]), dx)
        if "electrode_gap_um" in d:
            d["electrode_gap_um"] = q(float(d["electrode_gap_um"]), dx)
        return d

    def _snap_value_to_grid(self, field_name: str, value: float) -> float:
        step = float(self.config.step_sizes[field_name])
        lo, hi = self.config.bounds[field_name]

        if hi <= lo:
            return round(lo, 6)

        if step <= 0:
            return round(min(hi, max(lo, value)), 6)

        k = round((value - lo) / step)
        snapped = lo + k * step
        snapped = min(hi, max(lo, snapped))
        return round(snapped, 6)

    def random_point(self) -> CandidatePoint:
        kwargs = asdict(self.config.fixed_point)
        for field_name in self.config.active_fields:
            lo, hi = self.config.bounds[field_name]
            if hi <= lo:
                kwargs[field_name] = round(lo, 6)
                continue
            raw = self.rng.uniform(lo, hi)
            kwargs[field_name] = self._snap_value_to_grid(field_name, raw)
        kwargs = self._normalize_height_fields(kwargs)
        return CandidatePoint(**kwargs)

    def clamp_point(self, point: CandidatePoint) -> CandidatePoint:
        d = asdict(point)

        for field_name, (lo, hi) in self.config.bounds.items():
            if hi <= lo:
                d[field_name] = round(lo, 6)
            else:
                d[field_name] = self._snap_value_to_grid(field_name, min(hi, max(lo, d[field_name])))

        base = asdict(self.config.fixed_point)
        for field_name in d:
            if field_name not in self.config.active_fields:
                d[field_name] = base[field_name]

        if self.config.structure_family in ("sio2_patch", "patch_sio2", "patch_al2o3", "patch_sin"):
            d["f_spacer"] = 0.0

        d = self._normalize_height_fields(d)
        return CandidatePoint(**d)

    def _time_left(self) -> float:
        return max(0.0, self._deadline_monotonic - time.monotonic())

    # ------------------------------------------------------------------
    # Solver creation / mapping
    # ------------------------------------------------------------------
    def _make_solver(self) -> CombinedBTOFlatThinFilmSolver:
        try:
            return CombinedBTOFlatThinFilmSolver(
                wavelength_um=self.config.wavelength_um,
                dx=self.config.dx_um,
                dy=self.config.dy_um,
                verbose=False,
            )
        except TypeError:
            return CombinedBTOFlatThinFilmSolver(
                wavelength_um=self.config.wavelength_um,
                dx=self.config.dx_um,
                dy=self.config.dy_um,
            )

    def _apply_static_solver_settings(self, solver: CombinedBTOFlatThinFilmSolver) -> None:
        solver.orientation = self.config.orientation
        solver.phi_deg = self.config.phi_deg
        solver.top_core_material = self.config.top_core_material
        solver.spacer_material = self.config.spacer_material

    def _apply_point_to_solver(self, solver: CombinedBTOFlatThinFilmSolver, point: CandidatePoint) -> None:
        h_spacer = point.h_total_um * point.f_spacer
        h_rib = point.h_total_um * (1.0 - point.f_spacer)
        solver.al2o3_thickness = point.al2o3_thickness_um
        solver.bto_thickness = point.bto_thickness_um
        solver.spacer_thickness = h_spacer
        solver.sin_rib_width = point.top_width_um
        solver.sin_rib_height = h_rib
        solver.electrode_gap = point.electrode_gap_um
        if hasattr(solver, "invalidate_geometry_cache"):
            solver.invalidate_geometry_cache()

    # ------------------------------------------------------------------
    # Branch selection
    # ------------------------------------------------------------------
    def _make_mode_reference(self, mode) -> ModeReference:
        return ModeReference(
            n_eff=float(getattr(mode, "n_eff", 0.0).real),
            top_conf=float(getattr(mode, "confinement_factor", 0.0)),
            te_fraction=float(getattr(mode, "te_fraction", 0.0)),
        )

    def _collect_te_like_base_candidates(self, solver) -> List[Dict]:
        candidates: List[Dict] = []
        for guess in self.config.n_eff_guesses:
            modes = solver.solve_modes(
                n_modes=self.config.n_modes,
                n_eff_guess=float(guess),
            )
            if not modes:
                continue

            for i, mode in enumerate(modes):
                te = float(getattr(mode, "te_fraction", 0.0))
                if te < self.config.min_te_fraction:
                    continue
                candidates.append({
                    "mode": mode,
                    "mode_index": i,
                    "n_eff_guess": float(guess),
                })
        return candidates

    def _select_seed_candidate(self, candidates: List[Dict]) -> Optional[Dict]:
        if not candidates:
            return None

        def key(c: Dict):
            m = c["mode"]
            return (
                -float(getattr(m, "confinement_factor", 0.0)),
                -float(getattr(m, "te_fraction", 0.0)),
                -float(getattr(m, "n_eff", 0.0).real),
            )

        return sorted(candidates, key=key)[0]

    def _select_tracked_candidate(self, candidates: List[Dict], ref: ModeReference) -> Optional[Dict]:
        if not candidates:
            return None

        def key(c: Dict):
            m = c["mode"]
            return (
                self._tracking_distance(ref, m),
                -float(getattr(m, "te_fraction", 0.0)),
                -float(getattr(m, "confinement_factor", 0.0)),
            )

        return sorted(candidates, key=key)[0]

    def _tracking_distance(self, ref: ModeReference, mode) -> float:
        neff = float(getattr(mode, "n_eff", 0.0).real)
        top_conf = float(getattr(mode, "confinement_factor", 0.0))
        return abs(neff - ref.n_eff) + 0.5 * abs(top_conf - ref.top_conf)

    def _select_eo_match(self, modes_eo, ref: ModeReference):
        candidates = []
        for m in modes_eo:
            te = float(getattr(m, "te_fraction", 0.0))
            if te < self.config.min_te_fraction:
                continue
            neff = float(getattr(m, "n_eff", 0.0).real)
            top_conf = float(getattr(m, "confinement_factor", 0.0))
            candidates.append((m, abs(neff - ref.n_eff), abs(top_conf - ref.top_conf)))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (x[1], x[2]))
        return candidates[0][0]

    # ------------------------------------------------------------------
    # Single-point evaluation
    # ------------------------------------------------------------------
    def evaluate_point(
        self,
        point: CandidatePoint,
        reference_mode: Optional[ModeReference],
        use_seed_selection: bool,
    ) -> EvaluationResult:
        t0 = time.monotonic()
        self.evaluations_total += 1

        if self.config.print_each_eval:
            print(
                f"[Eval {self.evaluations_total} | left {self._time_left():.1f}s] "
                f"{self._fmt_point_short(point)}",
                flush=True,
            )

        try:
            # Fabrication hard constraints
            if (point.spacer_thickness_um > 0.0 and point.spacer_thickness_um < 0.020) or (point.top_height_um < 0.050):
                return EvaluationResult(
                    success=False,
                    message="Fabrication constraints violated (spacer<th_min or rib<h_min).",
                    point=point,
                    score=math.inf,
                    evaluation_time_sec=time.monotonic() - t0,
                )
            solver = self._make_solver()
            self._apply_static_solver_settings(solver)
            self._apply_point_to_solver(solver, point)

            candidates = self._collect_te_like_base_candidates(solver)
            if not candidates:
                return EvaluationResult(
                    success=False,
                    message="No TE-like base candidates found across n_eff_guesses.",
                    point=point,
                    score=math.inf,
                    evaluation_time_sec=time.monotonic() - t0,
                )

            if use_seed_selection or reference_mode is None:
                selected = self._select_seed_candidate(candidates)
                selection_reason = "base:max_top_conf"
            else:
                selected = self._select_tracked_candidate(candidates, reference_mode)
                selection_reason = "base:tracked_branch"

            if selected is None:
                return EvaluationResult(
                    success=False,
                    message="Mode selection returned no TE-like candidate.",
                    point=point,
                    score=math.inf,
                    evaluation_time_sec=time.monotonic() - t0,
                )

            m_base = selected["mode"]
            idx_base = int(selected["mode_index"])
            guess_used = float(selected["n_eff_guess"])
            base_ref = self._make_mode_reference(m_base)

            modes_eo = solver.solve_modes_with_eo(
                voltage=self.config.voltage_v,
                n_modes=self.config.n_modes,
                n_eff_guess=guess_used,
            )
            if not modes_eo:
                return EvaluationResult(
                    success=False,
                    message=f"No EO modes found ({selection_reason}).",
                    point=point,
                    score=math.inf,
                    selected_mode_index=idx_base,
                    selected_mode_neff_guess=guess_used,
                    mode_type_base=str(getattr(m_base, "mode_type", "")),
                    loss_db_cm=float(getattr(m_base, "alpha_db_per_cm", math.inf)),
                    te_fraction=float(getattr(m_base, "te_fraction", 0.0)),
                    tm_fraction=float(getattr(m_base, "tm_fraction", 0.0)),
                    n_eff_base=float(getattr(m_base, "n_eff", 0.0).real),
                    top_conf_base=float(getattr(m_base, "confinement_factor", 0.0)),
                    mode_ref=base_ref,
                    evaluation_time_sec=time.monotonic() - t0,
                )

            m_eo = self._select_eo_match(modes_eo, base_ref)
            if m_eo is None:
                return EvaluationResult(
                    success=False,
                    message=f"No TE-like EO match found ({selection_reason}).",
                    point=point,
                    score=math.inf,
                    selected_mode_index=idx_base,
                    selected_mode_neff_guess=guess_used,
                    mode_type_base=str(getattr(m_base, "mode_type", "")),
                    loss_db_cm=float(getattr(m_base, "alpha_db_per_cm", math.inf)),
                    te_fraction=float(getattr(m_base, "te_fraction", 0.0)),
                    tm_fraction=float(getattr(m_base, "tm_fraction", 0.0)),
                    n_eff_base=float(getattr(m_base, "n_eff", 0.0).real),
                    top_conf_base=float(getattr(m_base, "confinement_factor", 0.0)),
                    mode_ref=base_ref,
                    evaluation_time_sec=time.monotonic() - t0,
                )

            gamma_list = solver.compute_eo_overlap_for_modes([m_eo], self.config.voltage_v)
            gamma = float(gamma_list[0]) if gamma_list else 0.0

            loss_base = max(0.0, float(getattr(m_base, "alpha_db_per_cm", math.inf)))
            loss_eo = max(0.0, float(getattr(m_eo, "alpha_db_per_cm", math.inf)))
            n_eff_base = float(getattr(m_base, "n_eff", 0.0).real)
            n_eff_eo = float(getattr(m_eo, "n_eff", 0.0).real)
            r_eff = 800e-12
            gap_m = float(point.electrode_gap_um) * 1e-6
            dn_theory = 0.5 * (n_eff_base ** 3) * r_eff * gamma * (self.config.voltage_v / max(gap_m, 1e-30))

            # Hard constraints
            if float(getattr(m_base, "te_fraction", 0.0)) < self.config.min_te_fraction:
                return EvaluationResult(
                    success=False,
                    message="TE_fraction below minimum.",
                    point=point,
                    score=math.inf,
                    selected_mode_index=idx_base,
                    selected_mode_neff_guess=guess_used,
                    mode_type_base=str(getattr(m_base, "mode_type", "")),
                    loss_db_cm=loss_base,
                    te_fraction=float(getattr(m_base, "te_fraction", 0.0)),
                    n_eff_base=n_eff_base,
                    top_conf_base=float(getattr(m_base, "confinement_factor", 0.0)),
                    mode_ref=base_ref,
                    evaluation_time_sec=time.monotonic() - t0,
                )

            alpha_max = max(loss_base, loss_eo)
            if dn_theory < self.config.delta_n_min:
                return EvaluationResult(
                    success=False,
                    message="Hard constraints violated (|Δn|).",
                    point=point,
                    score=math.inf,
                    selected_mode_index=idx_base,
                    selected_mode_neff_guess=guess_used,
                    mode_type_base=str(getattr(m_base, "mode_type", "")),
                    loss_db_cm=loss_base,
                    te_fraction=float(getattr(m_base, "te_fraction", 0.0)),
                    n_eff_base=n_eff_base,
                    top_conf_base=float(getattr(m_base, "confinement_factor", 0.0)),
                    mode_ref=base_ref,
                    evaluation_time_sec=time.monotonic() - t0,
                )

            # Soft penalty
            if alpha_max <= self.config.loss_soft_db_cm:
                penalty_loss = 0.0
            else:
                x = (alpha_max - self.config.loss_soft_db_cm) / (self.config.loss_hard_db_cm - self.config.loss_soft_db_cm)
                x = min(1.0, max(0.0, x))
                penalty_loss = 0.05 * (x ** 2)

            if alpha_max > self.config.loss_hard_db_cm:
                vpi_l_cm = (self.config.wavelength_um * 1e-4 * self.config.voltage_v) / (2.0 * max(dn_theory, 1e-30))
                final_score = 1e6 + alpha_max * 1e4
                result = EvaluationResult(
                    success=True,
                    message="Loss above hard limit.",
                    point=point,
                    score=final_score,
                    delta_n_scaled=0.0,
                    vpi_l_cm=vpi_l_cm,
                    alpha_max_db_cm=alpha_max,
                    penalty_loss=penalty_loss,
                    selected_mode_index=idx_base,
                    selected_mode_neff_guess=guess_used,
                    mode_type_base=str(getattr(m_base, "mode_type", "")),
                    mode_type_eo=str(getattr(m_eo, "mode_type", "")),
                    loss_db_cm=loss_base,
                    loss_eo_db_cm=loss_eo,
                    te_fraction=float(getattr(m_base, "te_fraction", 0.0)),
                    tm_fraction=float(getattr(m_base, "tm_fraction", 0.0)),
                    n_eff_base=n_eff_base,
                    n_eff_eo=n_eff_eo,
                    delta_n=dn_theory,
                    gamma=gamma,
                    top_conf_base=float(getattr(m_base, "confinement_factor", 0.0)),
                    top_conf_eo=float(getattr(m_eo, "confinement_factor", 0.0)),
                    mode_ref=base_ref,
                    evaluation_time_sec=time.monotonic() - t0,
                )
                if self.config.print_each_eval:
                    print(
                        f"    -> score={result.score:.6f} VpiL={result.vpi_l_cm:.4f} "
                        f"loss={result.loss_db_cm:.6f} dN={result.delta_n:.3e} "
                        f"pen={result.penalty_loss:.4f} gamma={result.gamma:.4f} type={result.mode_type_base} "
                        f"guess={result.selected_mode_neff_guess:.3f} TE={result.te_fraction:.3f} "
                        f"topC={result.top_conf_base:.3f} t={result.evaluation_time_sec:.2f}s",
                        flush=True,
                    )
                self.successful_evaluations += 1
                return result

            # Objective: VpiL_cm * (1 + penalty_loss)
            vpi_l_cm = (self.config.wavelength_um * 1e-4 * self.config.voltage_v) / (2.0 * max(dn_theory, 1e-30))
            final_score = vpi_l_cm * (1.0 + penalty_loss)

            result = EvaluationResult(
                success=True,
                message=f"OK ({selection_reason})",
                point=point,
                score=final_score,
                delta_n_scaled=0.0,
                vpi_l_cm=vpi_l_cm,
                alpha_max_db_cm=alpha_max,
                penalty_loss=penalty_loss,
                selected_mode_index=idx_base,
                selected_mode_neff_guess=guess_used,
                mode_type_base=str(getattr(m_base, "mode_type", "")),
                mode_type_eo=str(getattr(m_eo, "mode_type", "")),
                loss_db_cm=loss_base,
                loss_eo_db_cm=loss_eo,
                te_fraction=float(getattr(m_base, "te_fraction", 0.0)),
                tm_fraction=float(getattr(m_base, "tm_fraction", 0.0)),
                n_eff_base=n_eff_base,
                n_eff_eo=n_eff_eo,
                delta_n=dn_theory,
                gamma=gamma,
                top_conf_base=float(getattr(m_base, "confinement_factor", 0.0)),
                top_conf_eo=float(getattr(m_eo, "confinement_factor", 0.0)),
                mode_ref=base_ref,
                evaluation_time_sec=time.monotonic() - t0,
            )

            if self.config.print_each_eval and result.success:
                print(
                    f"    -> score={result.score:.6f} VpiL={result.vpi_l_cm:.4f} "
                    f"loss={result.loss_db_cm:.6f} dN={result.delta_n:.3e} "
                    f"pen={result.penalty_loss:.4f} gamma={result.gamma:.4f} type={result.mode_type_base} "
                    f"guess={result.selected_mode_neff_guess:.3f} TE={result.te_fraction:.3f} "
                    f"topC={result.top_conf_base:.3f} t={result.evaluation_time_sec:.2f}s",
                    flush=True,
                )

            self.successful_evaluations += 1
            return result

        except Exception as exc:
            return EvaluationResult(
                success=False,
                message=f"{type(exc).__name__}: {exc}",
                point=point,
                score=math.inf,
                evaluation_time_sec=time.monotonic() - t0,
            )

    # ------------------------------------------------------------------
    # Neighborhood exploration
    # ------------------------------------------------------------------
    def _neighbor_points(self, point: CandidatePoint) -> List[CandidatePoint]:
        base = asdict(point)
        out: List[CandidatePoint] = []

        for field_name in self.config.active_fields:
            step = float(self.config.step_sizes[field_name])
            if step <= 0:
                continue

            for direction in (-1, 1):
                d = dict(base)
                d[field_name] = d[field_name] + direction * step
                out.append(self.clamp_point(CandidatePoint(**d)))

        # Deduplicate exact duplicates (can happen near bounds)
        unique: Dict[Tuple, CandidatePoint] = {}
        for p in out:
            key = tuple(getattr(p, f) for f in CandidatePoint.__dataclass_fields__.keys())
            unique[key] = p
        return list(unique.values())

    def _run_local_search(self, seed_point: CandidatePoint) -> Optional[EvaluationResult]:
        # Seed evaluation
        current = self.evaluate_point(
            point=seed_point,
            reference_mode=None,
            use_seed_selection=True,
        )
        if current.success:
            self.store.try_add(current)

        # Local greedy walk with branch tracking
        while self._time_left() > 0.0:
            neighbors = []
            for p in self._neighbor_points(current.point):
                neighbors.append(
                    self.evaluate_point(
                        point=p,
                        reference_mode=current.mode_ref if current.success else None,
                        use_seed_selection=False,
                    )
                )

            nxt = self.stepper.choose_next(current, neighbors)
            if nxt is None:
                if current.success:
                    self.local_optima_found += 1
                    return current
                return None

            # If step got stuck on bounds / no movement, count boundary stop
            if nxt.point == current.point:
                self.boundary_stopped += 1
                if nxt.success:
                    return nxt
                return current if current.success else None

            current = nxt
            if current.success:
                self.store.try_add(current)

        # Out of time
        return current if current.success else None

    # ------------------------------------------------------------------
    # Runner
    # ------------------------------------------------------------------
    def run(self) -> Dict:
        t0 = time.monotonic()
        self._deadline_monotonic = t0 + max(0.0, self.config.time_limit_sec)

        # Live progress tracking
        self._last_progress_print_t = t0
        self._last_progress_print_eval = 0
        self._print_progress(t0, prefix="[Start]")

        while self._time_left() > 0.0:
            seed = self.random_point()
            local_best = self._run_local_search(seed)
            if local_best is not None and local_best.success:
                self.store.try_add(local_best)

            # periodic progress print by time
            if self.config.progress_every_sec > 0:
                if (time.monotonic() - self._last_progress_print_t) >= self.config.progress_every_sec:
                    self._print_progress(t0)
                    self._last_progress_print_t = time.monotonic()

            # periodic progress print by eval count
            if self.config.progress_every_evals and self.config.progress_every_evals > 0:
                if (self.evaluations_total - self._last_progress_print_eval) >= self.config.progress_every_evals:
                    self._print_progress(t0, prefix="[Progress/evals]")
                    self._last_progress_print_eval = self.evaluations_total

        elapsed = time.monotonic() - t0
        self._print_progress(t0, prefix="[End]")
        return self.build_report(elapsed)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def _serialize_result(self, r: EvaluationResult) -> Dict:
        data = asdict(r)
        if data.get("mode_ref") is None:
            data["mode_ref"] = None
        return data

    def build_report(self, elapsed_sec: float) -> Dict:
        best = self.store.best()
        return {
            "structure_family": self.config.structure_family,
            "elapsed_time_sec": float(elapsed_sec),
            "time_limit_sec": float(self.config.time_limit_sec),
            "evaluations_total": int(self.evaluations_total),
            "successful_evaluations": int(self.successful_evaluations),
            "local_optima_found": int(self.local_optima_found),
            "boundary_stops": int(self.boundary_stopped),
            "config": asdict(self.config),
            "best_result": self._serialize_result(best) if best else None,
            "top_results": [self._serialize_result(r) for r in self.store.results()],
        }


# ============================================================================
# CLI helpers
# ============================================================================

def parse_n_eff_guesses(s: str) -> Tuple[float, ...]:
    parts = [p.strip() for p in str(s).split(",") if p.strip()]
    out: List[float] = []
    for p in parts:
        try:
            out.append(float(p))
        except ValueError:
            continue
    return tuple(out) if out else (1.7, 1.9, 2.1)


def build_default_config_from_args(args: argparse.Namespace) -> OptimizerConfig:
    step_scale = float(args.step_scale)

    h_total = float(args.fixed_spacer) + float(args.fixed_top_height)
    f_spacer = (float(args.fixed_spacer) / h_total) if h_total > 0 else 0.0
    f_spacer = min(1.0, max(0.0, f_spacer))

    config = OptimizerConfig(
        structure_family=args.structure_family,
        top_core_material=args.top_core_material,
        spacer_material=args.spacer_material,
        wavelength_um=1.55,
        dx_um=float(args.dx),
        dy_um=float(args.dy),
        voltage_v=float(args.voltage),
        orientation="a-axis",
        phi_deg=45.0,
        n_modes=int(args.n_modes),
        n_eff_guesses=parse_n_eff_guesses(args.n_eff_guesses),
        min_te_fraction=float(args.min_te_fraction),
        time_limit_sec=float(args.time_limit_sec),
        top_k=int(args.top_k),
        random_seed=args.seed,
        opt_gap=bool(getattr(args, "opt_gap", False)),
        print_each_eval=bool(getattr(args, "print_each_eval", False)),
        progress_every_sec=float(getattr(args, "progress_every_sec", 60.0)),
        progress_every_evals=int(getattr(args, "progress_every_evals", 0)),
        fixed_point=CandidatePoint(
            al2o3_thickness_um=float(args.fixed_al2o3),
            bto_thickness_um=float(args.fixed_bto),
            h_total_um=h_total,
            f_spacer=f_spacer,
            spacer_thickness_um=float(args.fixed_spacer),
            top_width_um=float(args.fixed_top_width),
            top_height_um=float(args.fixed_top_height),
            electrode_gap_um=float(args.fixed_gap),
        ),
    )

    for k in list(config.step_sizes.keys()):
        config.step_sizes[k] = round(config.step_sizes[k] * step_scale, 10)

    return config


def print_report(report: Dict) -> None:
    print("\n" + "=" * 80)
    print("OPTIMIZATION SUMMARY")
    print("=" * 80)
    print(f"Structure family        : {report.get('structure_family', '')}")
    print(f"Top core material      : {report['config']['top_core_material']}")
    print(f"Spacer material        : {report['config']['spacer_material']}")
    print(f"Elapsed time           : {report['elapsed_time_sec']:.2f} s")
    print(f"Total evaluations      : {report['evaluations_total']}")
    print(f"Successful evaluations : {report['successful_evaluations']}")
    print(f"Local optima found     : {report['local_optima_found']}")
    print(f"Boundary stops         : {report['boundary_stops']}")

    best = report.get("best_result")
    if not best:
        print("\nNo valid tracked TE-like result found.")
        return

    print("\nBest result:")
    print(f"  score (mod objective) : {best['score']:.6f}")
    print(f"  loss_db_cm            : {best['loss_db_cm']:.6f}")
    print(f"  delta_n               : {best['delta_n']:.6f}")
    print(f"  delta_n_scaled        : {best.get('delta_n_scaled', 0.0):.6f}")
    print(f"  height penalty        : {best.get('height_penalty_value', 0.0):.6f}")
    print(f"  mode_type_base        : {best['mode_type_base']}")
    print(f"  selected n_eff_guess  : {best['selected_mode_neff_guess']:.3f}")
    print(f"  te_fraction           : {best['te_fraction']:.4f}")
    print(f"  gamma                 : {best['gamma']:.4f}")
    print(f"  n_eff_base            : {best['n_eff_base']:.6f}")
    print(f"  top_conf_base         : {best['top_conf_base']:.4f}")
    print(f"  message               : {best['message']}")
    print("  point:")
    for k, v in best["point"].items():
        print(f"    {k:<20} = {v:.6f}" if isinstance(v, float) else f"    {k:<20} = {v}")

    top_results = report.get("top_results", [])
    if top_results:
        print("\nTop-K distinct local optima:")
        for i, r in enumerate(top_results, start=1):
            print(
                f"  [{i}] score={r['score']:.6f}, "
                f"loss={r['loss_db_cm']:.6f}, "
                f"dN={r['delta_n']:.6f}, "
                f"dN_scaled={r.get('delta_n_scaled', 0.0):.6f}, "
                f"penalty={r.get('height_penalty_value', 0.0):.6f}, "
                f"type={r['mode_type_base']}, "
                f"guess={r['selected_mode_neff_guess']:.3f}, "
                f"TE={r['te_fraction']:.4f}, "
                f"top_conf={r['top_conf_base']:.4f}, "
                f"gamma={r['gamma']:.4f}"
            )


# =============================================================================
# JSON CONFIG SUPPORT  (schema type: "bto_sandwich_sweep" / version "0.2.0")
# =============================================================================

EXPECTED_TYPE   = "bto_sandwich_sweep"
EXPECTED_SCHEMA = "0.2.0"


def load_cfg(cfg_path: str) -> dict:
    """Load and validate a JSON config file for this solver."""
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    if cfg.get("type") != EXPECTED_TYPE:
        raise ValueError(f"Wrong config type: {cfg.get('type')!r} (expected {EXPECTED_TYPE!r})")
    if cfg.get("schema_version") != EXPECTED_SCHEMA:
        raise ValueError(f"Wrong schema_version: {cfg.get('schema_version')!r} (expected {EXPECTED_SCHEMA!r})")
    return cfg


def build_config_from_json(cfg: dict) -> "OptimizerConfig":
    """Convert a JSON config dict into an OptimizerConfig dataclass."""
    geom = cfg.get("geometry", {})

    spacer_t = float(geom.get("spacer_thickness_um", 0.050))
    top_h    = float(geom.get("top_height_um",       0.150))
    h_total  = spacer_t + top_h
    f_spacer = min(1.0, max(0.0, spacer_t / h_total if h_total > 0 else 0.0))

    raw_guesses = cfg.get("n_eff_guesses", [1.7, 1.9, 2.1])
    if isinstance(raw_guesses, str):
        guesses: Tuple[float, ...] = parse_n_eff_guesses(raw_guesses)
    else:
        guesses = tuple(float(x) for x in raw_guesses)

    config = OptimizerConfig(
        structure_family   = cfg.get("structure_family",  "sio2_patch"),
        top_core_material  = cfg.get("top_core_material", "sio2"),
        spacer_material    = cfg.get("spacer_material",   "air"),
        wavelength_um      = float(cfg.get("wavelength_um",    1.55)),
        dx_um              = float(cfg.get("dx_um",            0.02)),
        dy_um              = float(cfg.get("dy_um",            0.02)),
        voltage_v          = float(cfg.get("voltage_v",        3.0)),
        orientation        = cfg.get("orientation",            "a-axis"),
        phi_deg            = float(cfg.get("phi_deg",          45.0)),
        n_modes            = int(cfg.get("n_modes",            8)),
        n_eff_guesses      = guesses,
        min_te_fraction    = float(cfg.get("min_te_fraction",  0.85)),
        time_limit_sec     = float(cfg.get("time_limit_sec",   3600.0)),
        top_k              = int(cfg.get("top_k",              3)),
        random_seed        = cfg.get("sweep_random_seed",      None),
        opt_gap            = bool(cfg.get("opt_gap",           False)),
        fixed_point        = CandidatePoint(
            al2o3_thickness_um  = float(geom.get("al2o3_thickness_um", 0.026)),
            bto_thickness_um    = float(geom.get("bto_thickness_um",   0.150)),
            h_total_um          = h_total,
            f_spacer            = f_spacer,
            spacer_thickness_um = spacer_t,
            top_width_um        = float(geom.get("top_width_um",       1.000)),
            top_height_um       = top_h,
            electrode_gap_um    = float(geom.get("electrode_gap_um",   4.400)),
        ),
    )
    return config


def resolve_save_json(cfg: dict, cli_save_json: str) -> str:
    """Create a per-run timestamped directory and return the report JSON path inside it.

    Priority:
      1. output_dir from config → creates {output_dir}/{dir_prefix}_{timestamp}/report.json
      2. cli_save_json fallback (no directory created)
    """
    from datetime import datetime

    out_section = cfg.get("output", {})
    out_dir  = (out_section.get("output_dir", "") or "").strip()
    prefix   = (out_section.get("dir_prefix",  "bto_sandwich_sweep") or "bto_sandwich_sweep").strip()
    cfg_name = (out_section.get("save_json",   "") or "").strip()

    # Report filename: prefer config, then CLI, then default
    report_filename = Path(cfg_name or cli_save_json or "sweep_report.json").name

    if out_dir:
        stamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = Path(out_dir).expanduser() / f"{prefix}_{stamp}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return str(run_dir / report_filename)

    # No output_dir: use CLI path or cwd fallback
    return cli_save_json or report_filename


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Time-budgeted multi-start local optimizer for the flat-BTO sandwich solver."
    )

    parser.add_argument(
        "--structure-family",
        type=str,
        default="sio2_patch",
        choices=["sio2_patch", "sio2_air_bto", "al2o3_sio2_bto", "patch_sio2", "patch_al2o3", "patch_sin", "sandwich_al2o3_sio2", "sandwich_sio2_al2o3", "custom"],
        help="Preset structure family or custom material mode.",
    )

    parser.add_argument(
        "--top-core-material",
        type=str,
        default="sio2",
        choices=["sio2", "al2o3", "sin"],
        help="Top core material (used directly in custom mode).",
    )

    parser.add_argument(
        "--spacer-material",
        type=str,
        default="air",
        choices=["air", "sio2", "al2o3", "water", "sin"],
        help="Spacer material (used directly in custom mode).",
    )

    parser.add_argument(
        "--opt-gap",
        action="store_true",
        help="Allow electrode gap to be optimized. If omitted, gap stays fixed at --fixed-gap.",
    )

    parser.add_argument("--time-limit-sec", type=float, default=60.0,
                        help="Total optimization time budget in seconds.")
    parser.add_argument("--step-scale", type=float, default=1.0,
                        help="Global multiplier for all neighborhood step sizes.")
    parser.add_argument("--top-k", type=int, default=3,
                        help="Number of distinct local optima to keep.")
    parser.add_argument("--seed", type=int, default=None,
                        help="Optional random seed for repeatability.")

    parser.add_argument("--print-each-eval", action="store_true",
                        help="Print every evaluated point and its result (live monitoring).")
    parser.add_argument("--progress-every-sec", type=float, default=60.0,
                        help="Print a progress line every N seconds (0 disables).")
    parser.add_argument("--progress-every-evals", type=int, default=0,
                        help="Print a progress line every N evaluations (0 disables).")

    parser.add_argument("--dx", type=float, default=0.02,
                        help="Static solver dx in um.")
    parser.add_argument("--dy", type=float, default=0.02,
                        help="Static solver dy in um.")
    parser.add_argument("--voltage", type=float, default=3.0,
                        help="Static evaluation voltage in V.")

    parser.add_argument("--min-te-fraction", type=float, default=0.80,
                        help="Minimum TE fraction required for a mode to be considered.")
    parser.add_argument("--n-modes", type=int, default=8,
                        help="Number of modes requested per solve.")
    parser.add_argument("--n-eff-guesses", type=str, default="1.7,1.9,2.1",
                        help="Comma-separated list of n_eff guesses.")

    parser.add_argument("--fixed-al2o3", type=float, default=0.026,
                        help="Fixed Al2O3 thickness in um.")
    parser.add_argument("--fixed-bto", type=float, default=0.150,
                        help="Fixed BTO thickness in um.")
    parser.add_argument("--fixed-spacer", type=float, default=0.050,
                        help="Baseline spacer thickness in um.")
    parser.add_argument("--fixed-top-width", type=float, default=1.000,
                        help="Fixed top width in um.")
    parser.add_argument("--fixed-top-height", type=float, default=0.150,
                        help="Baseline top height in um.")
    parser.add_argument("--fixed-gap", type=float, default=4.400,
                        help="Fixed electrode gap in um.")

    parser.add_argument("--save-json", type=str, default="",
                        help="Optional path to save the final report as JSON.")

    parser.add_argument("--config", type=str, default="",
                        help="Path to JSON config file (type=bto_sandwich_sweep, schema 0.2.0). "
                             "When provided, all geometry/solver settings are loaded from JSON; "
                             "other CLI flags become fallback defaults only.")

    args = parser.parse_args()

    if args.config:
        cfg_data = load_cfg(args.config)
        config = build_config_from_json(cfg_data)
        save_json_path = resolve_save_json(cfg_data, args.save_json)
    else:
        config = build_default_config_from_args(args)
        save_json_path = args.save_json

    optimizer = SandwichAutosweeper(config)
    report = optimizer.run()

    print_report(report)

    if save_json_path:
        save_path = sanitize_path(save_json_path)
        ensure_dir(save_path.parent)
        with save_path.open("w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"\nSaved report to: {save_path}")


if __name__ == "__main__":
    main()
