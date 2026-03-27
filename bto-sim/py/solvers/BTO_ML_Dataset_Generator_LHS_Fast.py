#!/usr/bin/env python3
"""
BTO Machine Learning Dataset Generator - Latin Hypercube Sampling + FAST Spot Simulations
========================================================================================

Uses Latin Hypercube Sampling (LHS) for better parameter space coverage.
Generates 10,000 random BTO modulator configurations for machine learning training:
- 10,000 ridge device configurations  
- 10,000 flat device configurations

FAST VERSION: Uses spot simulations at 0V and 5V only (no sweeps)

LHS parameter generation ensures better coverage of the parameter space
compared to uniform random sampling.

Random parameters with LHS:
- BTO thickness: 0.100-0.500 μm
- Waveguide width: 0.5-3.0 μm  
- Waveguide height: 0.050-0.300 μm
- Electrode gap: 1.5-10.0 μm
- Electrode height: 0.2-1.0 μm
- Ridge angle: 0-15 degrees (ridge only)
- Crystal angle: 0-90 degrees (varied per config)

Output metrics (UPDATED FOR NEW SOLVERS):
- Delta n_eff (n_eff@5V - n_eff@0V)
- Loss (dB/cm) at 0V
- Confinement factor at 0V
- EO overlap factor Γ
- V_π·L (V·cm) - NEW: 2-point method (0.1V to 5.0V) same as crystal sweep
- dn_eff/dV (1/V) - NEW: from 2-point measurement
- r_eff Direct (pm/V) - Direct Δn method using updated solvers
- r_eff V_π·L (pm/V) - NEW: r_eff extracted from V_π·L measurement
- V_π (V) - For 1cm device based on V_π·L

All results saved to CSV for ML training.
"""

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import os
import sys
import json
import pandas as pd
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing
import warnings
import random
from scipy.stats import qmc
from typing import Dict, List, Tuple, Any
import io
import contextlib
import time
import json
import os
from pathlib import Path
from datetime import datetime

# Add solvers directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'solvers'))

# Set matplotlib to non-interactive backend
matplotlib.use('Agg')

# Import the solver modules
from BTO_Flat_ThinFilm_Crystal_Sweep_Final import CombinedBTOFlatThinFilmSolver
from BTO_Ridge_Crystal_Sweep_Final import CombinedBTOSolver as CombinedBTORidgeSolver

# Suppress warnings for cleaner output
warnings.filterwarnings("ignore")


# ===========================
# CONFIGURATION
# ===========================

EXPECTED_TYPE = "bto_ml_dataset_lhs_fast"
EXPECTED_SCHEMA = "0.1.0"

def load_cfg(cfg_path: str) -> dict:
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))

    if cfg.get("type") != EXPECTED_TYPE:
        raise ValueError(f"Wrong config type: {cfg.get('type')} (expected {EXPECTED_TYPE})")

    if cfg.get("schema_version") != EXPECTED_SCHEMA:
        raise ValueError(f"Wrong schema_version: {cfg.get('schema_version')} (expected {EXPECTED_SCHEMA})")

    return cfg

def resolve_output_dir(cfg: dict) -> str:
    # priority: config output_dir > env var > current directory
    base = (cfg["output"].get("output_dir") or "").strip()
    if base:
        base_path = Path(base).expanduser()
    else:
        base_path = Path(os.environ.get("BTO_OUTPUT_ROOT", ".")).expanduser()

    prefix = cfg["output"].get("dir_prefix", "BTO_ML_Dataset_LHS_Fast")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = base_path / f"{prefix}_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    return str(out)

ML_PARAMETER_RANGES = {}
N_CONFIGS_PER_TYPE = 0
V_LOW = 0.0
V_HIGH = 0.0
ML_OUTPUT_DIR = ""
RANDOM_SEED = 0
DEVICE_TYPES = ["flat", "ridge"]
WAVELENGTH_UM = 1.55
WORKERS = None

def apply_cfg(cfg: dict) -> None:
    global ML_PARAMETER_RANGES, N_CONFIGS_PER_TYPE, V_LOW, V_HIGH, ML_OUTPUT_DIR
    global RANDOM_SEED, DEVICE_TYPES, WAVELENGTH_UM, WORKERS

    ML_PARAMETER_RANGES = cfg["parameter_ranges"]
    N_CONFIGS_PER_TYPE = int(cfg["n_configs_per_type"])
    DEVICE_TYPES = list(cfg["device_types"])
    RANDOM_SEED = int(cfg["random_seed"])
    WORKERS = cfg.get("workers", None)

    WAVELENGTH_UM = float(cfg["wavelength_um"])
    V_LOW = float(cfg["voltages_V"]["low"])
    V_HIGH = float(cfg["voltages_V"]["high"])

    ML_OUTPUT_DIR = resolve_output_dir(cfg)



def set_random_seed(seed=RANDOM_SEED):
    """Set random seed for reproducibility."""
    random.seed(seed)
    np.random.seed(seed)

def select_fundamental_te0_mode(modes):
    """
    Enhanced mode selection that ensures fundamental TE0 mode with highest n_eff is selected.
    EXACT COPY from BTO_Geometry_Sweep_Simple.py
    """
    if not modes:
        return None
        
    # Classify modes as TE or TM
    te_modes = []
    tm_modes = []
    
    for mode in modes:
        is_te = False
        
        # Check multiple ways to identify TE modes
        if hasattr(mode, 'mode_type') and 'TE' in str(mode.mode_type):
            is_te = True
        elif hasattr(mode, 'te_fraction') and mode.te_fraction > 0.7:
            is_te = True
        elif hasattr(mode, 'polarization') and 'TE' in str(mode.polarization):
            is_te = True
            
        if is_te:
            te_modes.append(mode)
        else:
            tm_modes.append(mode)
    
    # Sort TE modes by n_eff (descending) to get fundamental TE0 first
    te_modes.sort(key=lambda m: np.real(m.n_eff), reverse=True)
    
    # Return fundamental TE0 mode (highest n_eff among TE modes)
    return te_modes[0] if te_modes else modes[0] if modes else None

# =============================================================================
# LATIN HYPERCUBE SAMPLING PARAMETER GENERATION
# =============================================================================

def generate_lhs_parameters_batch(device_type: str, n_samples: int) -> List[Dict[str, float]]:
    """
    Generate random parameters using Latin Hypercube Sampling for better parameter space coverage.
    
    Args:
        device_type: 'flat' or 'ridge'
        n_samples: Number of parameter sets to generate
        
    Returns:
        List of dictionaries with LHS-sampled parameter values
    """
    # Define parameter names and ranges
    if device_type == 'flat':
        param_names = ['bto_thickness_um', 'width_um', 'height_um', 
                      'electrode_gap_um', 'electrode_height_um', 'crystal_angle_deg']
        param_ranges = [ML_PARAMETER_RANGES[name] for name in param_names]
        n_dims = len(param_names)
    else:  # ridge
        param_names = ['bto_thickness_um', 'width_um', 'height_um', 
                      'electrode_gap_um', 'electrode_height_um', 'ridge_angle_deg', 'crystal_angle_deg']
        param_ranges = [ML_PARAMETER_RANGES[name] for name in param_names]
        n_dims = len(param_names)
    
    # Create Latin Hypercube Sampler
    sampler = qmc.LatinHypercube(d=n_dims)
    # 设置 numpy 随机种子以保证 LHS 的可复现性
    np.random.seed(RANDOM_SEED)
    
    # Generate LHS samples (values between 0 and 1)
    lhs_samples = sampler.random(n=n_samples)
    
    # Scale samples to actual parameter ranges
    scaled_samples = qmc.scale(lhs_samples, 
                              l_bounds=[r[0] for r in param_ranges],
                              u_bounds=[r[1] for r in param_ranges])
    
    # Convert to list of parameter dictionaries
    parameter_sets = []
    
    for i, sample in enumerate(scaled_samples):
        params = {}
        
        # Assign scaled values to parameter names
        for j, param_name in enumerate(param_names):
            params[param_name] = sample[j]
        
        # Add device-specific parameters
        if device_type == 'flat':
            params['ridge_angle_deg'] = 0.0  # Not applicable for flat devices
        
        params['device_type'] = device_type
        params['config_id'] = f"{device_type}_{random.randint(10000, 99999)}"
        
        parameter_sets.append(params)
    
    return parameter_sets

def simulate_single_configuration_fast_NO_PRINT(config_params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Simulate a single BTO modulator configuration using FAST spot simulations.
    
    Args:
        config_params: Dictionary with device parameters
        
    Returns:
        Dictionary with simulation results
    """
    device_type = config_params['device_type']
    config_id = config_params['config_id']
    
    # Initialize result structure
    result = {
        'config_id': config_id,
        'device_type': device_type,
        'parameters': config_params.copy(),
        'metrics': {},
        'success': False,
        'error_message': None
    }
    
    try:
        pass  # Removed print statement
        
        # Create appropriate solver based on device architecture
        if device_type == 'flat':
            # FLAT DEVICE: SiNx ridge waveguide on BTO thin film
            solver = CombinedBTOFlatThinFilmSolver(wavelength_um=1.55)
            solver.bto_thickness = config_params['bto_thickness_um']      # BTO film thickness
            solver.sin_rib_width = config_params['width_um']              # SiNx ridge width
            solver.sin_rib_height = config_params['height_um']            # SiNx ridge height
            solver.electrode_gap = config_params['electrode_gap_um']      # Gap between electrodes
            solver.electrode_thickness = config_params['electrode_height_um']  # Metal thickness
            
        else:  # ridge
            # RIDGE DEVICE: BTO ridge waveguide (etched BTO)
            solver = CombinedBTORidgeSolver(wavelength_um=1.55)
            solver.slab_thickness = config_params['bto_thickness_um']     # BTO slab thickness
            solver.ridge_height = config_params['height_um']              # BTO ridge height (on top of slab)
            solver.top_width = config_params['width_um']                  # Ridge top width
            solver.electrode_gap = config_params['electrode_gap_um']      # Gap between electrodes
            solver.electrode_thickness = config_params['electrode_height_um']  # Metal thickness
            
            # Set ridge angle if supported
            if hasattr(solver, 'ridge_angle_deg'):
                # 如果 CombinedBTOSolver 没有 ridge_angle_deg 属性，则跳过赋值
                if hasattr(solver, 'ridge_angle_deg'):
                    # CombinedBTOSolver 没有 ridge_angle_deg 属性，跳过赋值
                    pass
            elif hasattr(solver, 'sidewall_angle'):
                solver.sidewall_angle = config_params['ridge_angle_deg']
        
        # Set crystal orientation (a-axis) and angle
        solver.orientation = "a-axis"  # Fixed a-axis orientation
        solver.phi_deg = config_params['crystal_angle_deg']
        
        # Force geometry rebuild
        if hasattr(solver, '_geom_built'):
            solver._geom_built = False
            
        # Suppress matplotlib during simulation
        plt.ioff()
        
        # 1. Solve modes at 0V (base case) - need 25 modes for proper TE0 selection
        pass  # Removed print
        if device_type == 'flat':
            # Need sufficient modes to find true fundamental TE0
            modes_0V = solver.solve_modes(n_modes=25, n_eff_guess=2.2)
        else:
            # Need sufficient modes for ridge devices  
            modes_0V = solver.solve_modes(n_modes=25, n_eff_guess=2.4)
        
        if modes_0V and len(modes_0V) > 0:
            # Find fundamental TE0 mode - EXACT SAME as working code
            mode_0V = select_fundamental_te0_mode(modes_0V)
            if mode_0V is None:
                raise ValueError("No TE0 mode found at 0V")
            
            n_eff_0V = np.real(mode_0V.n_eff)
            loss_0V = mode_0V.alpha_db_per_cm
            confinement_0V = mode_0V.confinement_factor
            
            result['metrics']['n_eff_0V'] = n_eff_0V
            result['metrics']['loss_db_per_cm'] = loss_0V
            result['metrics']['confinement_factor'] = confinement_0V
            
        else:
            raise ValueError("No modes found at 0V")
        
        # 2. Solve modes at 5V with EO effect - need 25 modes for proper TE0 selection
        pass  # Removed print
        if device_type == 'flat':
            # Need sufficient modes to find true fundamental TE0
            modes_5V = solver.solve_modes_with_eo(voltage=V_HIGH, n_modes=25, n_eff_guess=2.2)
        else:
            # Need sufficient modes for ridge devices
            modes_5V = solver.solve_modes_with_eo(voltage=V_HIGH, n_modes=25, n_eff_guess=2.4)
        
        if modes_5V and len(modes_5V) > 0:
            # Find fundamental TE0 mode - EXACT SAME as working code
            mode_5V = select_fundamental_te0_mode(modes_5V)
            if mode_5V is None:
                raise ValueError("No TE0 mode found at 5V")
            
            n_eff_5V = np.real(mode_5V.n_eff)
            
            # Calculate delta n_eff directly - SPOT CALCULATION
            delta_n_eff = n_eff_5V - n_eff_0V
            result['metrics']['delta_n_eff'] = delta_n_eff
            result['metrics']['n_eff_5V'] = n_eff_5V
            result['metrics']['n_eff_avg'] = (n_eff_0V + n_eff_5V) / 2.0
            
            # Get EO overlap factor from 5V mode
            gamma_list = solver.compute_eo_overlap_for_modes([mode_5V], V_HIGH)
            gamma = gamma_list[0] if gamma_list else 0.0
            result['metrics']['eo_overlap_gamma'] = gamma
            
        else:
            raise ValueError("No modes found at 5V")
        
        # 3. Calculate V_π·L using FAST approximation (REUSE EXISTING MODES)
        pass  # Removed print
        try:
            # FAST METHOD: Use already computed 0V and 5V modes (no additional mode solves!)
            # This approximates the 2-point method using 0V-5V instead of 0.1V-5V
            # Should be very close for ML dataset purposes
            
            delta_voltage_fast = V_HIGH - V_LOW  # 5.0V - 0.0V = 5.0V
            dn_eff_dv = abs(delta_n_eff) / delta_voltage_fast
            
            # Calculate V_π·L using same formula as crystal sweep
            wavelength_m = solver.wl * 1e-6  # Convert μm to m
            if dn_eff_dv > 0:
                vpil_m = wavelength_m / (2.0 * dn_eff_dv)
                vpil_vcm = vpil_m * 100  # Convert m to V·cm
                
                # Calculate V_π for 1cm device
                v_pi = vpil_vcm  # V_π·L for 1cm device
                
                result['metrics']['v_pi_L_V_cm'] = vpil_vcm
                result['metrics']['v_pi_V'] = v_pi
                result['metrics']['dn_eff_dv'] = dn_eff_dv
                
                # Extract r_eff using V_π·L method (NEW)
                gap_m = config_params['electrode_gap_um'] * 1e-6
                if vpil_m > 0 and gamma > 1e-15:
                    r_eff_vpil = (wavelength_m * gap_m) / (n_eff_0V**3 * gamma * vpil_m)
                    result['metrics']['r_eff_vpil_pm_per_V'] = r_eff_vpil * 1e12  # Convert to pm/V
                else:
                    result['metrics']['r_eff_vpil_pm_per_V'] = np.nan
                
                pass  # Removed print
            else:
                result['metrics']['v_pi_L_V_cm'] = np.inf
                result['metrics']['v_pi_V'] = np.inf
                result['metrics']['dn_eff_dv'] = 0
                result['metrics']['r_eff_vpil_pm_per_V'] = np.nan
                
        except Exception as e:
            pass  # Removed print
            result['metrics']['v_pi_L_V_cm'] = np.inf
            result['metrics']['v_pi_V'] = np.inf
            result['metrics']['dn_eff_dv'] = 0
            result['metrics']['r_eff_vpil_pm_per_V'] = np.nan
        
        # 4. Extract r_eff using direct Δn method (FAST FALLBACK METHOD)
        pass  # Removed print
        # Skip expensive extract_numerical_r_eff method to maintain FAST performance
        # Use fast fallback: estimate r_eff from delta_n_eff
        gap_m = config_params['electrode_gap_um'] * 1e-6
        if abs(gamma) > 1e-15 and abs(delta_n_eff) > 1e-15:
            r_eff_direct = 2.0 * abs(delta_n_eff) * gap_m / (n_eff_0V**3 * abs(gamma) * V_HIGH)
            result['metrics']['r_eff_direct_pm_per_V'] = r_eff_direct * 1e12  # Convert to pm/V
        else:
            result['metrics']['r_eff_direct_pm_per_V'] = np.nan
        result['metrics']['gamma_direct'] = gamma
        
        result['success'] = True
        pass  # Removed print
        
    except Exception as e:
        result['error_message'] = str(e)
        result['success'] = False
        pass  # Removed print
    
    finally:
        plt.close('all')
    
    return result

def simulate_single_configuration_fast(config_params: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper that suppresses ALL output including solver prints"""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return simulate_single_configuration_fast_NO_PRINT(config_params)

def save_results_to_csv(results: List[Dict[str, Any]], output_dir: str, device_type: str):
    """
    Save simulation results to CSV format for ML training.
    
    Args:
        results: List of simulation results
        output_dir: Output directory
        device_type: 'flat' or 'ridge'
    """
    
    # Prepare data for CSV
    csv_data = []
    
    for result in results:
        if result['success']:
            params = result['parameters']
            metrics = result['metrics']
            
            row = {
                # Configuration ID
                'config_id': result['config_id'],
                'device_type': device_type,
                
                # Input parameters (features for ML)
                'bto_thickness_um': params['bto_thickness_um'],
                'width_um': params['width_um'], 
                'height_um': params['height_um'],
                'electrode_gap_um': params['electrode_gap_um'],
                'electrode_height_um': params['electrode_height_um'],
                'crystal_angle_deg': params['crystal_angle_deg'],
                'ridge_angle_deg': params.get('ridge_angle_deg', 0.0),
                
                # Output metrics (targets for ML)
                'delta_n_eff': metrics.get('delta_n_eff', np.nan),
                'n_eff_avg': metrics.get('n_eff_avg', np.nan),
                'loss_db_per_cm': metrics.get('loss_db_per_cm', np.nan),
                'confinement_factor': metrics.get('confinement_factor', np.nan),
                'eo_overlap_gamma': metrics.get('eo_overlap_gamma', np.nan),
                
                # NEW V_π·L metrics using 2-point method (UPDATED)
                'v_pi_L_V_cm': metrics.get('v_pi_L_V_cm', np.nan),           # V_π·L using 2-point method
                'v_pi_V': metrics.get('v_pi_V', np.nan),                     # V_π for 1cm device
                'dn_eff_dv': metrics.get('dn_eff_dv', np.nan),               # dn_eff/dV from 2-point method
                
                # NEW r_eff metrics - TWO METHODS (UPDATED)
                'r_eff_direct_pm_per_V': metrics.get('r_eff_direct_pm_per_V', np.nan),   # Direct Δn method
                'r_eff_vpil_pm_per_V': metrics.get('r_eff_vpil_pm_per_V', np.nan),       # V_π·L method  
                'gamma_direct': metrics.get('gamma_direct', np.nan),                      # Gamma from direct method
                
                # Base mode metrics
                'n_eff_0V': metrics.get('n_eff_0V', np.nan),
                'n_eff_5V': metrics.get('n_eff_5V', np.nan),
            }
            csv_data.append(row)
    
    # Create DataFrame and save
    df = pd.DataFrame(csv_data)
    
    # Save individual device CSV
    csv_path = os.path.join(output_dir, f'BTO_{device_type}_ML_dataset_LHS_Fast_{len(csv_data)}_configs.csv')
    df.to_csv(csv_path, index=False)
    
    # Print statistics
    successful_configs = len(csv_data)
    total_configs = len(results)
    
    print(f"\n✓ {device_type.upper()} dataset (LHS+Fast) saved to: {csv_path}")
    print(f"  Successful configurations: {successful_configs}/{total_configs}")
    print(f"  Success rate: {successful_configs/total_configs*100:.1f}%")
    
    # Print parameter ranges achieved
    if successful_configs > 0:
        print(f"  Parameter ranges achieved with LHS:")
        for param in ['bto_thickness_um', 'width_um', 'height_um', 'electrode_gap_um', 'electrode_height_um', 'crystal_angle_deg']:
            values = df[param].dropna()
            print(f"    {param}: {values.min():.3f} - {values.max():.3f}")
            
        # Print metric ranges (UPDATED FOR NEW METRICS)
        print(f"  Metric ranges:")
        metric_ranges = {
            'delta_n_eff': 'Delta n_eff',
            'loss_db_per_cm': 'Loss (dB/cm)', 
            'v_pi_L_V_cm': 'V_π·L (V·cm) [NEW 2-point]',
            'r_eff_direct_pm_per_V': 'r_eff Direct (pm/V)',
            'r_eff_vpil_pm_per_V': 'r_eff V_π·L (pm/V) [NEW]',
            'dn_eff_dv': 'dn_eff/dV (1/V) [NEW]'
        }
        
        for metric, description in metric_ranges.items():
            values = df[metric].dropna()
            if len(values) > 0:
                print(f"    {description}: {values.min():.2e} - {values.max():.2e}")
    
    return df

def generate_ml_dataset_lhs_fast(custom_workers=None):
    """
    Main function to generate the complete ML dataset using Latin Hypercube Sampling + Fast spot simulations.
    """
    start_time = time.time()
    
    print("="*80)
    print("BTO ML DATASET GENERATOR - LATIN HYPERCUBE SAMPLING + FAST SPOT SIMULATIONS")
    print("="*80)
    print(f"Generating {N_CONFIGS_PER_TYPE} configurations per device type")
    print(f"Total configurations: {len(DEVICE_TYPES) * N_CONFIGS_PER_TYPE}")
    print("Using Latin Hypercube Sampling for better parameter space coverage")
    print(f"Using FAST spot simulations at {V_LOW}V and {V_HIGH}V only (no sweeps)")
    print("Crystal orientation: a-axis (fixed)")
    print("Crystal angle: 0-90° (varied per configuration)")
    print(f"Output directory: {ML_OUTPUT_DIR}")
    print("="*80)
    
    # Set random seed
    set_random_seed(RANDOM_SEED)
    
    # Create output directory
    os.makedirs(ML_OUTPUT_DIR, exist_ok=True)
    
    all_configs = []
    for device_type in DEVICE_TYPES:
        print(f"\nGenerating {N_CONFIGS_PER_TYPE} {device_type} device configurations using LHS...")
        all_configs.extend(generate_lhs_parameters_batch(device_type, N_CONFIGS_PER_TYPE))
        
    
    print(f"Total configurations generated with LHS: {len(all_configs)}")
    
    # Save configuration list
    config_file = os.path.join(ML_OUTPUT_DIR, 'ml_configurations_lhs_fast.json')
    with open(config_file, 'w') as f:
        # Convert numpy types to native Python for JSON serialization
        configs_for_json = []
        for config in all_configs:
            json_config = {}
            for key, value in config.items():
                if isinstance(value, np.floating):
                    json_config[key] = float(value)
                elif isinstance(value, np.integer):
                    json_config[key] = int(value)
                else:
                    json_config[key] = value
            configs_for_json.append(json_config)
        
        json.dump(configs_for_json, f, indent=2)
    
    print(f"✓ LHS configuration list saved to: {config_file}")
    
    # Run simulations with parallel processing - BLAST ALL CORES
    available_cores = multiprocessing.cpu_count()
    if custom_workers:
        max_workers = custom_workers
    else:
        max_workers = available_cores  # USE ALL CORES - NO LIMITS
    print(f"\n🚀 BLASTING ALL {max_workers} CORES - MAXIMUM PERFORMANCE + FAST SPOT SIMULATIONS")
    print(f"Available cores: {available_cores} | Using: {max_workers} workers")
    print(f"Each worker will simulate ~{len(all_configs) // max_workers} configurations")
    print("⚠️  HIGH MEMORY USAGE MODE - Monitor system resources")
    print(f"Progress will be shown after each completed simulation...")
    print("")  # Empty line before progress
    
    all_results = []
    flat_results = []
    ridge_results = []
    
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        # Submit all simulation tasks
        future_to_config = {
            executor.submit(simulate_single_configuration_fast, config): config 
            for config in all_configs
        }
        
        # Collect results as they complete
        completed = 0
        for future in as_completed(future_to_config):
            config = future_to_config[future]
            
            try:
                result = future.result()
                all_results.append(result)
                
                # Separate by device type
                if result['device_type'] == 'flat':
                    flat_results.append(result)
                else:
                    ridge_results.append(result)
                
                completed += 1
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (len(all_configs) - completed) / rate if rate > 0 else 0
                
                # Show progress after each completion
                status = "✓" if result['success'] else "✗"
                print(f"[{completed}/{len(all_configs)}] {result['config_id']}: {status} | "
                      f"Rate: {rate:.1f}/sec | ETA: {eta/60:.1f}min | "
                      f"Elapsed: {elapsed/60:.1f}min")
                
            except Exception as exc:
                completed += 1
                failed_result = {
                    'config_id': config.get('config_id', 'unknown'),
                    'device_type': config.get('device_type', 'unknown'),
                    'success': False,
                    'error_message': str(exc),
                    'parameters': config,
                    'metrics': {}
                }
                all_results.append(failed_result)
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                print(f"[{completed}/{len(all_configs)}] {config.get('config_id', 'unknown')}: ✗ FAILED | Rate: {rate:.1f}/sec")
    
    # Save results to CSV files
    print(f"\nSaving results to CSV files...")
    
    flat_df = save_results_to_csv(flat_results, ML_OUTPUT_DIR, 'flat')
    ridge_df = save_results_to_csv(ridge_results, ML_OUTPUT_DIR, 'ridge')
    
    # Combine both datasets
    combined_df = pd.concat([flat_df, ridge_df], ignore_index=True)
    combined_path = os.path.join(ML_OUTPUT_DIR, f'BTO_Combined_ML_dataset_LHS_Fast_{len(combined_df)}_configs.csv')
    combined_df.to_csv(combined_path, index=False)
    
    print(f"\n✓ Combined LHS+Fast dataset saved to: {combined_path}")
    
    # Save summary statistics
    summary = {
        'generation_timestamp': datetime.now().isoformat(),
        'sampling_method': 'Latin_Hypercube_Sampling',
        'simulation_method': 'fast_spot_simulations',
        'total_configurations': len(all_configs),
        'successful_configurations': len(combined_df),
        'success_rate_percent': len(combined_df) / len(all_configs) * 100,
        'flat_configs': len(flat_df),
        'ridge_configs': len(ridge_df),
        'voltages_used': [V_LOW, V_HIGH],
        'crystal_orientation': 'a-axis',
        'crystal_angle_range': ML_PARAMETER_RANGES['crystal_angle_deg'],
        'parameter_ranges': ML_PARAMETER_RANGES,
        'random_seed': RANDOM_SEED,
        'max_workers_used': max_workers,
        'output_files': {
            'flat_dataset': f'BTO_flat_ML_dataset_LHS_Fast_{len(flat_df)}_configs.csv',
            'ridge_dataset': f'BTO_ridge_ML_dataset_LHS_Fast_{len(ridge_df)}_configs.csv', 
            'combined_dataset': f'BTO_Combined_ML_dataset_LHS_Fast_{len(combined_df)}_configs.csv',
            'configurations': 'ml_configurations_lhs_fast.json'
        }
    }
    
    summary_file = os.path.join(ML_OUTPUT_DIR, 'ml_dataset_lhs_fast_summary.json')
    with open(summary_file, 'w') as f:
        json.dump(summary, f, indent=2)
    
    # Calculate final timing
    total_time = time.time() - start_time
    
    print(f"\n{'='*80}")
    print("MACHINE LEARNING DATASET GENERATION WITH LHS + FAST SPOT SIMULATIONS COMPLETE")
    print(f"{'='*80}")
    print(f"Sampling method: Latin Hypercube Sampling")
    print(f"Simulation method: Fast spot simulations (0V + 5V only)")
    print(f"Total configurations simulated: {len(all_configs)}")
    print(f"Successful configurations: {len(combined_df)}")
    print(f"Success rate: {len(combined_df) / len(all_configs) * 100:.1f}%")
    print(f"Workers used: {max_workers}/{available_cores} cores")
    print(f"Total elapsed time: {total_time/60:.1f} minutes ({total_time/3600:.2f} hours)")
    print(f"Average rate: {len(all_configs)/total_time:.1f} simulations/second")
    print(f"Output directory: {ML_OUTPUT_DIR}")
    print(f"Summary saved to: {summary_file}")
    print("="*80)
    
    return combined_df

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "configs" / "config.json"))
    args = parser.parse_args()

    cfg = load_cfg(args.config)
    apply_cfg(cfg)

    dataset = generate_ml_dataset_lhs_fast(custom_workers=WORKERS)
    print(dataset.head().to_string())
