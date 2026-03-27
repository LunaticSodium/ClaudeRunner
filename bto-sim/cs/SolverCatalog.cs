using System.Collections.Generic;

namespace bto_sim
{
    public enum FieldKey
    {
        // ── Universal ──────────────────────────────────────────────────────────
        Type,
        SchemaVersion,
        WavelengthUm,
        OutputDir,
        DirPrefix,

        // ── LHS fast specific ─────────────────────────────────────────────────
        NConfigsPerType,
        DeviceTypes,
        RandomSeed,
        Workers,
        VoltLow,
        VoltHigh,

        Range_BtoThickness,
        Range_Width,
        Range_Height,
        Range_ElectrodeGap,
        Range_ElectrodeHeight,
        Range_RidgeAngle,
        Range_CrystalAngle,

        // ── Sandwich sweep specific ────────────────────────────────────────────
        Sweep_StackTopology,
        Sweep_TopCoreMaterial,
        Sweep_SpacerMaterial,
        Sweep_VoltageV,
        Sweep_PhiDeg,
        Sweep_NModes,
        Sweep_MinTeFraction,
        Sweep_TimeLimitSec,
        Sweep_TopK,
        Sweep_SweepRandomSeed,
        Sweep_OptGap,
        Sweep_SaveJson,

        Geom_Al2O3Thickness,
        Geom_BtoThickness,
        Geom_SpacerThickness,
        Geom_TopWidth,
        Geom_TopHeight,
        Geom_ElectrodeGap,
    }

    public sealed class SolverSpec
    {
        public string Type { get; init; } = "";
        public string DisplayName { get; init; } = "";
        public string ScriptRelPath { get; init; } = "";
        public HashSet<FieldKey> EnabledFields { get; init; } = new();
    }

    public static class SolverCatalog
    {
        // Shared sweep fields — all features of the merged solver
        private static readonly HashSet<FieldKey> SweepFields = new()
        {
            FieldKey.Type,
            FieldKey.SchemaVersion,
            FieldKey.WavelengthUm,
            FieldKey.OutputDir,
            FieldKey.DirPrefix,

            FieldKey.Sweep_StackTopology,
            FieldKey.Sweep_TopCoreMaterial,
            FieldKey.Sweep_SpacerMaterial,
            FieldKey.Sweep_VoltageV,
            FieldKey.Sweep_PhiDeg,
            FieldKey.Sweep_NModes,
            FieldKey.Sweep_MinTeFraction,
            FieldKey.Sweep_TimeLimitSec,
            FieldKey.Sweep_TopK,
            FieldKey.Sweep_SweepRandomSeed,
            FieldKey.Sweep_OptGap,
            FieldKey.Sweep_SaveJson,

            FieldKey.Geom_Al2O3Thickness,
            FieldKey.Geom_BtoThickness,
            FieldKey.Geom_SpacerThickness,
            FieldKey.Geom_TopWidth,
            FieldKey.Geom_TopHeight,
            FieldKey.Geom_ElectrodeGap,
        };

        public static readonly List<SolverSpec> Solvers = new()
        {
            // ── ML Dataset LHS Fast ───────────────────────────────────────────
            new SolverSpec
            {
                Type = "bto_ml_dataset_lhs_fast",
                DisplayName = "ML Dataset — LHS Fast",
                ScriptRelPath = @"py\solvers\BTO_ML_Dataset_Generator_LHS_Fast.py",
                EnabledFields = new HashSet<FieldKey>
                {
                    FieldKey.Type,
                    FieldKey.SchemaVersion,
                    FieldKey.NConfigsPerType,
                    FieldKey.DeviceTypes,
                    FieldKey.RandomSeed,
                    FieldKey.Workers,
                    FieldKey.WavelengthUm,
                    FieldKey.VoltLow,
                    FieldKey.VoltHigh,
                    FieldKey.OutputDir,
                    FieldKey.DirPrefix,

                    FieldKey.Range_BtoThickness,
                    FieldKey.Range_Width,
                    FieldKey.Range_Height,
                    FieldKey.Range_ElectrodeGap,
                    FieldKey.Range_ElectrodeHeight,
                    FieldKey.Range_RidgeAngle,
                    FieldKey.Range_CrystalAngle,
                }
            },

            // ── Sandwich Sweep (unified — 9 families, opt_gap) ────────────────
            new SolverSpec
            {
                Type = "bto_sandwich_sweep",
                DisplayName = "Sandwich Sweep — Geometry Optimizer",
                ScriptRelPath = @"py\solvers\Sandwich_Autosweeper.py",
                EnabledFields = SweepFields,
            },

            // ── Device sweep placeholder ──────────────────────────────────────
            new SolverSpec
            {
                Type = "bto_device_sweep",
                DisplayName = "Device sweep (placeholder)",
                ScriptRelPath = "",
                EnabledFields = new HashSet<FieldKey>
                {
                    FieldKey.Type,
                    FieldKey.SchemaVersion,
                    FieldKey.DeviceTypes,
                    FieldKey.WavelengthUm,
                    FieldKey.VoltLow,
                    FieldKey.VoltHigh,

                    FieldKey.Range_Width,
                    FieldKey.Range_Height,
                }
            }
        };
    }
}
