#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTO Flat Thin Film Waveguide — Electro-optic Solver
==================================================

Flat thin-film stack family (bottom → top)
  1. SiO₂ substrate (oxide box)
  2. thin Al₂O₃ buffer
  3. BaTiO₃ (BTO) flat thin film — no etch-back
  4. configurable spacer layer (air / SiO₂ / water proxy)
  5. top rib waveguide (default geometry still reuses the original SiN dimensions)
  6. gold electrodes with central gap (placed on top of BTO outside the central gap)

All EM, electrostatic and EO-overlap calculations follow the original
ridge-waveguide solver, adapted for a *flat* BTO layer with an optional
intermediate spacer between BTO and the top rib.

Clean-ups in this edition
-------------------------
* Plain Matplotlib (`text.usetex = False`) — no LaTeX probing.
* Single global style block and FIGSIZE constant.
* Duplicate `plt.rcParams` definitions removed.
* Internal variable names like `sin_rib_width` are still kept for stability.
"""

from __future__ import annotations

# ── std-lib / third-party imports ────────────────────────────────────────────
import numpy as np
import matplotlib.pyplot as plt
from scipy.sparse import coo_matrix, lil_matrix
from scipy.sparse.linalg import eigs, spsolve
from dataclasses import dataclass
import warnings
warnings.filterwarnings("default")


# ──────────────────────────────────────────────────────────────────────────────
# Matplotlib configuration (plain defaults, no LaTeX)
# ──────────────────────────────────────────────────────────────────────────────
FIGSIZE = (14, 10)          # every figure uses the same dimensions
plt.style.use("default")
plt.rcParams.update({
    "text.usetex": False,
    "font.size": 12,
    "axes.labelsize": 12,
    "axes.titlesize": 14,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "legend.fontsize": 10,
    "figure.titlesize": 16,
})

# ──────────────────────────────────────────────────────────────────────────────
# Global constants
# ──────────────────────────────────────────────────────────────────────────────
OUTLINE_COLOR   = "white"   # geometry overlay
NEFF_RE_TOL     = 1e-6
NEFF_IM_TOL     = 1e-6
INT_OVERLAP_THR = 1 - 1e-3
GRID_DENSITY    = 0.05      # µm

# ═════════════════════════════════════════════════════════════════════════════
# Dataclass for mode storage
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class FullVectorMode:
    n_eff: complex
    beta: complex
    Hx: np.ndarray
    Hy: np.ndarray
    Hz: np.ndarray
    Ex: np.ndarray
    Ey: np.ndarray
    Ez: np.ndarray
    E_intensity: np.ndarray
    H_intensity: np.ndarray
    confinement_factor: float
    alpha_db_per_cm: float
    mode_type: str
    te_fraction: float
    tm_fraction: float

# ═════════════════════════════════════════════════════════════════════════════
# CombinedBTOFlatThinFilmSolver
# ═════════════════════════════════════════════════════════════════════════════
class CombinedBTOFlatThinFilmSolver:
    """
    Full-vector optical-mode + electrostatic + EO-overlap solver for the
    *flat* BTO thin-film architecture.
    """

    # ─────────────────────────────────────────────────────────────────────────
    # Initialisation
    # ─────────────────────────────────────────────────────────────────────────
    def __init__(self, wavelength_um=1.55,
                 dx: float = GRID_DENSITY,
                 dy: float = GRID_DENSITY,
                 verbose: bool = True) -> None:
        # wavelength & wavenumber
        self.wl  = float(wavelength_um)          # µm
        self.k0  = 2*np.pi / self.wl             # µm-¹

        # rectangular Yee-grid
        self.dx  = float(dx)
        self.dy  = float(dy)
        self.verbose = bool(verbose)

        # ── optical indices ───────────────────────────────────────────────
        self.n_sio2 = 1.444 + 0j
        self.n_air  = 1.0   + 0j
        self.n_gold = 0.55  + 8j
        self.n_al2o3 = 1.77 + 0j
        self.n_sin   = 2.8  + 0j
        self.n_water = 1.318 + 0j  # simple static optical proxy at 1.55 µm

        # configurable top-core / spacer family
        # top_core_material: 'sin', 'sio2', 'al2o3'
        self.top_core_material = "sin"
        # spacer_material: 'air', 'sio2', 'al2o3', 'water', 'sin'
        self.spacer_material = "air"
        self.spacer_thickness = 0.0
        
        # visual colour for filled electrodes
        gold_hex       = "#F1CB61"
        self.gold_color = gold_hex

        # BTO principal indices
        self.n_bto_o = 2.444 + 0j     # ordinary
        self.n_bto_e = 2.383 + 0j     # extraordinary (negative uniaxial)


        # EO tensor (m/V) - Tao et al. typical values
        self.r13 = 8.0e-12    # pm/V 
        self.r33 = 28.0e-12   # pm/V
        self.r42 = 800.0e-12  # pm/V (r51 = r42) - dominates for a-axis
        self.r51 = self.r42
        
        
        # # EO tensor coefficients (m V⁻¹)
        # self.r13 = -63e-12
        # self.r33 = 342e-12
        # self.r42 = 923e-12
        # self.r51 = self.r42

        # orientation
        self.orientation = "a-axis"
        self.phi_deg = 0.0
        self.tilt_deg = 0.0

        # ── electrostatic ε_r ─────────────────────────────────────────────
        self.eps_sio2  = 3.9
        self.eps_bto   = 2200.0
        self.eps_al2o3 = 9.3
        self.eps_sin   = 7.5
        self.eps_air   = 1.0
        self.eps_water = 1.77   # static placeholder for geometry-only electrostatic proxy
        self.eps_metal = 1e9

        # ── geometry (µm) ─────────────────────────────────────────────────
        self.oxide_thickness     = 2.00
        self.al2o3_thickness     = 0.002
        self.bto_thickness       = 0.250  # 250 nm
        self.electrode_gap       = 4.0
        self.electrode_thickness = 0.55
        self.sin_rib_width       = 1.2
        self.sin_rib_height      = 0.10

        # computational domain (µm)
        self.domain_width  = 8.0
        self.domain_height = 4.0

        # misc
        self.strict_anisotropy = False
        self.offdiag_threshold = 1e-6
        self._geom_built = False
        self._geom_signature = None

        if self.verbose:
            print(f"BTO Flat-Film solver @ λ = {self.wl:.3f} µm, "
                  f"grid = {self.dx:.3f} × {self.dy:.3f} µm")   

    @staticmethod
    def _Rx(theta):
        c, s = np.cos(theta), np.sin(theta)
        return np.array([[1, 0, 0],
                         [0, c, -s],
                         [0, s,  c]], float)

    @staticmethod
    def _Ry(theta):
        c, s = np.cos(theta), np.sin(theta)
        return np.array([[ c, 0, s],
                         [ 0, 1, 0],
                         [-s, 0, c]], float)

    @staticmethod
    def _Rz(theta):
        c, s = np.cos(theta), np.sin(theta)
        return np.array([[ c, -s, 0],
                         [ s,  c, 0],
                         [ 0,  0, 1]], float)

    def _get_rotations(self, orientation, phi_deg, tilt_deg):
        phi = np.deg2rad(phi_deg)
        tilt = np.deg2rad(tilt_deg)

        if orientation.lower() == "c-axis":
            R_intrinsic = np.array([[1, 0, 0],
                                    [0, 0, 1],
                                    [0, 1, 0]], float)
        elif orientation.lower() == "a-axis":
            R_intrinsic = np.array([[0, 1, 0],
                                    [0, 0, 1],
                                    [1, 0, 0]], float)
        else:
            raise ValueError("orientation must be 'a-axis' or 'c-axis'")

        R_phi = self._Rz(phi)
        R_tilt = self._Ry(tilt)
        R_g2c = R_intrinsic @ R_phi @ R_tilt
        R_c2g = R_g2c.T
        return R_g2c, R_c2g

    def rotation_sanity_check(self, phi_a=0.0, phi_b=45.0, orientation=None):
        if orientation is None:
            orientation = self.orientation
            
        if orientation.lower() == "c-axis":
            eps_c = np.diag([(self.n_bto_o**2).real,
                             (self.n_bto_o**2).real,
                             (self.n_bto_e**2).real])
        else:  # a-axis
            eps_c = np.diag([(self.n_bto_e**2).real,
                             (self.n_bto_o**2).real,
                             (self.n_bto_o**2).real])
        
        R_g2c_a, R_c2g_a = self._get_rotations(orientation, phi_a, 0.0)
        R_g2c_b, R_c2g_b = self._get_rotations(orientation, phi_b, 0.0)
        
        eps_glob_a = R_c2g_a @ eps_c @ R_g2c_a
        eps_glob_b = R_c2g_b @ eps_c @ R_g2c_b
        
        delta = np.linalg.norm(eps_glob_b - eps_glob_a, 'fro')
        
        print(f"Rotation sanity check ({orientation}):")
        print(f"  φ_a = {phi_a}°, φ_b = {phi_b}°")
        print(f"  δ(φ_a,φ_b) = ||ε_g(φ_b) - ε_g(φ_a)||_F = {delta:.4f}")
        print(f"  Expected: δ >> 0 for φ_b ≠ φ_a ✓" if delta > 0.1 else f"  WARNING: δ too small!")
        
        return delta

    # ---------- NEW FLAT GEOMETRY ----------
    def _current_geom_signature(self):
        return (
            self.dx, self.dy,
            self.domain_width, self.domain_height,
            self.oxide_thickness, self.al2o3_thickness, self.bto_thickness,
            self.electrode_gap, self.electrode_thickness,
            self.sin_rib_width, self.sin_rib_height,
            self.top_core_material, self.spacer_material, self.spacer_thickness,
            self.orientation, self.phi_deg, self.tilt_deg,
        )

    def invalidate_geometry_cache(self):
        self._geom_built = False
        self._geom_signature = None
        for attr in ("_sin_mask_vertices", "_bto_mask_vertices", "_bto_mask_centers", "_bto_coordinates"):
            if hasattr(self, attr):
                delattr(self, attr)

    def _ensure_geometry(self):
        sig = self._current_geom_signature()
        if (not self._geom_built) or (sig != self._geom_signature):
            self.invalidate_geometry_cache()
            self._shared = self.create_shared_geometry()
            self._geom_built = True
            self._geom_signature = sig
        return self._shared

    def create_shared_geometry(self):
        nx_v = int(round(self.domain_width / self.dx)) + 1
        ny_v = int(round(self.domain_height / self.dy)) + 1
        self.x  = np.linspace(0, self.domain_width, nx_v)
        self.y  = np.linspace(0, self.domain_height, ny_v)
        self.xc = 0.5*(self.x[:-1] + self.x[1:])
        self.yc = 0.5*(self.y[:-1] + self.y[1:])
        self.nx_v, self.ny_v = nx_v, ny_v
        self.nx_c, self.ny_c = len(self.xc), len(self.yc)

        epsxx_opt, epsxy_opt, epsyx_opt, epsyy_opt, epszz_opt = self._create_optical_structure()
        eps_r_elec = self._create_electrostatic_structure()
        return epsxx_opt, epsxy_opt, epsyx_opt, epsyy_opt, epszz_opt, eps_r_elec

    def _build_flat_bto_mask(self, Xc, Yc):
        """Return BTO thin film mask (flat layer - no ridge)."""
        al2o3_top = self.oxide_thickness + self.al2o3_thickness
        bto_bot = al2o3_top
        bto_top = bto_bot + self.bto_thickness

        # Simple rectangular BTO layer across full width
        mask_bto = (Yc >= bto_bot) & (Yc < bto_top)
        return mask_bto, bto_top

    def _layer_positions(self):
        al2o3_bot = self.oxide_thickness
        al2o3_top = al2o3_bot + self.al2o3_thickness
        bto_bot = al2o3_top
        bto_top = bto_bot + self.bto_thickness
        spacer_bot = bto_top
        spacer_top = spacer_bot + self.spacer_thickness
        top_bot = spacer_top
        top_top = top_bot + self.sin_rib_height
        electrode_bot = bto_top
        electrode_top = electrode_bot + self.electrode_thickness
        return {
            "al2o3_bot": al2o3_bot,
            "al2o3_top": al2o3_top,
            "bto_bot": bto_bot,
            "bto_top": bto_top,
            "spacer_bot": spacer_bot,
            "spacer_top": spacer_top,
            "top_bot": top_bot,
            "top_top": top_top,
            "electrode_bot": electrode_bot,
            "electrode_top": electrode_top,
        }

    def _top_core_x_bounds(self):
        cx = self.domain_width / 2
        left = cx - self.sin_rib_width / 2
        right = cx + self.sin_rib_width / 2
        return cx, left, right

    def _get_top_core_index(self):
        mat = self.top_core_material.lower()
        if mat == "sin":
            return self.n_sin
        if mat == "sio2":
            return self.n_sio2
        if mat == "al2o3":
            return self.n_al2o3
        raise ValueError(f"Unknown top_core_material: {self.top_core_material}")

    def _get_top_core_eps(self):
        mat = self.top_core_material.lower()
        if mat == "sin":
            return self.eps_sin
        if mat == "sio2":
            return self.eps_sio2
        if mat == "al2o3":
            return self.eps_al2o3
        raise ValueError(f"Unknown top_core_material: {self.top_core_material}")

    def _get_spacer_index(self):
        mat = self.spacer_material.lower()
        if mat == "air":
            return self.n_air
        if mat == "sio2":
            return self.n_sio2
        if mat == "al2o3":
            return self.n_al2o3
        if mat == "water":
            return self.n_water
        if mat == "sin":
            return self.n_sin
        raise ValueError(f"Unknown spacer_material: {self.spacer_material}")

    def _get_spacer_eps(self):
        mat = self.spacer_material.lower()
        if mat == "air":
            return self.eps_air
        if mat == "sio2":
            return self.eps_sio2
        if mat == "al2o3":
            return self.eps_al2o3
        if mat == "water":
            return self.eps_water
        if mat == "sin":
            return self.eps_sin
        raise ValueError(f"Unknown spacer_material: {self.spacer_material}")

    def _build_spacer_mask(self, Xc, Yc):
        if self.spacer_thickness <= 0:
            return np.zeros_like(Xc, dtype=bool)
        pos = self._layer_positions()
        return (Yc >= pos["spacer_bot"]) & (Yc < pos["spacer_top"])

    def _build_top_core_mask(self, Xc, Yc):
        pos = self._layer_positions()
        _, left, right = self._top_core_x_bounds()
        return ((Yc >= pos["top_bot"]) & (Yc < pos["top_top"]) &
                (Xc >= left) & (Xc <= right))

    def _create_optical_structure(self):
        Xc, Yc = np.meshgrid(self.xc, self.yc, indexing='xy')
        epsxx = np.ones_like(Xc, dtype=complex) * (self.n_air**2)
        epsyy = epsxx.copy()
        epszz = epsxx.copy()
        epsxy = np.zeros_like(Xc, dtype=complex)
        epsyx = np.zeros_like(Xc, dtype=complex)

        # substrate (SiO2)
        mask_sub = (Yc < self.oxide_thickness)
        epsxx[mask_sub] = self.n_sio2**2
        epsyy[mask_sub] = self.n_sio2**2
        epszz[mask_sub] = self.n_sio2**2

        # Al2O3 buffer
        pos = self._layer_positions()
        mask_al2o3 = (Yc >= pos["al2o3_bot"]) & (Yc < pos["al2o3_top"])
        epsxx[mask_al2o3] = self.n_al2o3**2
        epsyy[mask_al2o3] = self.n_al2o3**2
        epszz[mask_al2o3] = self.n_al2o3**2

        # BTO flat thin film
        mask_bto, bto_top = self._build_flat_bto_mask(Xc, Yc)
        self._bto_mask_centers = mask_bto.copy()
        self._bto_coordinates = (Xc[mask_bto], Yc[mask_bto])

        # Apply spatially varying BTO material properties
        epsxx, epsxy, epsyx, epsyy, epszz = self._apply_bto_material_spatially(
            epsxx, epsxy, epsyx, epsyy, epszz, Xc, Yc, mask_bto)

        # optional spacer layer between BTO and top rib
        mask_spacer = self._build_spacer_mask(Xc, Yc)
        if np.any(mask_spacer):
            n_spacer = self._get_spacer_index()
            epsxx[mask_spacer] = n_spacer**2
            epsyy[mask_spacer] = n_spacer**2
            epszz[mask_spacer] = n_spacer**2
            epsxy[mask_spacer] = 0
            epsyx[mask_spacer] = 0

        # top rib waveguide on top of spacer (or directly on BTO if spacer_thickness == 0)
        mask_top = self._build_top_core_mask(Xc, Yc)
        n_top = self._get_top_core_index()
        epsxx[mask_top] = n_top**2
        epsyy[mask_top] = n_top**2
        epszz[mask_top] = n_top**2
        epsxy[mask_top] = 0
        epsyx[mask_top] = 0

        # side electrodes last (overwrite any spacer overlap in y-range)
        cx = self.domain_width / 2
        gap_half = 0.5 * self.electrode_gap
        e_bot = bto_top
        e_top = e_bot + self.electrode_thickness
        mask_e = (Yc >= e_bot) & (Yc < e_top)
        left = (Xc < cx - gap_half) & mask_e
        right = (Xc > cx + gap_half) & mask_e
        for m in (left, right):
            epsxx[m] = self.n_gold**2
            epsyy[m] = self.n_gold**2
            epszz[m] = self.n_gold**2
            epsxy[m] = 0
            epsyx[m] = 0

        if self.verbose:
            print(f"BTO thin film pixels: {int(mask_bto.sum())}/{mask_bto.size} "  
                  f"({100*mask_bto.mean():.1f}%)")
        return epsxx, epsxy, epsyx, epsyy, epszz

    def _apply_bto_material_spatially(self, epsxx, epsxy, epsyx, epsyy, epszz, Xc, Yc, mask_bto):
        """Apply BTO material properties with tensor refractive index in thin film block."""
        # base principal ε (crystal frame)
        if self.orientation.lower() == "c-axis":
            eps_c = np.diag([(self.n_bto_o**2).real,
                             (self.n_bto_o**2).real,
                             (self.n_bto_e**2).real])
        else:  # a-axis
            eps_c = np.diag([(self.n_bto_e**2).real,
                             (self.n_bto_o**2).real,
                             (self.n_bto_o**2).real])

        # rotate with SAME (φ, tilt) as EO step
        R_g2c, R_c2g = self._get_rotations(self.orientation, self.phi_deg, self.tilt_deg)
        eps_glob = R_c2g @ eps_c @ R_g2c

        # warn if large εxz/εyz
        exz, eyz = abs(eps_glob[0,2]), abs(eps_glob[1,2])
        if exz > self.offdiag_threshold or eyz > self.offdiag_threshold:
            warnings.warn(f"Base BTO tilt introduces εxz≈{exz:.2e}, εyz≈{eyz:.2e}; transverse operator ignores them.")

        # Apply uniform BTO properties in thin film block
        epsxx[mask_bto] = eps_glob[0,0]
        epsyy[mask_bto] = eps_glob[1,1]
        epsxy[mask_bto] = eps_glob[0,1]
        epsyx[mask_bto] = eps_glob[1,0]
        epszz[mask_bto] = eps_glob[2,2]
        
        return epsxx, epsxy, epsyx, epsyy, epszz

    def _create_electrostatic_structure(self):
        Xc, Yc = np.meshgrid(self.xc, self.yc, indexing='xy')
        eps_r = np.ones_like(Xc) * self.eps_air

        # substrate
        mask_sub = (Yc < self.oxide_thickness)
        eps_r[mask_sub] = self.eps_sio2

        # Al2O3 layer
        pos = self._layer_positions()
        mask_al2o3 = (Yc >= pos["al2o3_bot"]) & (Yc < pos["al2o3_top"])
        eps_r[mask_al2o3] = self.eps_al2o3

        # BTO flat thin film
        mask_bto, bto_top = self._build_flat_bto_mask(Xc, Yc)
        eps_r[mask_bto] = self.eps_bto

        # optional spacer layer
        mask_spacer = self._build_spacer_mask(Xc, Yc)
        if np.any(mask_spacer):
            eps_r[mask_spacer] = self._get_spacer_eps()

        # top rib waveguide on top of spacer
        mask_top = self._build_top_core_mask(Xc, Yc)
        eps_r[mask_top] = self._get_top_core_eps()

        # side electrodes
        cx = self.domain_width / 2
        gap_half = 0.5 * self.electrode_gap
        e_bot = bto_top
        e_top = e_bot + self.electrode_thickness
        mask_e = (Yc >= e_bot) & (Yc < e_top)
        left = (Xc < cx - gap_half) & mask_e
        right = (Xc > cx + gap_half) & mask_e
        eps_r[left | right] = self.eps_metal

        return eps_r

    def _harmonic_avg(self, a, b):
        if a == 0 or b == 0:
            return 0.0
        return 2.0*a*b/(a+b)

    def solve_electrostatic(self, voltage=3.0):
        _, _, _, _, _, eps_r = self._ensure_geometry()
        dx, dy = self.dx, self.dy
        nx, ny = self.nx_c, self.ny_c

        lambda_boundary = max(0.5, 0.5*min(self.domain_width, self.domain_height))

        N = nx*ny
        A = lil_matrix((N, N), dtype=np.float64)
        b = np.zeros(N, dtype=np.float64)

        def idx(i, j): return i*nx + j

        for i in range(ny):
            for j in range(nx):
                n = idx(i, j)

                if eps_r[i, j] == self.eps_metal:
                    A[n, n] = 1.0
                    b[n] = (-voltage/2.0) if j < nx//2 else (+voltage/2.0)
                    continue

                eps_e = self._harmonic_avg(eps_r[i, j], eps_r[i, j+1]) if j < nx-1 else eps_r[i, j]
                eps_w = self._harmonic_avg(eps_r[i, j], eps_r[i, j-1]) if j > 0     else eps_r[i, j]
                eps_n = self._harmonic_avg(eps_r[i, j], eps_r[i+1, j]) if i < ny-1 else eps_r[i, j]
                eps_s = self._harmonic_avg(eps_r[i, j], eps_r[i-1, j]) if i > 0     else eps_r[i, j]

                wE = eps_e/(dx*dx); wW = eps_w/(dx*dx)
                wN = eps_n/(dy*dy); wS = eps_s/(dy*dy)

                diag = 0.0
                if j < nx-1: A[n, idx(i, j+1)] += wE; diag += wE
                else:        diag += wE*(dx/lambda_boundary)
                if j > 0:    A[n, idx(i, j-1)] += wW; diag += wW
                else:        diag += wW*(dx/lambda_boundary)
                if i < ny-1: A[n, idx(i+1, j)] += wN; diag += wN
                else:        diag += wN*(dy/lambda_boundary)
                if i > 0:    A[n, idx(i-1, j)] += wS; diag += wS
                else:        diag += wS*(dy/lambda_boundary)

                A[n, n] -= diag

        phi_1d = spsolve(A.tocsr(), b)
        potential = phi_1d.reshape((ny, nx))

        # E = -∇φ (V/m)
        Ex = np.zeros_like(potential)
        Ey = np.zeros_like(potential)
        Ex[:, 1:-1] = -(potential[:, 2:] - potential[:, :-2]) / (2*dx*1e-6)
        Ex[:, 0]    = -(potential[:, 1] - potential[:, 0])   / (dx*1e-6)
        Ex[:, -1]   = -(potential[:, -1] - potential[:, -2]) / (dx*1e-6)
        Ey[1:-1, :] = -(potential[2:, :] - potential[:-2, :]) / (2*dy*1e-6)
        Ey[0, :]    = -(potential[1, :] - potential[0, :])     / (dy*1e-6)
        Ey[-1, :]   = -(potential[-1, :] - potential[-2, :])   / (dy*1e-6)

        E_mag = np.sqrt(Ex**2 + Ey**2)
        return potential, Ex, Ey, E_mag

    # ---------- EO perturbation (same as original) ----------
    def eps_with_pockels(self, Ex_c, Ey_c, Ez_c=None,
                         orientation=None, phi_deg=None, tilt_deg=None):
        if orientation is None: orientation = self.orientation
        if phi_deg     is None: phi_deg     = self.phi_deg
        if tilt_deg    is None: tilt_deg    = self.tilt_deg
        if Ez_c is None: Ez_c = np.zeros_like(Ex_c)

        epsxx0, epsxy0, epsyx0, epsyy0, epszz0, _ = self._ensure_geometry()
        epsxx = np.array(epsxx0, copy=True)
        epsyy = np.array(epsyy0, copy=True)
        epsxy = np.array(epsxy0, copy=True)
        epsyx = np.array(epsyx0, copy=True)
        epszz = np.array(epszz0, copy=True)

        # base principal ε (crystal frame)
        if orientation.lower() == "c-axis":
            eps_c_xx = float((self.n_bto_o**2).real)
            eps_c_yy = float((self.n_bto_o**2).real)
            eps_c_zz = float((self.n_bto_e**2).real)
        else:  # a-axis
            eps_c_xx = float((self.n_bto_e**2).real)
            eps_c_yy = float((self.n_bto_o**2).real)
            eps_c_zz = float((self.n_bto_o**2).real)

        inv_eps_base = np.diag([1.0/eps_c_xx, 1.0/eps_c_yy, 1.0/eps_c_zz])

        r13, r33, r42, r51 = self.r13, self.r33, self.r42, self.r51
        R_g2c_base, R_c2g_base = self._get_rotations(orientation, phi_deg, tilt_deg)

        mask_bto = getattr(self, "_bto_mask_centers", None)
        if mask_bto is None:
            return epsxx, epsxy, epsyx, epsyy, epszz, (0.0, 0.0)

        rr, cc = np.where(mask_bto)
        max_exz = 0.0; max_eyz = 0.0

        for i, j in zip(rr, cc):
            E_global = np.array([Ex_c[i, j], Ey_c[i, j], 0.0 if Ez_c is None else Ez_c[i, j]], float)
            E_crys = R_g2c_base @ E_global
            Exp, Eyp, Ezp = E_crys

            # Pockels tensor in crystal frame
            d_inv = np.array([
                [ r13*Ezp,         0.0,          r51*Exp ],
                [ 0.0,             r13*Ezp,      r42*Eyp ],
                [ r51*Exp,         r42*Eyp,      r33*Ezp ]
            ], float)

            inv_eps = inv_eps_base + d_inv
            try:
                eps_crys = np.linalg.inv(inv_eps)
            except np.linalg.LinAlgError:
                eps_crys = np.diag([eps_c_xx, eps_c_yy, eps_c_zz])

            eps_glob = R_c2g_base @ eps_crys @ R_g2c_base

            epsxx[i, j] = eps_glob[0, 0]
            epsyy[i, j] = eps_glob[1, 1]
            epszz[i, j] = eps_glob[2, 2]
            epsxy[i, j] = eps_glob[0, 1]
            epsyx[i, j] = eps_glob[1, 0]

            max_exz = max(max_exz, abs(eps_glob[0, 2]))
            max_eyz = max(max_eyz, abs(eps_glob[1, 2]))

        if max_exz > self.offdiag_threshold or max_eyz > self.offdiag_threshold:
            msg = (f"Note: |ε_xz|≈{max_exz:.2e}, |ε_yz|≈{max_eyz:.2e}; transverse operator ignores ε_xz/ε_yz.")
            if self.strict_anisotropy:
                raise RuntimeError(msg)
            else:
                warnings.warn(msg)

        return epsxx, epsxy, epsyx, epsyy, epszz, (max_exz, max_eyz)

    # ---------- Mode solver methods (same as original) ----------
    @staticmethod
    def centers_to_vertices(F):
        P = np.pad(F, ((1,1),(1,1)), mode='edge')
        return 0.25*(P[:-1,:-1] + P[1:,:-1] + P[:-1,1:] + P[1:,1:])

    @staticmethod  
    def vertices_to_centers(F):
        """Convert vertex values to centers (inverse of centers_to_vertices)."""
        return 0.25*(F[:-1,:-1] + F[1:,:-1] + F[:-1,1:] + F[1:,1:])

    def get_center_mesh(self):
        return np.meshgrid(self.xc, self.yc, indexing='xy')

    def get_vertex_mesh(self):
        return np.meshgrid(self.x, self.y, indexing='xy')

    def build_q_matrix(self, epsxx_c, epsxy_c, epsyx_c, epsyy_c):
        nx_v, ny_v = self.nx_v, self.ny_v
        N = nx_v*ny_v
        k0 = self.k0
        dx, dy = self.dx, self.dy

        exx_v = self.centers_to_vertices(epsxx_c)
        eyy_v = self.centers_to_vertices(epsyy_c)
        exy_v = self.centers_to_vertices(epsxy_c)
        eyx_v = self.centers_to_vertices(epsyx_c)

        rows, cols, vals = [], [], []
        def vidx(iv, jv): return iv*nx_v + jv

        ax = 1.0/dx**2; ay = 1.0/dy**2
        w_diag = -0.5*(ax+ay)
        w_ns = ax + 0.5*ay
        w_we = ay + 0.5*ax
        w_center = -2*(ax+ay)

        for iv in range(ny_v):
            for jv in range(nx_v):
                idx = vidx(iv, jv); idy = idx + N

                W  = vidx(iv, jv-1) if jv>0 else None
                E  = vidx(iv, jv+1) if jv<nx_v-1 else None
                S  = vidx(iv-1, jv) if iv>0 else None
                Nn = vidx(iv+1, jv) if iv<ny_v-1 else None
                SW = vidx(iv-1, jv-1) if (iv>0 and jv>0) else None
                SE = vidx(iv-1, jv+1) if (iv>0 and jv<nx_v-1) else None
                NW = vidx(iv+1, jv-1) if (iv<ny_v-1 and jv>0) else None
                NE = vidx(iv+1, jv+1) if (iv<ny_v-1 and jv<nx_v-1) else None

                rows.append(idx); cols.append(idx); vals.append(w_center + (k0**2)*exx_v[iv,jv])
                if W is not None: rows.append(idx); cols.append(W); vals.append(w_we)
                if E is not None: rows.append(idx); cols.append(E); vals.append(w_we)
                if S is not None: rows.append(idx); cols.append(S); vals.append(w_ns)
                if Nn is not None: rows.append(idx); cols.append(Nn); vals.append(w_ns)
                if SW is not None: rows.append(idx); cols.append(SW); vals.append(w_diag)
                if SE is not None: rows.append(idx); cols.append(SE); vals.append(w_diag)
                if NW is not None: rows.append(idx); cols.append(NW); vals.append(w_diag)
                if NE is not None: rows.append(idx); cols.append(NE); vals.append(w_diag)

                rows.append(idy); cols.append(idy); vals.append(w_center + (k0**2)*eyy_v[iv,jv])
                if W is not None: rows.append(idy); cols.append(W+N); vals.append(w_we)
                if E is not None: rows.append(idy); cols.append(E+N); vals.append(w_we)
                if S is not None: rows.append(idy); cols.append(S+N); vals.append(w_ns)
                if Nn is not None: rows.append(idy); cols.append(Nn+N); vals.append(w_ns)
                if SW is not None: rows.append(idy); cols.append(SW+N); vals.append(w_diag)
                if SE is not None: rows.append(idy); cols.append(SE+N); vals.append(w_diag)
                if NW is not None: rows.append(idy); cols.append(NW+N); vals.append(w_diag)
                if NE is not None: rows.append(idy); cols.append(NE+N); vals.append(w_diag)

                rows.append(idx); cols.append(idy); vals.append((k0**2)*exy_v[iv,jv])
                rows.append(idy); cols.append(idx); vals.append((k0**2)*eyx_v[iv,jv])

        return coo_matrix((vals, (rows, cols)), shape=(2*N, 2*N)).tocsr()

    def postprocess_murphy(self, Ex_v, Ey_v, n_eff, epsxx_c, epsxy_c, epsyx_c, epsyy_c, epszz_c):
        """Compute Ez and H-field components from Ex, Ey using Maxwell's equations."""
        k0 = self.k0
        beta = n_eff * k0
        dx, dy = self.dx, self.dy

        # Compute derivatives of E-field components (all on vertex grid)
        dEx_dx_v = np.zeros_like(Ex_v, dtype=complex)
        dEy_dy_v = np.zeros_like(Ey_v, dtype=complex)
        dEx_dy_v = np.zeros_like(Ex_v, dtype=complex)
        dEy_dx_v = np.zeros_like(Ey_v, dtype=complex)
        
        dEx_dx_v[:,1:-1] = (Ex_v[:,2:] - Ex_v[:,:-2])/(2*dx)
        dEx_dx_v[:,0]    = (Ex_v[:,1] - Ex_v[:,0]) / dx
        dEx_dx_v[:,-1]   = (Ex_v[:,-1] - Ex_v[:,-2]) / dx
        
        dEy_dy_v[1:-1,:] = (Ey_v[2:,:] - Ey_v[:-2,:])/(2*dy)
        dEy_dy_v[0,:]    = (Ey_v[1,:] - Ey_v[0,:]) / dy
        dEy_dy_v[-1,:]   = (Ey_v[-1,:] - Ey_v[-2,:]) / dy
        
        dEx_dy_v[1:-1,:] = (Ex_v[2:,:] - Ex_v[:-2,:])/(2*dy)
        dEx_dy_v[0,:]    = (Ex_v[1,:] - Ex_v[0,:]) / dy
        dEx_dy_v[-1,:]   = (Ex_v[-1,:] - Ex_v[-2,:]) / dy
        
        dEy_dx_v[:,1:-1] = (Ey_v[:,2:] - Ey_v[:,:-2])/(2*dx)
        dEy_dx_v[:,0]    = (Ey_v[:,1] - Ey_v[:,0]) / dx
        dEy_dx_v[:,-1]   = (Ey_v[:,-1] - Ey_v[:,-2]) / dx

        # Compute Ez from divergence constraint
        # Need to interpolate epsilon tensors from centers to vertices
        denom_beta = 1j*beta if abs(beta) > 0 else 1j*(1e-12+0j)
        
        # Interpolate epsilon tensors to vertex grid to match derivative arrays
        epsxx_v = self.centers_to_vertices(epsxx_c)
        epsxy_v = self.centers_to_vertices(epsxy_c)
        epsyx_v = self.centers_to_vertices(epsyx_c)
        epsyy_v = self.centers_to_vertices(epsyy_c)
        epszz_v = self.centers_to_vertices(epszz_c)
        epszz_safe = np.where(np.abs(epszz_v) < 1e-18, 1e-18+0j, epszz_v)
        
        Ez_v = -(epsxx_v*dEx_dx_v + epsxy_v*dEy_dx_v + epsyx_v*dEx_dy_v + epsyy_v*dEy_dy_v)/(denom_beta*epszz_safe)

        # Now compute H-field from H = (1/(iωμ₀))∇×E = (1/(ik₀))∇×E
        # From Maxwell's curl equation: ∇×E = -iωμ₀H = -ik₀H (in vacuum units)
        # So H = (1/(ik₀))(∇×E)
        
        # For 2D mode with propagation in z: Hx = (1/(ik₀))(∂Ez/∂y - iβEy)
        #                                    Hy = (1/(ik₀))(iβEx - ∂Ez/∂x)  
        #                                    Hz = (1/(ik₀))(∂Ey/∂x - ∂Ex/∂y)
        
        # Interpolate E-fields from vertices to centers for consistent grid
        Ex_NW = Ex_v[1:, :-1]; Ex_SW = Ex_v[:-1, :-1]; Ex_SE = Ex_v[:-1, 1:]; Ex_NE = Ex_v[1:, 1:]
        Ey_NW = Ey_v[1:, :-1]; Ey_SW = Ey_v[:-1, :-1]; Ey_SE = Ey_v[:-1, 1:]; Ey_NE = Ey_v[1:, 1:]
        Ez_NW = Ez_v[1:, :-1]; Ez_SW = Ez_v[:-1, :-1]; Ez_SE = Ez_v[:-1, 1:]; Ez_NE = Ez_v[1:, 1:]

        Ex_avg = 0.25*(Ex_NW + Ex_SW + Ex_SE + Ex_NE)
        Ey_avg = 0.25*(Ey_NW + Ey_SW + Ey_SE + Ey_NE)
        
        # Compute derivatives of Ez at cell centers
        dEz_dx = (Ez_NE + Ez_SE - Ez_NW - Ez_SW)/(2*dx)
        dEz_dy = (Ez_NW + Ez_NE - Ez_SW - Ez_SE)/(2*dy)
        
        # Compute H-field components using curl relationship
        denom = 1j*k0 if abs(k0) > 0 else 1j*(1e-12+0j)
        Hx_c = (dEz_dy - 1j*beta*Ey_avg)/denom
        Hy_c = (1j*beta*Ex_avg - dEz_dx)/denom
        
        # For Hz, use derivatives at cell centers
        dEy_dx = (Ey_SE + Ey_NE - Ey_NW - Ey_SW)/(2*dx)
        dEx_dy = (Ex_NW + Ex_NE - Ex_SW - Ex_SE)/(2*dy)
        Hz_c = (dEy_dx - dEx_dy)/denom
        
        return Ez_v, Hx_c, Hy_c, Hz_c

    def confinement(self, mode):
        """Calculate confinement factor using Poynting vector power flow.

        For thin-film modulators: confinement in the top rib waveguide region.
        η_top = ∬_top S_z dA / ∬_Ω S_z dA
        where S_z = (1/2) Re(E_x H_y* - E_y H_x*) is the z-component of Poynting vector
        """
        # Get E fields on vertices
        Ex_v = mode.Ex
        Ey_v = mode.Ey

        # Calculate H fields from E using Maxwell's equations
        k0 = self.k0
        beta = mode.n_eff * k0
        dx, dy = self.dx, self.dy

        # Calculate derivatives for Ez
        dEx_dx_v = np.zeros_like(Ex_v, dtype=complex)
        dEy_dy_v = np.zeros_like(Ey_v, dtype=complex)

        # Central differences for derivatives
        dEx_dx_v[:, 1:-1] = (Ex_v[:, 2:] - Ex_v[:, :-2]) / (2*dx)
        dEx_dx_v[:, 0] = (Ex_v[:, 1] - Ex_v[:, 0]) / dx
        dEx_dx_v[:, -1] = (Ex_v[:, -1] - Ex_v[:, -2]) / dx

        dEy_dy_v[1:-1, :] = (Ey_v[2:, :] - Ey_v[:-2, :]) / (2*dy)
        dEy_dy_v[0, :] = (Ey_v[1, :] - Ey_v[0, :]) / dy
        dEy_dy_v[-1, :] = (Ey_v[-1, :] - Ey_v[-2, :]) / dy

        # Ez from divergence equation
        Ez_v = -1j * (dEx_dx_v + dEy_dy_v) / beta if abs(beta) > 1e-10 else np.zeros_like(Ex_v)

        # Calculate derivatives of Ez
        dEz_dx_v = np.zeros_like(Ez_v, dtype=complex)
        dEz_dy_v = np.zeros_like(Ez_v, dtype=complex)

        dEz_dx_v[:, 1:-1] = (Ez_v[:, 2:] - Ez_v[:, :-2]) / (2*dx)
        dEz_dy_v[1:-1, :] = (Ez_v[2:, :] - Ez_v[:-2, :]) / (2*dy)

        # H field components
        omega_mu0_inv = 1j * k0  # Simplified (assuming μ₀=1 in normalized units)
        Hx_v = (dEz_dy_v - 1j*beta*Ey_v) / omega_mu0_inv
        Hy_v = (1j*beta*Ex_v - dEz_dx_v) / omega_mu0_inv

        # Calculate z-component of Poynting vector
        Sz_v = 0.5 * np.real(Ex_v * np.conj(Hy_v) - Ey_v * np.conj(Hx_v))

        # Create top-rib mask on vertices
        if not hasattr(self, "_sin_mask_vertices"):
            Xc, Yc = np.meshgrid(self.xc, self.yc)
            mask_top_c = self._build_top_core_mask(Xc, Yc)

            # Convert to vertices
            mask_v = np.zeros((self.ny_v, self.nx_v), dtype=bool)
            mask_v[:-1, :-1] = mask_top_c
            mask_v[-1, :-1] = mask_top_c[-1, :]
            mask_v[:-1, -1] = mask_top_c[:, -1]
            mask_v[-1, -1] = mask_top_c[-1, -1]
            self._sin_mask_vertices = mask_v

        sin_mask_v = self._sin_mask_vertices

        # Integrate Poynting flux
        total_power = np.sum(Sz_v)

        # Handle edge cases
        if abs(total_power) < 1e-30:
            return 0.0

        # Power flow through top rib region
        sin_power = np.sum(Sz_v[sin_mask_v])

        # Confinement factor
        confinement = abs(sin_power / total_power)

        # Ensure it's between 0 and 1
        confinement = min(1.0, max(0.0, confinement))

        return float(confinement)

    def classify_mode(self, Ex_v, Ey_v, Ez_v, mnum):
        # Use proper E fields for classification
        # TE modes: Transverse E field (Ex, Ey dominant), minimal Ez
        # TM modes: Significant Ez component
        Px = float(np.sum(np.abs(Ex_v)**2))  # Transverse Ex component
        Py = float(np.sum(np.abs(Ey_v)**2))  # Transverse Ey component  
        Pz = float(np.sum(np.abs(Ez_v)**2))  # Longitudinal Ez component
        P  = Px+Py+Pz if (Px+Py+Pz) > 1e-30 else 1.0
        
        # CORRECTED: TE has transverse E field, TM has longitudinal Ez
        te = (Px+Py)/P  # TE fraction: transverse E field
        tm = Pz/P       # TM fraction: longitudinal E field
        
        if te > 0.8: mtype = f"TE{mnum}"
        elif tm > 0.8: mtype = f"TM{mnum}"
        else: mtype = f"HE{mnum}"
        return float(te), float(tm), mtype

    def is_duplicate(self, n_eff_new, Eint_new, modes):
        for m in modes:
            if (abs(np.real(n_eff_new)-np.real(m.n_eff)) < NEFF_RE_TOL and
                abs(np.imag(n_eff_new)-np.imag(m.n_eff)) < NEFF_IM_TOL):
                return True
        num = float(np.sqrt(np.sum(Eint_new*Eint_new)))
        if num < 1e-30: return False
        for m in modes:
            den = float(np.sqrt(np.sum(m.E_intensity*m.E_intensity)))
            if den < 1e-30: continue
            sim = float(np.sum(Eint_new*m.E_intensity)/(num*den))
            if sim > INT_OVERLAP_THR: return True
        return False

    def solve_modes(self, n_modes=6, n_eff_guess=2.5, eps_override=None, search_multiplier=3):
        if eps_override is None:
            epsxx, epsxy, epsyx, epsyy, epszz, _ = self._ensure_geometry()
        else:
            epsxx, epsxy, epsyx, epsyy, epszz = eps_override[:5]

        A = self.build_q_matrix(epsxx, epsxy, epsyx, epsyy)

        n_clad = max(float(self.n_air.real), float(self.n_sio2.real))
        eps_max_real = max(float(np.max(np.real(epsxx))),
                           float(np.max(np.real(epsyy))),
                           float(np.max(np.real(epszz))))
        n_core = float(np.sqrt(max(eps_max_real, 0.0)))
        if self.verbose:
            print(f"Index window: n_clad={n_clad:.3f}, n_core≈{n_core:.3f}")

        target = (n_eff_guess*self.k0)**2
        try:
            k_eigs = min(max(2*n_modes, n_modes+8), 48)
            vals, vecs = eigs(A, k=k_eigs, sigma=target, which='LM',
                              maxiter=15000, tol=1e-10)
        except Exception:
            if self.verbose:
                print(f"Index window: n_clad={n_clad:.3f}, n_core≈{n_core:.3f}")
            return []

        N = self.nx_v*self.ny_v
        neffs = np.sqrt(vals+0.0j)/self.k0
        # Increase search space to find more modes for better TE0 selection
        search_modes = min(len(neffs), n_modes * search_multiplier)
        order = np.argsort(-np.real(neffs))[:search_modes]

        modes = []
        for idx in order:
            lam = vals[idx]
            if np.real(lam) <= 0: continue
            beta = np.sqrt(lam+0j); n_eff = beta/self.k0
            nre, nim = float(np.real(n_eff)), float(np.imag(n_eff))
            if not (n_clad < nre < n_core+0.1): continue
            if nim > 0.2: continue

            h = vecs[:, idx]
            Ex = h[:N].reshape((self.ny_v, self.nx_v))  # Eigenvectors are E-field components
            Ey = h[N:].reshape((self.ny_v, self.nx_v))  # Eigenvectors are E-field components

            dom = Ex.ravel()[np.argmax(np.abs(Ex))] if np.max(np.abs(Ex)) >= np.max(np.abs(Ey)) \
                  else Ey.ravel()[np.argmax(np.abs(Ey))]
            if abs(dom) > 1e-15:
                s = np.abs(dom)/dom
                Ex *= s; Ey *= s

            p = float(np.sum(np.abs(Ex)**2 + np.abs(Ey)**2))
            if p > 1e-20:
                s = 1.0/np.sqrt(p); Ex *= s; Ey *= s

            Ez, Hx, Hy, Hz = self.postprocess_murphy(Ex, Ey, n_eff, epsxx, epsxy, epsyx, epsyy, epszz)

            E2 = np.abs(Ex)**2 + np.abs(Ey)**2 + np.abs(Ez)**2
            H2 = np.abs(Hx)**2 + np.abs(Hy)**2 + np.abs(Hz)**2

            if self.is_duplicate(n_eff, E2, modes): continue  # Use E-field intensity for duplicate check

            lambda_cm = self.wl*1e-4
            alpha_db_cm = 4.342944819*(2*np.pi/lambda_cm)*np.imag(n_eff)
            
            temp_mode = FullVectorMode(
                n_eff=n_eff, beta=beta,
                Hx=Hx, Hy=Hy, Hz=Hz,
                Ex=Ex, Ey=Ey, Ez=Ez,
                E_intensity=E2, H_intensity=H2,  # Proper field intensities: E2=|E|², H2=|H|²
                confinement_factor=0.0,
                alpha_db_per_cm=float(np.real(alpha_db_cm)),
                mode_type="temp", te_fraction=0.0, tm_fraction=0.0
            )
            
            tef, tmf, mtype = self.classify_mode(Ex, Ey, Ez, len(modes))  # Use proper E fields
            temp_mode.te_fraction = float(tef)
            temp_mode.tm_fraction = float(tmf)
            
            conf = self.confinement(temp_mode)

            modes.append(FullVectorMode(
                n_eff=n_eff, beta=beta,
                Hx=Hx, Hy=Hy, Hz=Hz,
                Ex=Ex, Ey=Ey, Ez=Ez,
                E_intensity=E2, H_intensity=H2,  # Proper field intensities: E2=|E|², H2=|H|²
                confinement_factor=conf,
                alpha_db_per_cm=float(np.real(alpha_db_cm)),
                mode_type=mtype, te_fraction=float(tef), tm_fraction=float(tmf)
            ))
            # Continue until we have enough modes or exhaust search space
            if len(modes) >= n_modes * search_multiplier:
                break

        # Enhanced mode sorting: prioritize fundamental TE0 mode with highest n_eff
        te_modes = [m for m in modes if hasattr(m, 'te_fraction') and m.te_fraction > 0.7]
        tm_modes = [m for m in modes if hasattr(m, 'tm_fraction') and m.tm_fraction > 0.7]
        other_modes = [m for m in modes if m not in te_modes and m not in tm_modes]
        
        # Sort each category by n_eff (descending)
        te_modes.sort(key=lambda m: np.real(m.n_eff), reverse=True)
        tm_modes.sort(key=lambda m: np.real(m.n_eff), reverse=True)
        other_modes.sort(key=lambda m: np.real(m.n_eff), reverse=True)
        
        # Return TE modes first (fundamental TE0 will be first), then TM, then others
        return te_modes + tm_modes + other_modes

    # ---------- EO overlap (same algorithm) ----------
    def compute_eo_overlap_for_modes(self, modes, voltage, E_mag=None, unit_direction='x'):
        """
        Calculate EO overlap factor Γ using simplified field overlap approach.
        
        For BTO devices, Γ should be a dimensionless factor between 0 and 1 representing
        the spatial overlap between the optical mode and the electro-optic effect.
        """
        if E_mag is None:
            _, Ex_dc, Ey_dc, E_mag = self.solve_electrostatic(voltage=voltage)
        
        gammas = []
        mask_centers = self._bto_mask_centers
        
        for mode in modes:
            # Get optical mode intensity - check if it's already on centers or vertices
            if hasattr(mode, 'E_intensity'):
                E_opt_intensity_v = mode.E_intensity
            elif hasattr(mode, 'H_intensity'):
                E_opt_intensity_v = mode.E_intensity  # Use actual E field intensity
            else:
                gammas.append(0.0)
                continue
            
            # Convert to centers if needed
            if E_opt_intensity_v.shape == (self.ny_v, self.nx_v):
                E_opt_intensity = self.vertices_to_centers(E_opt_intensity_v)
            else:
                E_opt_intensity = E_opt_intensity_v
            
            # Ensure dimensions match
            if E_opt_intensity.shape != E_mag.shape:
                print(f"Warning: Shape mismatch - E_opt: {E_opt_intensity.shape}, E_mag: {E_mag.shape}")
                gammas.append(0.0)
                continue
            
            # Get DC field magnitude (already on centers)
            E_dc_magnitude = E_mag
            
            # Calculate overlap in BTO region only
            # Γ = (∬_BTO |E_opt|² * |E_dc| dA) / (∬_total |E_opt|² dA) / |E_dc|_max
            numerator = float(np.sum(E_opt_intensity[mask_centers] * E_dc_magnitude[mask_centers]))
            denominator_opt = float(np.sum(E_opt_intensity))
            denominator_dc = float(np.max(E_dc_magnitude[mask_centers])) if np.any(mask_centers) else 1.0
            
            if denominator_opt > 1e-20 and denominator_dc > 1e-20:
                gamma = numerator / (denominator_opt * denominator_dc)
                # Ensure gamma is reasonable (0 to 1 range) 
                gamma = min(abs(gamma), 1.0)
            else:
                gamma = 0.0
                
            gammas.append(gamma)
            
        return gammas

    def compute_eo_overlap_for_modes_original(self, modes, voltage, E_mag=None, unit_direction='x'):
        """Calculate bounded EO overlap factor Γ ∈ [0,1] following Tao et al."""
        if E_mag is None:
            _, Ex_dc, Ey_dc, E_mag = self.solve_electrostatic(voltage=voltage)
        else:
            _, Ex_dc, Ey_dc, _ = self.solve_electrostatic(voltage=voltage)
            
        mask = getattr(self, "_bto_mask_centers", None)
        if mask is None:
            raise RuntimeError("BTO mask not built yet.")

        if unit_direction.lower() == 'x':
            u_hat = np.array([1.0, 0.0, 0.0])
        elif unit_direction.lower() == 'y':
            u_hat = np.array([0.0, 1.0, 0.0])  
        else:
            u_hat = np.array([1.0, 0.0, 0.0])
            
        max_E_dc_in_bto = np.max(E_mag[mask]) if np.any(mask) else 1.0
        E_dc_norm_mag = E_mag / max_E_dc_in_bto if max_E_dc_in_bto > 0 else E_mag
        
        Ex_dc_norm = Ex_dc / max_E_dc_in_bto if max_E_dc_in_bto > 0 else Ex_dc
        Ey_dc_norm = Ey_dc / max_E_dc_in_bto if max_E_dc_in_bto > 0 else Ey_dc
        u_dot_E_dc = u_hat[0] * Ex_dc_norm + u_hat[1] * Ey_dc_norm

        gammas = []
        for m in modes:
            E2_opt_v = m.E_intensity  # Use actual E field intensity
            
            max_E_opt_all = np.max(E2_opt_v) if np.max(E2_opt_v) > 0 else 1.0
            E2_opt_norm_v = E2_opt_v / max_E_opt_all
            
            u_dot_E_dc_v = self.centers_to_vertices(u_dot_E_dc)
            
            # Ensure both arrays are on the same grid - work on vertices grid
            if E2_opt_norm_v.shape != (self.ny_v, self.nx_v):
                # E2_opt_norm_v is likely on center grid, convert to vertices
                if E2_opt_norm_v.shape == (self.ny_c, self.nx_c):
                    E2_opt_norm_v = self.centers_to_vertices(E2_opt_norm_v)
            
            # Ensure u_dot_E_dc_v is on vertices grid
            if u_dot_E_dc_v.shape != (self.ny_v, self.nx_v):
                # Should not happen, but handle just in case
                u_dot_E_dc_v = self.centers_to_vertices(u_dot_E_dc)
            
            mask_v = getattr(self, "_bto_mask_vertices", None)
            if mask_v is None:
                mask_c = self._bto_mask_centers
                mask_v = np.zeros((self.ny_v, self.nx_v), dtype=bool)
                if mask_c.shape == (self.ny_c, self.nx_c):
                    mask_v[:-1, :-1] = mask_c
                    mask_v[-1, :-1] = mask_c[-1, :]
                    mask_v[:-1, -1] = mask_c[:, -1]
                    mask_v[-1, -1] = mask_c[-1, -1]
                else:
                    # Fallback: use centers mask on vertices
                    mask_v = self.centers_to_vertices(mask_c.astype(float)) > 0.5
                self._bto_mask_vertices = mask_v
            
            # Now both arrays should be on vertices grid
            mask_to_use = mask_v
            overlap_integrand = u_dot_E_dc_v * E2_opt_norm_v
            
            numerator = float(np.sum(overlap_integrand[mask_to_use]))
            denominator = float(np.sum(E2_opt_norm_v))
            
            if denominator <= 0:
                gamma = 0.0
            else:
                gamma = numerator / denominator
                gamma = max(0.0, min(1.0, abs(gamma)))
                
            gammas.append(gamma)
                
        return gammas

    # ========== Mode Analysis Tools ==========
    def analyze_all_modes(self, n_modes=8, n_eff_guess=2.5, voltage=0.0, show_plots=True):
        """
        Comprehensive analysis of all found modes to identify which is fundamental.
        
        Args:
            n_modes: Number of modes to find
            n_eff_guess: Initial guess for mode search
            voltage: Applied voltage (0 for base modes, >0 for EO modes)
            show_plots: Whether to show mode field plots
            
        Returns:
            List of modes with detailed analysis
        """
        if voltage == 0.0:
            print(f"\n=== BASE MODES ANALYSIS (V=0V) ===")
            modes = self.solve_modes(n_modes=n_modes, n_eff_guess=n_eff_guess)
        else:
            print(f"\n=== EO MODES ANALYSIS (V={voltage}V) ===")
            modes = self.solve_modes_with_eo(voltage=voltage, n_modes=n_modes, n_eff_guess=n_eff_guess)
        
        if not modes:
            print("No modes found!")
            return []
            
        print(f"Found {len(modes)} modes:")
        print(f"{'Mode':<4} {'n_eff':<12} {'Loss(dB/cm)':<12} {'Type':<8} {'TopConf':<12} {'TE%':<6} {'TM%':<6}")
        print("-" * 70)
        
        for i, mode in enumerate(modes):
            te_pct = mode.te_fraction * 100
            tm_pct = mode.tm_fraction * 100
            print(f"{i:<4} {mode.n_eff.real:<12.6f} {mode.alpha_db_per_cm:<12.3f} {mode.mode_type:<8} "
                  f"{mode.confinement_factor:<12.3f} {te_pct:<6.1f} {tm_pct:<6.1f}")
        
        # Analysis of fundamental mode characteristics
        if len(modes) > 0:
            fund_mode = modes[0]  # Highest n_eff
            print(f"\n=== FUNDAMENTAL MODE CHARACTERISTICS ===")
            print(f"Effective Index: {fund_mode.n_eff.real:.6f}")
            print(f"Top-core Confinement: {fund_mode.confinement_factor:.3f}")
            print(f"Mode Type: {fund_mode.mode_type}")
            print(f"TE Fraction: {fund_mode.te_fraction:.3f}")
            print(f"TM Fraction: {fund_mode.tm_fraction:.3f}")
            
            # Check if this is truly the fundamental mode
            peak_intensity = np.max(fund_mode.H_intensity)
            center_x_idx = len(fund_mode.Hx[0]) // 2
            center_y_idx = len(fund_mode.Hx) // 2
            center_intensity = fund_mode.H_intensity[center_y_idx, center_x_idx]
            
            print(f"Peak Intensity: {peak_intensity:.3e}")
            print(f"Center Intensity: {center_intensity:.3e}")
            print(f"Center/Peak Ratio: {center_intensity/peak_intensity:.3f}")
            
            if fund_mode.confinement_factor < 0.1:
                print("WARNING: Low top-core confinement - mode may not be strongly hybridized with top rib")
            if center_intensity/peak_intensity < 0.5:
                print("WARNING: Mode not centered - might be higher order")
        
        # Plot all modes if requested
        if show_plots and len(modes) > 0:
            self.plot_all_modes(modes[:min(6, len(modes))])
            
        return modes
    
    def plot_all_modes(self, modes):
        """Plot intensity patterns for all modes to identify fundamental."""
        n_modes = len(modes)
        cols = min(3, n_modes)
        rows = (n_modes + cols - 1) // cols

        fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 4 * rows))
        axes = np.atleast_1d(axes).ravel()

        # H_intensity is on center grid
        Xc, Yc = self.get_center_mesh()

        for i, mode in enumerate(modes):
            ax = axes[i]

            im = ax.contourf(Xc, Yc, mode.H_intensity, levels=20, cmap='rainbow')
            self.add_geometry_overlay(ax, color=OUTLINE_COLOR, alpha=0.8)

            n_eff_str = f"{mode.n_eff.real:.4f}"
            if mode.n_eff.imag != 0:
                n_eff_str += f" + {mode.n_eff.imag:.1e}j"
            loss_str = f"{mode.alpha_db_per_cm:.2f}"

            ax.set_title(
                f"Mode {i}: n_eff = {n_eff_str}\n"
                f"Loss = {loss_str} dB/cm, Conf = {mode.confinement_factor:.3f}",
                fontsize=10
            )
            ax.set_xlabel('x (μm)', fontsize=9)
            ax.set_ylabel('y (μm)', fontsize=9)
            ax.set_aspect('equal')
            cbar = plt.colorbar(im, ax=ax, shrink=0.6)
            cbar.ax.tick_params(labelsize=8)

        for j in range(n_modes, len(axes)):
            axes[j].set_visible(False)

        plt.tight_layout()
        plt.show()
    
    def find_fundamental_mode_index(self, modes):
        """
        Identify which mode is truly fundamental based on:
        1. High BTO confinement
        2. Centered intensity pattern
        3. Appropriate effective index
        """
        if not modes:
            return 0
            
        best_idx = 0
        best_score = -1
        
        for i, mode in enumerate(modes):
            # Score based on multiple criteria
            confinement_score = mode.confinement_factor  # Higher is better
            
            # Center concentration score
            # Use the actual top-core center rather than the whole-domain center.
            _, top_left, top_right = self._top_core_x_bounds()
            target_x = 0.5 * (top_left + top_right)
            target_y = 0.5 * (self._layer_positions()['top_bot'] + self._layer_positions()['top_top'])
            center_x_idx = int(np.argmin(np.abs(self.xc - target_x))) if hasattr(self, 'xc') else len(mode.Hx[0]) // 2
            center_y_idx = int(np.argmin(np.abs(self.yc - target_y))) if hasattr(self, 'yc') else len(mode.Hx) // 2
            peak_intensity = np.max(mode.H_intensity)
            center_intensity = mode.H_intensity[center_y_idx, center_x_idx]
            center_score = center_intensity / peak_intensity if peak_intensity > 0 else 0
            
            # Combined score
            total_score = confinement_score * 0.7 + center_score * 0.3
            
            if total_score > best_score:
                best_score = total_score
                best_idx = i
                
        return best_idx

    # ========== All analysis methods (same as original) ==========
    def extract_numerical_r_eff(self, voltage=3.0, n_eff_guess=2.5):
        """
        Extract r_eff numerically using proper fundamental mode selection.
        Uses consistent n_eff_guess and validates mode selection.
        """
        gap_m = self.electrode_gap * 1e-6  # Convert μm to m
        
        # Solve base modes with more modes to ensure we get fundamental
        modes_base = self.solve_modes(n_modes=4, n_eff_guess=n_eff_guess)
        if not modes_base:
            return {'r_eff': 0.0, 'delta_n': 0.0, 'n_eff_base': 0.0, 'n_eff_eo': 0.0, 'gamma': 0.0}
            
        # Find true fundamental mode (highest confinement + centered)
        fund_idx_base = self.find_fundamental_mode_index(modes_base)
        mode_base = modes_base[fund_idx_base]
        
        # Solve EO modes with SAME n_eff_guess
        modes_eo = self.solve_modes_with_eo(voltage=voltage, n_modes=4, n_eff_guess=n_eff_guess)
        if not modes_eo:
            return {'r_eff': 0.0, 'delta_n': 0.0, 'n_eff_base': mode_base.n_eff.real, 'n_eff_eo': 0.0, 'gamma': 0.0}
            
        # Find corresponding fundamental mode
        fund_idx_eo = self.find_fundamental_mode_index(modes_eo)  
        mode_eo = modes_eo[fund_idx_eo]
        
        n_eff_base = np.real(mode_base.n_eff)
        n_eff_eo = np.real(mode_eo.n_eff)
        delta_n = n_eff_eo - n_eff_base
        
        gamma_list = self.compute_eo_overlap_for_modes([mode_eo], voltage)
        gamma = gamma_list[0] if gamma_list else 0.0
        
        if abs(voltage) > 1e-12 and abs(delta_n) > 1e-15 and abs(gamma) > 1e-15:
            # Include Γ in r_eff extraction to get true r_eff (not r_eff * Γ)
            r_eff_numerical = 2.0 * abs(delta_n) * gap_m / (n_eff_base**3 * abs(gamma) * abs(voltage))
        else:
            r_eff_numerical = 0.0
            
        return {
            'r_eff': r_eff_numerical,
            'delta_n': delta_n,
            'n_eff_base': n_eff_base,
            'n_eff_eo': n_eff_eo,
            'gamma': gamma,
            'voltage': voltage,
            'gap_m': gap_m,
            'mode_base_idx': fund_idx_base,
            'mode_eo_idx': fund_idx_eo,
            'base_confinement': mode_base.confinement_factor,
            'eo_confinement': mode_eo.confinement_factor
        }

    def extract_key_metrics(self, voltage=3.0, n_modes=4, n_eff_guess=2.1):
        """
        Run one base + EO evaluation and return compact metrics only.
        No plots.
        """
        modes_base = self.solve_modes(n_modes=n_modes, n_eff_guess=n_eff_guess)
        if not modes_base:
            return {
                "success": False,
                "reason": "no base modes",
            }

        idx_base = self.find_fundamental_mode_index(modes_base)
        m_base = modes_base[idx_base]

        modes_eo = self.solve_modes_with_eo(voltage=voltage, n_modes=n_modes, n_eff_guess=n_eff_guess)
        if not modes_eo:
            return {
                "success": False,
                "reason": "no EO modes",
                "n_eff_base": float(np.real(m_base.n_eff)),
                "loss_base_db_cm": float(m_base.alpha_db_per_cm),
                "top_conf_base": float(m_base.confinement_factor),
                "mode_type_base": m_base.mode_type,
            }

        idx_eo = self.find_fundamental_mode_index(modes_eo)
        m_eo = modes_eo[idx_eo]

        gamma_list = self.compute_eo_overlap_for_modes([m_eo], voltage)
        gamma = float(gamma_list[0]) if gamma_list else 0.0

        n_eff_base = float(np.real(m_base.n_eff))
        n_eff_eo = float(np.real(m_eo.n_eff))
        delta_n = n_eff_eo - n_eff_base

        return {
            "success": True,
            "reason": "",
            "mode_type_base": m_base.mode_type,
            "mode_type_eo": m_eo.mode_type,
            "n_eff_base": n_eff_base,
            "n_eff_eo": n_eff_eo,
            "delta_n": delta_n,
            "loss_base_db_cm": float(m_base.alpha_db_per_cm),
            "loss_eo_db_cm": float(m_eo.alpha_db_per_cm),
            "top_conf_base": float(m_base.confinement_factor),
            "top_conf_eo": float(m_eo.confinement_factor),
            "te_frac_base": float(m_base.te_fraction),
            "tm_frac_base": float(m_base.tm_fraction),
            "gamma": gamma,
            "fund_idx_base": int(idx_base),
            "fund_idx_eo": int(idx_eo),
        }

    def check_geometry_consistency(self):
        """Check if the flat thin film geometry is properly constructed."""
        print("\n=== GEOMETRY CONSISTENCY CHECK ===")

        pos = self._layer_positions()
        top_label = self.top_core_material.upper()
        spacer_label = self.spacer_material.lower()
        n_top = self._get_top_core_index().real
        n_spacer = self._get_spacer_index().real

        print(f"Layer Stack (bottom to top):")
        print(f"  SiO2 substrate: 0.000 → {self.oxide_thickness:.3f}μm")
        print(f"  Al2O3 buffer:   {pos['al2o3_bot']:.3f} → {pos['al2o3_top']:.3f}μm ({self.al2o3_thickness*1000:.1f}nm)")
        print(f"  BTO thin film:  {pos['bto_bot']:.3f} → {pos['bto_top']:.3f}μm ({self.bto_thickness*1000:.1f}nm)")
        if self.spacer_thickness > 0:
            print(f"  Spacer layer:   {pos['spacer_bot']:.3f} → {pos['spacer_top']:.3f}μm ({spacer_label}, {self.spacer_thickness*1000:.1f}nm)")
        else:
            print("  Spacer layer:   disabled (direct-contact case)")
        print(f"  Top waveguide:  {pos['top_bot']:.3f} → {pos['top_top']:.3f}μm ({top_label}, {self.sin_rib_width:.3f}μm × {self.sin_rib_height*1000:.1f}nm)")
        print(f"  Electrodes:     {pos['electrode_bot']:.3f} → {pos['electrode_top']:.3f}μm (gap={self.electrode_gap}μm)")

        if abs(pos['electrode_bot'] - pos['bto_top']) > 1e-12:
            print(f"WARNING: Electrodes not on BTO! Gap = {pos['electrode_bot'] - pos['bto_top']:.3f}μm")

        print(f"\nMaterial Properties:")
        print(f"  SiO2: n = {self.n_sio2.real:.3f}")
        print(f"  Al2O3: n = {self.n_al2o3.real:.3f}")
        print(f"  Water proxy: n = {self.n_water.real:.3f}")
        print(f"  BTO ordinary: n = {self.n_bto_o.real:.3f}")
        print(f"  BTO extraordinary: n = {self.n_bto_e.real:.3f}")
        print(f"  Top core ({top_label}): n = {n_top:.3f}")
        print(f"  Spacer ({spacer_label}): n = {n_spacer:.3f}")

        top_bto_contrast = abs(n_top - self.n_bto_o.real)
        bto_sio2_contrast = abs(self.n_bto_o.real - self.n_sio2.real)

        print(f"\nIndex Contrasts:")
        print(f"  Top core/BTO: Δn = {top_bto_contrast:.3f}")
        print(f"  BTO/SiO2:     Δn = {bto_sio2_contrast:.3f}")

        if top_bto_contrast < 0.5:
            print("WARNING: Low top-core/BTO contrast may cause weak confinement")
        if n_top < self.n_bto_o.real:
            print("WARNING: Top-core index < BTO index - mode may be pulled toward BTO")
        if self.spacer_thickness > 0:
            print("NOTE: Top core is separated from BTO by a spacer.")

    def voltage_sweep_analysis(self, voltage_range=None, n_modes=4, mode_index=0):
        if voltage_range is None:
            voltage_range = (0.1, 5.0)
            
        v_min = max(voltage_range[0], 0.05)
        voltages = np.linspace(v_min, voltage_range[1], 21)
        
        n_effs = []
        gammas = []
        confinements = []
        alphas = []
        
        original_orientation = self.orientation
        self.orientation = "a-axis"
        
        print(f"Voltage sweep: {v_min}V to {voltage_range[1]}V (a-axis orientation)")
        
        for i, V in enumerate(voltages):
            if i % 5 == 0:
                print(f"  Processing V = {V:.1f}V...")
                
            modes = self.solve_modes_with_eo(voltage=V, n_modes=n_modes)
            
            if len(modes) > mode_index:
                mode = modes[mode_index]
                n_effs.append(mode.n_eff)
                confinements.append(mode.confinement_factor)
                alphas.append(mode.alpha_db_per_cm)
                
                gamma_list = self.compute_eo_overlap_for_modes([mode], V)
                gammas.append(gamma_list[0] if gamma_list else 0.0)
            else:
                n_effs.append(np.nan + 1j*np.nan)
                confinements.append(0.0)
                alphas.append(0.0)
                gammas.append(0.0)
        
        self.orientation = original_orientation
        
        return {
            'voltages': voltages,
            'n_effs': np.array(n_effs),
            'gammas': np.array(gammas),
            'confinements': np.array(confinements),
            'alphas': np.array(alphas)
        }

    def crystal_angle_sweep_analysis(self, phi_range=None, n_modes=4, mode_index=0, voltage=3.0):
        if phi_range is None:
            phi_range = (0.0, 90.0)
            
        phi_angles = np.linspace(phi_range[0], phi_range[1], 11)
        
        n_effs = []
        gammas = []
        confinements = []
        r_effs = []
        
        original_phi = self.phi_deg
        
        print(f"Crystal angle sweep: {phi_range[0]}° to {phi_range[1]}°")
        
        for i, phi in enumerate(phi_angles):
            if i % 4 == 0:
                print(f"  Processing φ = {phi:.1f}°...")
                
            self.phi_deg = phi
            self.invalidate_geometry_cache()
            
            modes = self.solve_modes_with_eo(voltage=voltage, n_modes=n_modes)
            
            if len(modes) > mode_index:
                mode = modes[mode_index]
                n_eff_real = np.real(mode.n_eff)
                n_effs.append(mode.n_eff)
                confinements.append(mode.confinement_factor)
                
                r_eff_data = self.extract_numerical_r_eff(voltage=voltage)
                r_effs.append(r_eff_data['r_eff'])
                
                gamma_list = self.compute_eo_overlap_for_modes([mode], voltage)
                gammas.append(gamma_list[0] if gamma_list else 0.0)
            else:
                n_effs.append(np.nan + 1j*np.nan)
                r_effs.append(np.nan)
                confinements.append(0.0)
                gammas.append(0.0)
        
        self.phi_deg = original_phi
        self.invalidate_geometry_cache()
        
        return {
            'phi_angles': phi_angles,
            'n_effs': np.array(n_effs),
            'r_effs': np.array(r_effs),
            'gammas': np.array(gammas),
            'confinements': np.array(confinements)
        }

    def solve_modes_with_eo(self, voltage=3.0, n_modes=4, n_eff_guess=2.5):
        _, Ex_dc, Ey_dc, E_mag = self.solve_electrostatic(voltage=voltage)
        
        epsxx, epsxy, epsyx, epsyy, epszz, _ = self.eps_with_pockels(
            Ex_dc, Ey_dc, Ez_c=None,
            orientation=self.orientation,
            phi_deg=self.phi_deg,
            tilt_deg=self.tilt_deg
        )
        
        eps_override = (epsxx, epsxy, epsyx, epsyy, epszz)
        modes = self.solve_modes(n_modes=n_modes, n_eff_guess=n_eff_guess, 
                               eps_override=eps_override)
        
        return modes

    def plot_bto_index_distribution(self, voltage=3.0, mode=None):
        """Plot the refractive index distribution in the BTO flat thin film layer.
        
        Following Lumerical methodology:
        1. Calculate perturbed permittivity tensor at each point
        2. Diagonalize to get principal refractive indices
        3. Show spatial variation correctly using H field (E field) weighting
        """
        # Get DC electric field
        _, Ex_dc, Ey_dc, E_mag = self.solve_electrostatic(voltage=voltage)
        
        # Get base and EO-perturbed permittivity
        epsxx_base, epsxy_base, epsyx_base, epsyy_base, epszz_base, _ = self._ensure_geometry()
        epsxx_eo, epsxy_eo, epsyx_eo, epsyy_eo, epszz_eo, _ = self.eps_with_pockels(
            Ex_dc, Ey_dc, orientation=self.orientation, 
            phi_deg=self.phi_deg, tilt_deg=self.tilt_deg
        )
        
        Xc, Yc = np.meshgrid(self.xc, self.yc, indexing='xy')
        
        # Calculate principal refractive indices following Lumerical methodology
        n_base_principal = np.zeros_like(epsxx_base, dtype=complex)
        n_eo_principal = np.zeros_like(epsxx_base, dtype=complex)
        dn_eo = np.zeros_like(epsxx_base, dtype=float)
        
        mask = self._bto_mask_centers
        rr, cc = np.where(mask)
        
        for i, j in zip(rr, cc):
            # Base permittivity tensor
            eps_base = np.array([[epsxx_base[i,j], epsxy_base[i,j], 0],
                                [epsyx_base[i,j], epsyy_base[i,j], 0], 
                                [0, 0, epszz_base[i,j]]], dtype=complex)
            
            # EO perturbed permittivity tensor 
            eps_eo = np.array([[epsxx_eo[i,j], epsxy_eo[i,j], 0],
                              [epsyx_eo[i,j], epsyy_eo[i,j], 0],
                              [0, 0, epszz_eo[i,j]]], dtype=complex)
            
            # Diagonalize to get principal values (following Lumerical Step 5)
            try:
                eigvals_base = np.linalg.eigvals(eps_base)
                eigvals_eo = np.linalg.eigvals(eps_eo)
                
                # Take the dominant eigenvalue (largest real part)
                idx_base = np.argmax(np.real(eigvals_base))
                idx_eo = np.argmax(np.real(eigvals_eo))
                
                n_base_principal[i,j] = np.sqrt(eigvals_base[idx_base])
                n_eo_principal[i,j] = np.sqrt(eigvals_eo[idx_eo])
                
                dn_eo[i,j] = np.real(n_eo_principal[i,j] - n_base_principal[i,j])
                
            except np.linalg.LinAlgError:
                # Fallback to trace if eigenvalue fails
                n_base_principal[i,j] = np.sqrt(np.trace(eps_base)/3.0)
                n_eo_principal[i,j] = np.sqrt(np.trace(eps_eo)/3.0) 
                dn_eo[i,j] = np.real(n_eo_principal[i,j] - n_base_principal[i,j])
        
        # If mode provided, weight the index change by E field intensity
        if mode is not None:
            # Get E field intensity on proper grid
            if hasattr(mode, 'E_intensity'):
                E_intensity_v = mode.E_intensity
            elif hasattr(mode, 'H_intensity'):
                E_intensity_v = mode.E_intensity  # Use actual E field intensity
            else:
                E_intensity_v = None
            
            if E_intensity_v is not None:
                # Convert to centers if needed
                if E_intensity_v.shape == (self.ny_v, self.nx_v):
                    E_intensity_centers = self.vertices_to_centers(E_intensity_v)
                else:
                    E_intensity_centers = E_intensity_v
                
                # Ensure dimensions match
                if E_intensity_centers.shape == dn_eo.shape:
                    # Weight the index change by field intensity
                    dn_eo_weighted = dn_eo * (E_intensity_centers / np.max(E_intensity_centers))
                else:
                    print(f"Warning: Shape mismatch in weighting - E_intensity: {E_intensity_centers.shape}, dn_eo: {dn_eo.shape}")
                    dn_eo_weighted = dn_eo
            else:
                dn_eo_weighted = dn_eo
        else:
            dn_eo_weighted = dn_eo
        
        # Only plot delta n - same size as electrostatic field graphs
        fig, ax = plt.subplots(1, 1, figsize=(6, 5))
        fig.suptitle(f'BTO Index Change Analysis (V={voltage}V)', fontsize=16)
        
        dn_masked = np.where(mask, dn_eo_weighted, np.nan)
        finite_vals = dn_masked[~np.isnan(dn_masked)]
        if finite_vals.size > 0:
            p = np.nanpercentile(finite_vals, [2, 98])
            a = float(max(abs(p[0]), abs(p[1])))
            if a <= 0:
                a = float(np.nanmax(np.abs(finite_vals)))
            if a > 0:
                levels_dn = np.linspace(-a, a, 41)
                im = ax.contourf(Xc, Yc, dn_masked, levels=levels_dn, cmap='RdBu_r', extend='both')
            else:
                im = ax.contourf(Xc, Yc, dn_masked, levels=30, cmap='RdBu_r')
        else:
            im = ax.contourf(Xc, Yc, dn_masked, levels=30, cmap='RdBu_r')
        
        # BLACK outline for geometry
        self.add_geometry_overlay(ax, color='black')
        cbar = plt.colorbar(im, ax=ax, shrink=0.8, format='%.2e')
        cbar.set_label('Δn_EO')
        ax.set_title('BTO Index Change Δn_EO')
        ax.set_xlabel('x (μm)')
        ax.set_ylabel('y (μm)')
        ax.set_aspect('equal')
        
        # Match bounds with mode graph
        ax.set_xlim(0, self.domain_width)
        ax.set_ylim(0, self.domain_height)
        
        plt.tight_layout()
        plt.show()
        
        # Print key statistics
        print(f"BTO Index Distribution Analysis:")
        print(f"  Max Δn_EO: {np.nanmax(np.abs(dn_masked)):.2e}")
        print(f"  BTO region analyzed: {np.sum(~np.isnan(dn_masked))} grid points")

    def plot_bto_index_distribution_raw(self, voltage=3.0):
        _, Ex_dc, Ey_dc, _ = self.solve_electrostatic(voltage=voltage)
        epsxx_base, epsxy_base, epsyx_base, epsyy_base, epszz_base, _ = self._ensure_geometry()
        epsxx_eo, epsxy_eo, epsyx_eo, epsyy_eo, epszz_eo, _ = self.eps_with_pockels(
            Ex_dc, Ey_dc, orientation=self.orientation, phi_deg=self.phi_deg, tilt_deg=self.tilt_deg
        )
        Xc, Yc = np.meshgrid(self.xc, self.yc, indexing="xy")
        dn_eo = np.zeros_like(epsxx_base, dtype=float)
        mask = self._bto_mask_centers
        rr, cc = np.where(mask)
        for i, j in zip(rr, cc):
            eps_base = np.array([[epsxx_base[i, j], epsxy_base[i, j], 0],
                                 [epsyx_base[i, j], epsyy_base[i, j], 0],
                                 [0, 0, epszz_base[i, j]]], dtype=complex)
            eps_eo = np.array([[epsxx_eo[i, j], epsxy_eo[i, j], 0],
                               [epsyx_eo[i, j], epsyy_eo[i, j], 0],
                               [0, 0, epszz_eo[i, j]]], dtype=complex)
            try:
                eigvals_base = np.linalg.eigvals(eps_base)
                eigvals_eo = np.linalg.eigvals(eps_eo)
                idx_base = np.argmax(np.real(eigvals_base))
                idx_eo = np.argmax(np.real(eigvals_eo))
                n_base = np.sqrt(eigvals_base[idx_base])
                n_eo = np.sqrt(eigvals_eo[idx_eo])
                dn_eo[i, j] = np.real(n_eo - n_base)
            except np.linalg.LinAlgError:
                n_base = np.sqrt(np.trace(eps_base) / 3.0)
                n_eo = np.sqrt(np.trace(eps_eo) / 3.0)
                dn_eo[i, j] = np.real(n_eo - n_base)
        fig, ax = plt.subplots(1, 1, figsize=(6, 5))
        fig.suptitle(f"Raw EO Index Change in BTO (V={voltage}V)", fontsize=16)
        dn_masked = np.where(mask, dn_eo, np.nan)
        finite_vals = dn_masked[~np.isnan(dn_masked)]
        if finite_vals.size > 0:
            p = np.nanpercentile(finite_vals, [2, 98])
            a = float(max(abs(p[0]), abs(p[1])))
            if a <= 0:
                a = float(np.nanmax(np.abs(finite_vals)))
            if a > 0:
                levels_dn = np.linspace(-a, a, 41)
                im = ax.contourf(Xc, Yc, dn_masked, levels=levels_dn, cmap="RdBu_r", extend="both")
            else:
                im = ax.contourf(Xc, Yc, dn_masked, levels=30, cmap="RdBu_r")
        else:
            im = ax.contourf(Xc, Yc, dn_masked, levels=30, cmap="RdBu_r")
        self.add_geometry_overlay(ax, color="black")
        cbar = plt.colorbar(im, ax=ax, shrink=0.8, format="%.2e")
        cbar.set_label("Δn_EO,raw")
        ax.set_title("Raw Δn_EO map")
        ax.set_xlabel("x (μm)")
        ax.set_ylabel("y (μm)")
        ax.set_aspect("equal")
        ax.set_xlim(0, self.domain_width)
        ax.set_ylim(0, self.domain_height)
        plt.tight_layout()
        plt.show()

    # ========== Plotting methods (same as original) ==========
    def plot_voltage_sweep_results(self, sweep_data):
        """
        Plot individual voltage sweep analysis results.
        
        This function creates separate plots for each voltage-dependent parameter
        to analyze the electro-optic response of the BTO thin film waveguide.
        
        Parameters:
        -----------
        sweep_data : dict
            Dictionary containing voltage sweep results with keys:
            'voltages', 'n_effs', 'gammas', 'confinements'
            
        Plots:
        ------
        - Δn_eff vs voltage: Mode effective index change
        - EO overlap vs voltage: Electro-optic overlap factor Γ  
        - Confinement vs voltage: Mode confinement in BTO layer
        """
        voltages = sweep_data['voltages']
        n_effs = sweep_data['n_effs']
        gammas = sweep_data['gammas']
        confinements = sweep_data['confinements']
        
        # Δn_eff vs voltage
        n_eff_0 = np.real(n_effs[0]) if not np.isnan(np.real(n_effs[0])) else np.real(n_effs[1])
        dn_eff = np.real(n_effs) - n_eff_0
        
        fig, ax = plt.subplots(1, 1, figsize=(6, 5))
        ax.plot(voltages, dn_eff*1e6, 'g-', linewidth=2)
        ax.set_xlabel('Voltage (V)')
        ax.set_ylabel('Δn_eff (×10⁻⁶)')
        ax.set_title('Mode Index Change vs Voltage')
        ax.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.show()
        
        # EO Overlap vs voltage
        fig, ax = plt.subplots(1, 1, figsize=(6, 5))
        ax.plot(voltages, gammas, 'b-', linewidth=2)
        ax.set_xlabel('Voltage (V)')
        ax.set_ylabel('EO Overlap Factor Γ')
        ax.set_title('Electro-Optic Overlap vs Voltage')
        ax.grid(True, alpha=0.3)
        if np.max(gammas) > 0:
            ax.set_ylim([0, np.max(gammas)*1.1])
        plt.tight_layout()
        plt.show()
        
        # Confinement factor vs voltage
        fig, ax = plt.subplots(1, 1, figsize=(6, 5))
        ax.plot(voltages, confinements, 'm-', linewidth=2)
        ax.set_xlabel('Voltage (V)')
        ax.set_ylabel('Confinement Factor')
        ax.set_title('Mode Confinement vs Voltage')
        ax.grid(True, alpha=0.3)
        ax.set_ylim([0, 1])
        plt.tight_layout()
        plt.show()

    def plot_angle_sweep_results(self, sweep_data):
        """
        Plot individual crystal angle sweep analysis results.
        
        This function creates separate plots for each crystal orientation-dependent 
        parameter to analyze the electro-optic tensor effects in BTO.
        
        Parameters:
        -----------
        sweep_data : dict
            Dictionary containing angle sweep results with keys:
            'phi_angles', 'n_effs', 'r_effs', 'gammas', 'confinements'
            
        Plots:
        ------
        - r_eff vs crystal orientation: Purely numerical Pockels coefficient
        - V_π vs crystal orientation: Half-wave voltage with minimum marked
        - EO overlap vs crystal angle: Electro-optic overlap factor
        """
        phi_angles = sweep_data['phi_angles']
        n_effs = sweep_data['n_effs']
        r_effs = sweep_data['r_effs']
        gammas = sweep_data['gammas']
        confinements = sweep_data['confinements']
        
        # Calculate V_π values
        wavelength = 1.55e-6
        device_length = 10e-3
        gap_m = self.electrode_gap * 1e-6
        
        vpis = []
        for i, (n_eff, r_eff, gamma) in enumerate(zip(n_effs, r_effs, gammas)):
            if np.isnan(n_eff) or np.isnan(r_eff) or gamma <= 0 or r_eff <= 0:
                vpis.append(np.nan)
            else:
                n_eff_real = np.real(n_eff)
                vpi = wavelength * gap_m / (n_eff_real**3 * r_eff * gamma * device_length)
                vpis.append(vpi)
        vpis = np.array(vpis)
        
        # r_eff vs crystal orientation - PURELY NUMERICAL
        fig, ax = plt.subplots(1, 1, figsize=(6, 5))
        ax.plot(phi_angles, np.array(r_effs)*1e12, 'k-', linewidth=2)
        ax.set_xlabel('Crystal Angle φ (degrees)')
        ax.set_ylabel('r_eff (pm/V)')
        ax.set_title('Pockels Coefficient vs Crystal Orientation (Numerical)')
        ax.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.show()
        
        # V_π vs crystal orientation
        fig, ax = plt.subplots(1, 1, figsize=(6, 5))
        ax.plot(phi_angles, vpis, 'm-', linewidth=2)
        ax.set_xlabel('Crystal Angle φ (degrees)')
        ax.set_ylabel('V_π (V)')
        ax.set_title('Half-Wave Voltage vs Crystal Orientation')
        ax.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.show()
        
        # EO Overlap vs crystal angle
        fig, ax = plt.subplots(1, 1, figsize=(6, 5))
        ax.plot(phi_angles, gammas, 'g-', linewidth=2)
        ax.set_xlabel('Crystal Angle φ (degrees)')
        ax.set_ylabel('EO Overlap Factor Γ')
        ax.set_title('Electro-Optic Overlap vs Crystal Angle')
        ax.grid(True, alpha=0.3)
        if np.max(gammas) > 0:
            ax.set_ylim([0, np.max(gammas)*1.1])
        plt.tight_layout()
        plt.show()
        
        print(f"\nCrystal Angle Sweep Results ({self.orientation}, Flat Thin Film):")
        print(f"  Analysis complete. Results plotted individually.")
        
        valid_vpis = vpis[~np.isnan(vpis)]
        if len(valid_vpis) > 0:
            min_vpi_idx = np.nanargmin(vpis)
            min_vpi_angle = phi_angles[min_vpi_idx]
            min_vpi_value = vpis[min_vpi_idx]
            print(f"  Minimum V_π = {min_vpi_value:.1f}V at φ = {min_vpi_angle:.1f}° (optimal for switching)")
            print(f"  V_π range: {np.nanmin(vpis):.1f}V - {np.nanmax(vpis):.1f}V")

    def _get_material_display_style(self, material_name):
        """Internal plotting colours for geometry overlays only."""
        mat = str(material_name).lower()
        styles = {
            "air":   (None, 0.0),
            "sio2":  ("#8ecae6", 0.20),
            "al2o3": ("#cdb4db", 0.22),
            "sin":   ("#52b788", 0.22),
            "water": ("#4dabf7", 0.18),
            "bto":   ("#f4a261", 0.10),
        }
        return styles.get(mat, ("#bdbdbd", 0.18))

    def _fill_rect(self, ax, x0, x1, y0, y1, facecolor, alpha, zorder=1):
        """Lightweight rectangle fill helper used only by plotting overlays."""
        if facecolor is None or alpha <= 0:
            return
        if x1 <= x0 or y1 <= y0:
            return
        ax.fill(
            [x0, x1, x1, x0],
            [y0, y0, y1, y1],
            facecolor=facecolor,
            edgecolor='none',
            alpha=alpha,
            zorder=zorder,
        )

    def add_geometry_overlay(self, ax, color=OUTLINE_COLOR, alpha=0.9, lw=1.2):
        """Add outlines of the flat-stack geometry with material-aware fills for plotting."""
        cx = self.domain_width / 2
        pos = self._layer_positions()
        gap_half = 0.5 * self.electrode_gap
        e_bot = pos['electrode_bot']
        e_top = pos['electrode_top']
        _, top_left, top_right = self._top_core_x_bounds()

        # Light material fills first so non-air spacers are not visually mistaken for air gaps.
        bto_color, bto_alpha = self._get_material_display_style('bto')
        self._fill_rect(ax, 0.0, self.domain_width, pos['bto_bot'], pos['bto_top'], bto_color, bto_alpha, zorder=1)

        if self.spacer_thickness > 0:
            spacer_color, spacer_alpha = self._get_material_display_style(self.spacer_material)
            self._fill_rect(
                ax,
                0.0,
                self.domain_width,
                pos['spacer_bot'],
                pos['spacer_top'],
                spacer_color,
                spacer_alpha,
                zorder=2,
            )

        top_color, top_alpha = self._get_material_display_style(self.top_core_material)
        self._fill_rect(ax, top_left, top_right, pos['top_bot'], pos['top_top'], top_color, top_alpha, zorder=3)

        # Electrodes stay visually dominant.
        self._fill_rect(ax, 0.0, cx - gap_half, e_bot, e_top, self.gold_color, 1.0, zorder=5)
        self._fill_rect(ax, cx + gap_half, self.domain_width, e_bot, e_top, self.gold_color, 1.0, zorder=5)

        # Al2O3 boundary
        ax.plot([0, self.domain_width], [pos['al2o3_bot'], pos['al2o3_bot']], color=color, alpha=alpha, linewidth=lw, linestyle='--', zorder=7)
        ax.plot([0, self.domain_width], [pos['al2o3_top'], pos['al2o3_top']], color=color, alpha=alpha, linewidth=lw, linestyle='--', zorder=7)

        # BTO thin film
        ax.plot([0, self.domain_width], [pos['bto_bot'], pos['bto_bot']], color=color, alpha=alpha, linewidth=lw, zorder=7)
        ax.plot([0, self.domain_width], [pos['bto_top'], pos['bto_top']], color=color, alpha=alpha, linewidth=lw, zorder=7)

        # SiO2 substrate boundary
        ax.plot([0, self.domain_width], [self.oxide_thickness, self.oxide_thickness], color=color, alpha=alpha, linewidth=lw, zorder=7)

        # Spacer outline. Keep air spacers as outline-only, but fill solid materials above.
        if self.spacer_thickness > 0:
            ax.plot([0, self.domain_width], [pos['spacer_bot'], pos['spacer_bot']], color=color, alpha=alpha, linewidth=lw, linestyle=':', zorder=7)
            ax.plot([0, self.domain_width], [pos['spacer_top'], pos['spacer_top']], color=color, alpha=alpha, linewidth=lw, linestyle=':', zorder=7)

        # Top rib waveguide outline
        ax.plot([top_left, top_right, top_right, top_left, top_left],
                [pos['top_bot'], pos['top_bot'], pos['top_top'], pos['top_top'], pos['top_bot']],
                color=color, alpha=alpha, linewidth=lw, zorder=7)

        # Explicitly mark the electrode-gap sidewalls so the central opening is visually well-defined.
        ax.plot([cx - gap_half, cx - gap_half], [e_bot, e_top], color=color, alpha=alpha, linewidth=lw, zorder=7)
        ax.plot([cx + gap_half, cx + gap_half], [e_bot, e_top], color=color, alpha=alpha, linewidth=lw, zorder=7)

    def plot_mode(self, mode, tag=""):
        """Plot mode fields with rainbow colormap - same size as electrostatic plots."""
        fig, ax = plt.subplots(1, 1, figsize=(6, 5))

        n_eff_str = f"{mode.n_eff.real:.6f}"
        if mode.n_eff.imag != 0:
            n_eff_str += f" + {mode.n_eff.imag:.2e}j"
        loss_str = f"{mode.alpha_db_per_cm:.3f} dB/cm"

        fig.suptitle(
            f"Mode Profile: n_eff = {n_eff_str}, Loss = {loss_str} {tag}",
            fontsize=14, y=0.95
        )

        Xv, Yv = self.get_vertex_mesh()

        im = ax.contourf(Xv, Yv, mode.E_intensity, levels=30, cmap='rainbow')
        self.add_geometry_overlay(ax)
        cbar = plt.colorbar(im, ax=ax, shrink=0.8)
        cbar.set_label('E Field Intensity')
        ax.set_title('E Field Intensity')
        ax.set_xlabel('x (μm)')
        ax.set_ylabel('y (μm)')
        ax.set_aspect('equal')
        ax.set_xlim(0, self.domain_width)
        ax.set_ylim(0, self.domain_height)

        plt.tight_layout()
        plt.show()

    def plot_mode_with_vectors(self, mode, tag="", arrow_spacing=5, scale=None):
        """
        Plot mode intensity as background contour with overlaid vector arrows showing E-field direction.
        """
        fig, ax = plt.subplots(1, 1, figsize=(8, 6))

        n_eff_str = f"{mode.n_eff.real:.6f}"
        if mode.n_eff.imag != 0:
            n_eff_str += f" + {mode.n_eff.imag:.2e}j"

        fig.suptitle(f'{mode.mode_type} Mode: n_eff = {n_eff_str} {tag}', fontsize=14)

        Xv, Yv = self.get_vertex_mesh()

        # Background on vertex grid
        im = ax.contourf(Xv, Yv, mode.E_intensity, levels=30, cmap='viridis', alpha=0.7)
        self.add_geometry_overlay(ax)

        y_indices = np.arange(0, self.ny_v, arrow_spacing)
        x_indices = np.arange(0, self.nx_v, arrow_spacing)

        X_sub = Xv[np.ix_(y_indices, x_indices)]
        Y_sub = Yv[np.ix_(y_indices, x_indices)]
        Ex_sub = mode.Ex[np.ix_(y_indices, x_indices)]
        Ey_sub = mode.Ey[np.ix_(y_indices, x_indices)]

        Ex_real = np.real(Ex_sub)
        Ey_real = np.real(Ey_sub)

        if scale is None:
            E_mag_sub = np.sqrt(Ex_real**2 + Ey_real**2)
            max_E = np.max(E_mag_sub) if np.max(E_mag_sub) > 0 else 1.0
            scale = 0.5 * arrow_spacing * self.dx / max_E

        ax.quiver(
            X_sub, Y_sub, Ex_real, Ey_real,
            scale_units='xy', scale=1/scale, width=0.003,
            color='white', alpha=0.8
        )

        cbar = plt.colorbar(im, ax=ax, shrink=0.8)
        cbar.set_label('E Field Intensity')
        ax.set_title(f'{mode.mode_type} Mode E-field (Intensity + Vector Directions)')
        ax.set_xlabel('x (μm)')
        ax.set_ylabel('y (μm)')
        ax.set_aspect('equal')
        ax.set_xlim(0, self.domain_width)
        ax.set_ylim(0, self.domain_height)

        plt.tight_layout()
        plt.show()

    def plot_electrostatic_field(self, voltage=3.0, show_streamlines=False):
        """
        Plot electrostatic field analysis showing potential and field magnitude.
        
        This function visualizes the DC electric field distribution used for 
        electro-optic analysis in the BTO thin film waveguide structure.
        
        Parameters:
        -----------
        voltage : float, default=3.0
            Applied voltage across electrodes (V)
        show_streamlines : bool, default=False
            Whether to show electric field streamlines (disabled for cleaner plots)
        
        Plots:
        ------
        - Electric potential distribution with viridis colormap
        - Electric field magnitude distribution with plasma colormap
        - Device geometry overlay in white
        """
        phi, Ex, Ey, Em = self.solve_electrostatic(voltage)
        
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
        fig.suptitle(f'Electrostatic Field Analysis (V = {voltage}V)', fontsize=16)
        
        Xc, Yc = np.meshgrid(self.xc, self.yc, indexing='xy')
        
        # Potential - RdYlBu COLORMAP (original)
        levels_phi = np.linspace(phi.min(), phi.max(), 25)
        im1 = ax1.contourf(Xc, Yc, phi, levels=levels_phi, cmap='RdYlBu')
        self.add_geometry_overlay(ax1)
        cbar1 = plt.colorbar(im1, ax=ax1, shrink=0.8)
        cbar1.set_label('Potential (V)')
        ax1.set_title('Electric Potential')
        ax1.set_xlabel('x (μm)')
        ax1.set_ylabel('y (μm)')
        ax1.set_aspect('equal')
        
        # E field magnitude - PLASMA COLORMAP
        im2 = ax2.contourf(Xc, Yc, Em/1e6, levels=30, cmap='plasma')
        self.add_geometry_overlay(ax2)
        cbar2 = plt.colorbar(im2, ax=ax2, shrink=0.8)
        cbar2.set_label('|E| (MV/m)')
        ax2.set_title('Electric Field Magnitude')
        ax2.set_xlabel('x (μm)')
        ax2.set_ylabel('y (μm)')
        ax2.set_aspect('equal')
        
        for ax in [ax1, ax2]:
            ax.set_xlim(0, self.domain_width)
            ax.set_ylim(0, self.domain_height)
        
        plt.tight_layout()
        plt.show()

    def select_te_like_modes_by_metrics(self, modes, min_te_fraction=0.80):
        """
        From a mode list, return:
        1) the TE-like mode with the lowest loss
        2) the TE-like mode with the highest top confinement

        Returns
        -------
        (best_loss_mode, best_topconf_mode)
        """
        te_like = []
        for m in modes:
            te = float(getattr(m, "te_fraction", 0.0))
            if te >= min_te_fraction:
                te_like.append(m)

        if not te_like:
            return None, None

        best_loss_mode = min(
            te_like,
            key=lambda m: float(getattr(m, "alpha_db_per_cm", float("inf")))
        )

        best_topconf_mode = max(
            te_like,
            key=lambda m: float(getattr(m, "confinement_factor", 0.0))
        )

        return best_loss_mode, best_topconf_mode

    def collect_te_like_modes_multi_guess(
        self,
        n_eff_guesses=(1.7, 1.9, 2.1),
        n_modes=8,
        min_te_fraction=0.80,
     ):
        """
        Solve modes for several n_eff_guess values and collect all TE-like candidates.
        """
        candidates = []

        for guess in n_eff_guesses:
            modes = self.solve_modes(n_modes=n_modes, n_eff_guess=guess)
            for m in modes:
                te = float(getattr(m, "te_fraction", 0.0))
                if te >= min_te_fraction:
                    candidates.append(m)

        return candidates

    def select_seed_mode_max_topconf(
        self,
        n_eff_guesses=(1.7, 1.9, 2.1),
        n_modes=8,
        min_te_fraction=0.80,
    ):
        """
        Choose the initial target branch as the TE-like mode with maximum top confinement.
        """
        candidates = self.collect_te_like_modes_multi_guess(
            n_eff_guesses=n_eff_guesses,
            n_modes=n_modes,
            min_te_fraction=min_te_fraction,
        )

        if not candidates:
            return None

        return max(
            candidates,
            key=lambda m: float(getattr(m, "confinement_factor", 0.0))
        )

    def track_mode_from_reference(
        self,
        ref_mode,
        n_eff_guesses=(1.7, 1.9, 2.1),
        n_modes=8,
        min_te_fraction=0.80,
    ):
        """
        Track the most similar TE-like mode to a reference mode.

        First version:
        - same TE-like family only
        - minimize a simple distance in (n_eff, top_conf)
        """
        candidates = self.collect_te_like_modes_multi_guess(
            n_eff_guesses=n_eff_guesses,
            n_modes=n_modes,
            min_te_fraction=min_te_fraction,
        )

        if not candidates:
            return None

        ref_neff = float(ref_mode.n_eff.real)
        ref_conf = float(ref_mode.confinement_factor)

        def distance(m):
            neff = float(m.n_eff.real)
            conf = float(m.confinement_factor)

            d_neff = abs(neff - ref_neff)
            d_conf = abs(conf - ref_conf)

            # n_eff is primary, top_conf is secondary
            return d_neff + 0.5 * d_conf

        return min(candidates, key=distance)

    def spacer_branch_tracking_sweep(
        self,
        spacer_values=(0.00, 0.02, 0.05, 0.08, 0.10, 0.15, 0.20),
        n_eff_guesses=(1.7, 1.9, 2.1),
        n_modes=8,
        min_te_fraction=0.80,
    ):
        """
        Sweep spacer_thickness and track one TE-like loaded branch across the sweep.
        """
        print("\n" + "=" * 80)
        print("SPACER BRANCH-TRACKING SWEEP")
        print("=" * 80)

        tracked_mode = None

        print(f"{'Spacer':<8} {'Type':<8} {'n_eff':<10} {'Loss':<12} {'TopConf':<10} {'TE%':<8}")
        print("-" * 64)

        for i, sp in enumerate(spacer_values):
            self.spacer_thickness = sp
            self.invalidate_geometry_cache()

            if i == 0:
                tracked_mode = self.select_seed_mode_max_topconf(
                    n_eff_guesses=n_eff_guesses,
                    n_modes=n_modes,
                    min_te_fraction=min_te_fraction,
                )
            else:
                tracked_mode = self.track_mode_from_reference(
                    tracked_mode,
                    n_eff_guesses=n_eff_guesses,
                    n_modes=n_modes,
                    min_te_fraction=min_te_fraction,
                )

            if tracked_mode is None:
                print(f"{sp:<8.3f} {'NONE':<8} {'-':<10} {'-':<12} {'-':<10} {'-':<8}")
                continue

            print(
                f"{sp:<8.3f} "
                f"{tracked_mode.mode_type:<8} "
                f"{tracked_mode.n_eff.real:<10.6f} "
                f"{tracked_mode.alpha_db_per_cm:<12.6f} "
                f"{tracked_mode.confinement_factor:<10.4f} "
                f"{100.0*tracked_mode.te_fraction:<8.2f}"
            )

    def diagnostic_neff_guess_scan(
        self,
        n_eff_guesses=(1.5, 1.7, 1.9, 2.1),
        n_modes=10,
        min_te_fraction=0.80,
    ):
        """
        Diagnose which TE-like branches are found under different n_eff_guess values.

        For each n_eff_guess, print:
        - all TE-like modes found
        - the lowest-loss TE-like mode
        - the highest-top-conf TE-like mode
        """
        print("\n" + "=" * 80)
        print("DIAGNOSTIC n_eff_guess SCAN")
        print("=" * 80)

        for guess in n_eff_guesses:
            print(f"\n--- n_eff_guess = {guess:.3f} ---")
            modes = self.solve_modes(n_modes=n_modes, n_eff_guess=guess)

            if not modes:
                print("No modes found.")
                continue

            te_like = []
            for i, m in enumerate(modes):
                te = float(getattr(m, "te_fraction", 0.0))
                if te >= min_te_fraction:
                    te_like.append((i, m))

            if not te_like:
                print(f"No TE-like modes found with TE >= {min_te_fraction:.2f}")
                continue

            print(f"{'Idx':<4} {'Type':<8} {'n_eff':<10} {'Loss':<12} {'TopConf':<10} {'TE%':<8}")
            print("-" * 60)
            for i, m in te_like:
                print(
                    f"{i:<4} "
                    f"{str(getattr(m, 'mode_type', '')):<8} "
                    f"{float(getattr(m, 'n_eff', 0.0).real):<10.6f} "
                    f"{float(getattr(m, 'alpha_db_per_cm', 0.0)):<12.6f} "
                    f"{float(getattr(m, 'confinement_factor', 0.0)):<10.4f} "
                    f"{100.0*float(getattr(m, 'te_fraction', 0.0)):<8.2f}"
                )

            best_loss_mode, best_topconf_mode = self.select_te_like_modes_by_metrics(
                modes,
                min_te_fraction=min_te_fraction
            )

            if best_loss_mode is not None:
                print("\nLowest-loss TE-like mode:")
                print(
                    f"  type={best_loss_mode.mode_type}, "
                    f"n_eff={best_loss_mode.n_eff.real:.6f}, "
                    f"loss={best_loss_mode.alpha_db_per_cm:.6f}, "
                    f"top_conf={best_loss_mode.confinement_factor:.4f}, "
                    f"TE={best_loss_mode.te_fraction:.4f}"
                )

            if best_topconf_mode is not None:
                print("Highest-top-conf TE-like mode:")
                print(
                    f"  type={best_topconf_mode.mode_type}, "
                    f"n_eff={best_topconf_mode.n_eff.real:.6f}, "
                    f"loss={best_topconf_mode.alpha_db_per_cm:.6f}, "
                    f"top_conf={best_topconf_mode.confinement_factor:.4f}, "
                    f"TE={best_topconf_mode.te_fraction:.4f}"
                )
#------------------------------------------------------------------------
#----------------------MAIN RUN------------------------------------------
#------------------------------------------------------------------------
"""
if __name__ == "__main__":
    print("=" * 80)
    print("BTO FLAT THIN FILM / SANDWICH SOLVER — Plain Matplotlib Edition")
    print("=" * 80)

    Vapp   = 3.0
    solver = CombinedBTOFlatThinFilmSolver(wavelength_um=1.55)

    # crystal orientation
    solver.orientation = "a-axis"
    solver.phi_deg     = 45.0

    print("\nConfiguration")
    print("──────────────")
    print(f"  Crystal orientation : {solver.orientation}")
    print(f"  Crystal angle  φ    : {solver.phi_deg}°")
    print(f"  Applied voltage     : {Vapp} V")
    print(f"  BTO thickness       : {solver.bto_thickness} µm")
    print(f"  Al₂O₃ buffer        : {solver.al2o3_thickness*1e3:.0f} nm")
    print(f"  Top core ({solver.top_core_material}) : {solver.sin_rib_width} µm × {solver.sin_rib_height*1e3:.0f} nm")
    print(f"  Spacer ({solver.spacer_material})     : {solver.spacer_thickness*1e3:.0f} nm")

    # 1) geometry sanity
    solver.check_geometry_consistency()

    # 2) rotation sanity
    solver.rotation_sanity_check(phi_a=0.0, phi_b=45.0)

    # 3) base-mode analysis
    modes = solver.analyze_all_modes(n_modes=8,
                                     n_eff_guess=2.5,
                                     voltage=0.0,
                                     show_plots=True)

    # 4) EO-perturbed modes
    modes_eo = solver.analyze_all_modes(n_modes=8,
                                        n_eff_guess=2.5,
                                        voltage=Vapp,
                                        show_plots=True)

    # 4a) Vector overlay plots for TE/TM mode identification
    print("\nPlotting modes with E-field vector overlays...")
    if modes:
        print("Plotting fundamental base mode with E-field vectors...")
        fund_idx = solver.find_fundamental_mode_index(modes)
        solver.plot_mode_with_vectors(modes[fund_idx], tag="(Base Mode with E-field vectors)", arrow_spacing=4)
    
    if modes_eo:
        print("Plotting fundamental EO mode with E-field vectors...")  
        fund_eo_idx = solver.find_fundamental_mode_index(modes_eo)
        solver.plot_mode_with_vectors(modes_eo[fund_eo_idx], tag="(EO Mode with E-field vectors)", arrow_spacing=4)

    # 5) electrostatics
    solver.plot_electrostatic_field(voltage=Vapp, show_streamlines=True)

    # 6) BTO index map (weighted by fundamental mode)
    if modes:
        fund = modes[solver.find_fundamental_mode_index(modes)]
        solver.plot_bto_index_distribution(voltage=Vapp, mode=fund)

    # 7) EO overlap for fundamental
    if modes_eo:
        fund_eo = modes_eo[solver.find_fundamental_mode_index(modes_eo)]
        Γ = solver.compute_eo_overlap_for_modes([fund_eo], Vapp)[0]
        print(f"\nEO-overlap Γ (fundamental) = {Γ:.4f}")

    # 8) voltage sweep
    v_data = solver.voltage_sweep_analysis(voltage_range=(0.1, 5.0))
    solver.plot_voltage_sweep_results(v_data)

    # 9) crystal-angle sweep
    φ_data = solver.crystal_angle_sweep_analysis(phi_range=(0, 90), voltage=Vapp)
    solver.plot_angle_sweep_results(φ_data)

    # 10) numerical r_eff extraction
    rdat = solver.extract_numerical_r_eff(voltage=Vapp, n_eff_guess=2.5)
    print(f"\nr_eff (numerical) ≈ {rdat['r_eff']*1e12:.1f} pm V⁻¹")

    print("\n" + "=" * 80)
    print("Analysis complete.")
    """

"""
if __name__ == "__main__":
    solver = CombinedBTOFlatThinFilmSolver(wavelength_um=1.55)

    # choose one simple case
    solver.top_core_material = "sio2"
    solver.spacer_material = "air"
    solver.spacer_thickness = 0.0

    # optional geometry
    solver.sin_rib_width = 1.2
    solver.sin_rib_height = 0.10

    # print stack
    solver.check_geometry_consistency()

    # build both geometry maps once
    epsxx, epsxy, epsyx, epsyy, epszz, eps_r = solver.create_shared_geometry()
    print("Geometry build OK.")
    print("Optical map shape:", epsxx.shape)
    print("Electrostatic map shape:", eps_r.shape)

    # run only a small mode solve first
    modes = solver.analyze_all_modes(
        n_modes=4,
        n_eff_guess=2.0,
        voltage=0.0,
        show_plots=True
    )

    print(f"Found {len(modes)} modes.")
    """

"""
if __name__ == "__main__":
    print("=" * 80)
    print("BTO FLAT THIN FILM / SANDWICH SOLVER — Plain Matplotlib Edition")
    print("=" * 80)

    Vapp   = 3.0
    solver = CombinedBTOFlatThinFilmSolver(wavelength_um=1.55)

    # choose geometry explicitly
    solver.top_core_material = "sio2"
    solver.spacer_material   = "air"
    solver.spacer_thickness  = 0.05

    # crystal orientation
    solver.orientation = "a-axis"
    solver.phi_deg     = 45.0

    print("\nConfiguration")
    print("──────────────")
    print(f"  Crystal orientation : {solver.orientation}")
    print(f"  Crystal angle  φ    : {solver.phi_deg}°")
    print(f"  Applied voltage     : {Vapp} V")
    print(f"  BTO thickness       : {solver.bto_thickness} µm")
    print(f"  Al₂O₃ buffer        : {solver.al2o3_thickness*1e3:.0f} nm")
    print(f"  Top core ({solver.top_core_material}) : {solver.sin_rib_width} µm × {solver.sin_rib_height*1e3:.0f} nm")
    print(f"  Spacer ({solver.spacer_material})     : {solver.spacer_thickness*1e3:.0f} nm")

    # 1) geometry sanity
    solver.check_geometry_consistency()

    # 2) rotation sanity
    solver.rotation_sanity_check(phi_a=0.0, phi_b=45.0)

    # 3) base-mode analysis
    modes = solver.analyze_all_modes(n_modes=4,
                                     n_eff_guess=2.1,
                                     voltage=0.0,
                                     show_plots=True)

    # 4) EO-perturbed modes
    modes_eo = solver.analyze_all_modes(n_modes=4,
                                        n_eff_guess=2.1,
                                        voltage=Vapp,
                                        show_plots=True)

    # 4a) Vector overlay plots
    print("\nPlotting modes with E-field vector overlays...")
    if modes:
        print("Plotting fundamental base mode with E-field vectors...")
        fund_idx = solver.find_fundamental_mode_index(modes)
        solver.plot_mode_with_vectors(modes[fund_idx], tag="(Base Mode with E-field vectors)", arrow_spacing=4)

    if modes_eo:
        print("Plotting fundamental EO mode with E-field vectors...")
        fund_eo_idx = solver.find_fundamental_mode_index(modes_eo)
        solver.plot_mode_with_vectors(modes_eo[fund_eo_idx], tag="(EO Mode with E-field vectors)", arrow_spacing=4)

    # 5) electrostatics
    solver.plot_electrostatic_field(voltage=Vapp, show_streamlines=True)

    # 6) BTO index map
    if modes:
        fund = modes[solver.find_fundamental_mode_index(modes)]
        solver.plot_bto_index_distribution(voltage=Vapp, mode=fund)

    # 7) EO overlap for fundamental
    if modes_eo:
        fund_eo = modes_eo[solver.find_fundamental_mode_index(modes_eo)]
        Γ = solver.compute_eo_overlap_for_modes([fund_eo], Vapp)[0]
        print(f"\nEO-overlap Γ (fundamental) = {Γ:.4f}")

    print("\n" + "=" * 80)
    print("Analysis complete.")
    """

"""
if __name__ == "__main__":
    print("=" * 80)
    print("BTO FLAT THIN FILM / SANDWICH SOLVER — FULL SPACER SWEEP (NO PLOTS)")
    print("=" * 80)

    Vapp = 3.0

    # Choose ONE fixed resolution for the whole sweep
    # Change dy here when you want to test a finer grid
    solver = CombinedBTOFlatThinFilmSolver(
        wavelength_um=1.55,
        dx=0.01,
        dy=0.01,      # <- use 0.05 / 0.02 / 0.01 here as your fixed test resolution
        verbose=False
    )

    # fixed baseline
    solver.orientation = "a-axis"
    solver.phi_deg = 45.0
    solver.top_core_material = "sio2"
    solver.spacer_material = "air"
    solver.sin_rib_width = 1.2
    solver.sin_rib_height = 0.10

    # keep the original sweep idea
    spacer_list = [0.00, 0.05, 0.10, 0.20]

    print("\nFixed baseline:")
    print(f"  dx                = {solver.dx} um")
    print(f"  dy                = {solver.dy} um")
    print(f"  top_core_material = {solver.top_core_material}")
    print(f"  spacer_material   = {solver.spacer_material}")
    print(f"  rib_width         = {solver.sin_rib_width} um")
    print(f"  rib_height        = {solver.sin_rib_height} um")
    print(f"  orientation       = {solver.orientation}")
    print(f"  phi_deg           = {solver.phi_deg}")
    print(f"  voltage           = {Vapp} V")

    for i, sp in enumerate(spacer_list, start=1):
        print("\n" + "=" * 80)
        print(f"CASE {i}: spacer_thickness = {sp:.3f} um")
        print("=" * 80)

        solver.spacer_thickness = sp
        solver.invalidate_geometry_cache()

        # 1) geometry
        solver.check_geometry_consistency()

        # 2) rotation sanity
        solver.rotation_sanity_check(phi_a=0.0, phi_b=45.0)

        # 3) base modes (full text, no plots)
        modes = solver.analyze_all_modes(
            n_modes=4,
            n_eff_guess=2.1,
            voltage=0.0,
            show_plots=False
        )

        # 4) EO modes (full text, no plots)
        modes_eo = solver.analyze_all_modes(
            n_modes=4,
            n_eff_guess=2.1,
            voltage=Vapp,
            show_plots=False
        )

        # 5) EO overlap for currently selected mode
        if modes_eo:
            fund_eo_idx = solver.find_fundamental_mode_index(modes_eo)
            fund_eo = modes_eo[fund_eo_idx]
            gamma = solver.compute_eo_overlap_for_modes([fund_eo], Vapp)[0]
            print(f"\nEO-overlap Γ (selected mode) = {gamma:.4f}")
        else:
            print("\nEO-overlap Γ (selected mode) = N/A (no EO mode found)")

    print("\n" + "=" * 80)
    print("Full spacer sweep complete.")
     """

"""
if __name__ == "__main__":
    print("=" * 80)
    print("BTO FLAT THIN FILM / SANDWICH SOLVER — DIAGNOSTIC MODE")
    print("=" * 80)

    solver = CombinedBTOFlatThinFilmSolver(
        wavelength_um=1.55,
        dx=0.02,
        dy=0.02
    )

    # Static materials
    solver.top_core_material = "sio2"
    solver.spacer_material = "air"

    # Pick ONE geometry to diagnose.
    # Start with a reasonable test point; you can replace this with any point you want.
    solver.al2o3_thickness = 0.026
    solver.bto_thickness = 0.44
    solver.spacer_thickness = 0.10
    solver.sin_rib_width = 0.70
    solver.sin_rib_height = 0.15
    solver.electrode_gap = 4.4

    # Crystal settings
    solver.orientation = "a-axis"
    solver.phi_deg = 45.0

    # Optional sanity print
    solver.check_geometry_consistency()

    # Diagnostic scan
    solver.diagnostic_neff_guess_scan(
        n_eff_guesses=(1.5, 1.7, 1.9, 2.1),
        n_modes=10,
        min_te_fraction=0.80
    )

    print("\n" + "=" * 80)
    print("Diagnostic scan complete.")
    """

if __name__ == "__main__":
    print("=" * 80)
    print("BTO FLAT THIN FILM / SANDWICH SOLVER — BRANCH TRACKING TEST")
    print("=" * 80)

    solver = CombinedBTOFlatThinFilmSolver(
        wavelength_um=1.55,
        dx=0.02,
        dy=0.02
    )

    # Static materials
    solver.top_core_material = "sio2"
    solver.spacer_material = "air"

    # Fixed geometry (reduce dimensions first)
    solver.al2o3_thickness = 0.026
    solver.bto_thickness = 0.30
    solver.sin_rib_width = 0.70
    solver.sin_rib_height = 0.15
    solver.electrode_gap = 4.4

    # Crystal settings
    solver.orientation = "a-axis"
    solver.phi_deg = 45.0

    solver.check_geometry_consistency()

    solver.spacer_branch_tracking_sweep(
        spacer_values=(0.00, 0.02, 0.05, 0.08, 0.10, 0.15, 0.20),
        n_eff_guesses=(1.7, 1.9, 2.1),
        n_modes=8,
        min_te_fraction=0.80,
    )

    print("\n" + "=" * 80)
    print("Branch tracking sweep complete.")
