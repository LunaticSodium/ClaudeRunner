using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace bto_sim
{
    public sealed class Config
    {
        // ── Universal ─────────────────────────────────────────────────────────
        [JsonPropertyName("type")]
        public string Type { get; set; } = "bto_ml_dataset_lhs_fast";

        [JsonPropertyName("schema_version")]
        public string SchemaVersion { get; set; } = "0.1.0";

        [JsonPropertyName("wavelength_um")]
        public double WavelengthUm { get; set; } = 1.55;

        [JsonPropertyName("output")]
        public OutputSpec Output { get; set; } = new OutputSpec();

        // ── LHS fast only (null = omitted from JSON for sweep configs) ────────
        [JsonPropertyName("n_configs_per_type")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public int? NConfigsPerType { get; set; } = 100;

        [JsonPropertyName("device_types")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public List<string>? DeviceTypes { get; set; } = new() { "flat", "ridge" };

        [JsonPropertyName("random_seed")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public int? RandomSeed { get; set; } = 42;

        [JsonPropertyName("workers")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public int? Workers { get; set; } = null;

        [JsonPropertyName("voltages_V")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public Voltages? Voltages { get; set; } = new Voltages();

        [JsonPropertyName("parameter_ranges")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public ParameterRanges? ParameterRanges { get; set; } = new ParameterRanges();

        // ── Sandwich sweep specific (null = omitted for LHS fast configs) ─────
        [JsonPropertyName("structure_family")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public string? StructureFamily { get; set; } = null;

        [JsonPropertyName("top_core_material")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public string? TopCoreMaterial { get; set; } = null;

        [JsonPropertyName("spacer_material")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public string? SpacerMaterial { get; set; } = null;

        [JsonPropertyName("voltage_v")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public double? VoltageV { get; set; } = null;

        [JsonPropertyName("phi_deg")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public double? PhiDeg { get; set; } = null;

        [JsonPropertyName("n_modes")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public int? NModes { get; set; } = null;

        [JsonPropertyName("min_te_fraction")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public double? MinTeFraction { get; set; } = null;

        [JsonPropertyName("time_limit_sec")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public double? TimeLimitSec { get; set; } = null;

        [JsonPropertyName("top_k")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public int? TopK { get; set; } = null;

        [JsonPropertyName("sweep_random_seed")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public int? SweepRandomSeed { get; set; } = null;

        [JsonPropertyName("opt_gap")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public bool? OptGap { get; set; } = null;

        [JsonPropertyName("geometry")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public SweepGeometry? Geometry { get; set; } = null;
    }

    // ── Shared sub-types ──────────────────────────────────────────────────────

    public sealed class Voltages
    {
        [JsonPropertyName("low")]
        public double Low { get; set; } = 0.1;

        [JsonPropertyName("high")]
        public double High { get; set; } = 5.0;
    }

    public sealed class OutputSpec
    {
        [JsonPropertyName("output_dir")]
        public string OutputDir { get; set; } = "";

        [JsonPropertyName("dir_prefix")]
        public string DirPrefix { get; set; } = "";

        [JsonPropertyName("save_json")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingDefault)]
        public string SaveJson { get; set; } = "";
    }

    // ── LHS fast sub-types ────────────────────────────────────────────────────

    public sealed class ParameterRanges
    {
        [JsonPropertyName("bto_thickness_um")]
        public double[] BtoThicknessUm { get; set; } = new[] { 0.05, 0.5 };

        [JsonPropertyName("width_um")]
        public double[] WidthUm { get; set; } = new[] { 0.5, 2.0 };

        [JsonPropertyName("height_um")]
        public double[] HeightUm { get; set; } = new[] { 0.05, 0.35 };

        [JsonPropertyName("electrode_gap_um")]
        public double[] ElectrodeGapUm { get; set; } = new[] { 2.5, 7.0 };

        [JsonPropertyName("electrode_height_um")]
        public double[] ElectrodeHeightUm { get; set; } = new[] { 0.1, 1.0 };

        [JsonPropertyName("ridge_angle_deg")]
        public double[] RidgeAngleDeg { get; set; } = new[] { 0.0, 35.0 };

        [JsonPropertyName("crystal_angle_deg")]
        public double[] CrystalAngleDeg { get; set; } = new[] { 0.0, 90.0 };
    }

    // ── Sandwich sweep sub-types ──────────────────────────────────────────────

    public sealed class SweepGeometry
    {
        [JsonPropertyName("al2o3_thickness_um")]
        public double Al2O3ThicknessUm { get; set; } = 0.026;

        [JsonPropertyName("bto_thickness_um")]
        public double BtoThicknessUm { get; set; } = 0.150;

        [JsonPropertyName("spacer_thickness_um")]
        public double SpacerThicknessUm { get; set; } = 0.050;

        [JsonPropertyName("top_width_um")]
        public double TopWidthUm { get; set; } = 1.000;

        [JsonPropertyName("top_height_um")]
        public double TopHeightUm { get; set; } = 0.150;

        [JsonPropertyName("electrode_gap_um")]
        public double ElectrodeGapUm { get; set; } = 4.400;
    }

    // ── Config I/O ────────────────────────────────────────────────────────────

    public static class ConfigIO
    {
        private static readonly JsonSerializerOptions JsonOptions = new()
        {
            PropertyNamingPolicy = null,
            WriteIndented = true,
            ReadCommentHandling = JsonCommentHandling.Skip,
            AllowTrailingCommas = true
        };

        public static Config Load(string path)
        {
            var json = File.ReadAllText(path);
            var cfg = JsonSerializer.Deserialize<Config>(json, JsonOptions);
            return cfg ?? new Config();
        }

        public static void Save(string path, Config cfg)
        {
            var json = JsonSerializer.Serialize(cfg, JsonOptions);
            File.WriteAllText(path, json);
        }
    }
}
