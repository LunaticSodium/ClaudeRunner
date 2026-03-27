using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Windows;
using System.Windows.Controls;

using Forms = System.Windows.Forms;
using MessageBox = System.Windows.MessageBox;
using OpenFileDialog = Microsoft.Win32.OpenFileDialog;
using SaveFileDialog = Microsoft.Win32.SaveFileDialog;

namespace bto_sim
{
    public partial class MainWindow : Window
    {
        private readonly Dictionary<FieldKey, List<FrameworkElement>> _fieldElements = new();
        private SolverSpec? _currentSpec;

        // Combo item lists (populated once)
        private static readonly string[] StackTopologies  = new[] { "Patch", "Sandwich" };
        private static readonly string[] TopCoreMaterials = new[] { "sio2", "al2o3", "sin" };
        private static readonly string[] SpacerMaterials  = new[] { "air", "sio2", "al2o3", "water", "sin" };

        public MainWindow()
        {
            InitializeComponent();

            InitSolverCombo();
            InitSweepCombos();
            RegisterFields();
            ApplySolverSpecFromSelection();

            Status("Ready.");
        }

        // ---------- Solver selection ----------

        private void InitSolverCombo()
        {
            TypeCombo.ItemsSource = SolverCatalog.Solvers;
            TypeCombo.DisplayMemberPath = "DisplayName";
            TypeCombo.SelectedValuePath = "Type";

            if (TypeCombo.Items.Count > 0)
                TypeCombo.SelectedIndex = 0;
        }

        private void InitSweepCombos()
        {
            StackTopologyCombo.ItemsSource = StackTopologies;
            StackTopologyCombo.SelectedIndex = 0;   // "Patch"

            TopCoreCombo.ItemsSource = TopCoreMaterials;
            TopCoreCombo.SelectedIndex = 0;

            SpacerCombo.ItemsSource = SpacerMaterials;
            SpacerCombo.SelectedIndex = 0;
        }

        private void StackTopologyCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
        {
            ApplyTopologySpacerRule();
        }

        private void ApplyTopologySpacerRule()
        {
            bool isPatch = StackTopologyCombo.SelectedItem?.ToString() == "Patch";
            // SpacerCombo is irrelevant for Patch (Python forces air); grey it out
            SpacerCombo.IsEnabled  = !isPatch;
            SpacerCombo.Opacity    = isPatch ? 0.45 : 1.0;
            SpacerMatLabel.IsEnabled = !isPatch;
            SpacerMatLabel.Opacity = isPatch ? 0.45 : 1.0;
        }

        private void TypeCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
        {
            ApplySolverSpecFromSelection();
        }

        // ---------- UI wiring: enable/disable by solver type ----------

        private void Register(FieldKey key, params FrameworkElement[] elements)
        {
            if (!_fieldElements.TryGetValue(key, out var list))
            {
                list = new List<FrameworkElement>();
                _fieldElements[key] = list;
            }
            list.AddRange(elements);
        }

        private void RegisterFields()
        {
            _fieldElements.Clear();

            // ── Universal ──────────────────────────────────────────────────────
            Register(FieldKey.Type, TypeLabel, TypeCombo);
            Register(FieldKey.SchemaVersion, SchemaLabel, SchemaBox);
            Register(FieldKey.WavelengthUm, WavelengthLabel, WavelengthBox);
            Register(FieldKey.OutputDir, OutputDirLabel, OutputDirBox, BrowseOutputDirButton);
            Register(FieldKey.DirPrefix, DirPrefixLabel, DirPrefixBox);

            // ── LHS fast ──────────────────────────────────────────────────────
            Register(FieldKey.NConfigsPerType, NConfigsLabel, NConfigsBox);
            Register(FieldKey.DeviceTypes, DeviceTypesLabel, DeviceFlatCheck, DeviceRidgeCheck);
            Register(FieldKey.RandomSeed, SeedLabel, SeedBox);
            Register(FieldKey.Workers, WorkersLabel, WorkersBox);
            Register(FieldKey.VoltLow, VlowLabel, VlowBox);
            Register(FieldKey.VoltHigh, VhighLabel, VhighBox);

            Register(FieldKey.Range_BtoThickness, BtoTLabel, BtoTMinBox, BtoTMaxBox);
            Register(FieldKey.Range_Width, WLabel, WMinBox, WMaxBox);
            Register(FieldKey.Range_Height, HLabel, HMinBox, HMaxBox);
            Register(FieldKey.Range_ElectrodeGap, GapLabel, GapMinBox, GapMaxBox);
            Register(FieldKey.Range_ElectrodeHeight, EHLabel, EHMinBox, EHMaxBox);
            Register(FieldKey.Range_RidgeAngle, RidgeLabel, RidgeMinBox, RidgeMaxBox);
            Register(FieldKey.Range_CrystalAngle, CryLabel, CryMinBox, CryMaxBox);

            // ── Sandwich sweep ─────────────────────────────────────────────────
            Register(FieldKey.Sweep_StackTopology, StackTopologyLabel, StackTopologyCombo);
            Register(FieldKey.Sweep_TopCoreMaterial, TopCoreLabel, TopCoreCombo);
            Register(FieldKey.Sweep_SpacerMaterial, SpacerMatLabel, SpacerCombo);
            Register(FieldKey.Sweep_VoltageV, VoltVLabel, VoltVBox);
            Register(FieldKey.Sweep_PhiDeg, PhiDegLabel, PhiDegBox);
            Register(FieldKey.Sweep_NModes, NModesLabel, NModesBox);
            Register(FieldKey.Sweep_MinTeFraction, MinTEFracLabel, MinTEFracBox);
            Register(FieldKey.Sweep_TimeLimitSec, TimeLimitLabel, TimeLimitBox);
            Register(FieldKey.Sweep_TopK, TopKLabel, TopKBox);
            Register(FieldKey.Sweep_SweepRandomSeed, SweepSeedLabel, SweepSeedBox);
            Register(FieldKey.Sweep_OptGap, OptGapLabel, OptGapCheck);
            Register(FieldKey.Sweep_SaveJson, SaveJsonLabel, SaveJsonBox, BrowseSaveJsonButton);

            Register(FieldKey.Geom_Al2O3Thickness, Al2O3ThkLabel, Al2O3ThkBox);
            Register(FieldKey.Geom_BtoThickness, BtoThkLabel, BtoThkBox);
            Register(FieldKey.Geom_SpacerThickness, SpacerThkLabel, SpacerThkBox);
            Register(FieldKey.Geom_TopWidth, TopWidthLabel, TopWidthBox);
            Register(FieldKey.Geom_TopHeight, TopHeightLabel, TopHeightBox);
            Register(FieldKey.Geom_ElectrodeGap, ElecGapLabel, ElecGapBox);
        }

        private void ApplySolverSpecFromSelection()
        {
            _currentSpec = TypeCombo.SelectedItem as SolverSpec;
            if (_currentSpec == null) return;

            foreach (var kv in _fieldElements)
            {
                bool enabled = _currentSpec.EnabledFields.Contains(kv.Key);
                SetElementsEnabled(kv.Value, enabled);
            }

            // If a device type checkbox is disabled by spec, force uncheck it
            if (!DeviceFlatCheck.IsEnabled) DeviceFlatCheck.IsChecked = false;
            if (!DeviceRidgeCheck.IsEnabled) DeviceRidgeCheck.IsChecked = false;

            // Force rule: dir_prefix always auto-filled + read-only + grey
            DirPrefixBox.Text = _currentSpec.Type;
            DirPrefixBox.IsReadOnly = true;
            DirPrefixBox.IsEnabled = false;
            DirPrefixBox.Opacity = 0.45;
            DirPrefixLabel.Opacity = 0.45;

            // Schema version auto-fills based on solver type
            if (_currentSpec.Type.StartsWith("bto_sandwich_sweep"))
                SchemaBox.Text = "0.2.0";
            else if (_currentSpec.Type == "bto_ml_dataset_lhs_fast")
                SchemaBox.Text = "0.1.0";

            // Populate sweep defaults when switching to a sweep solver
            if (_currentSpec.EnabledFields.Contains(FieldKey.Sweep_VoltageV) && string.IsNullOrWhiteSpace(VoltVBox.Text))
                SetSweepDefaults();

            // Apply topology-driven spacer greying (on top of the EnabledFields greying)
            if (_currentSpec.EnabledFields.Contains(FieldKey.Sweep_StackTopology))
                ApplyTopologySpacerRule();

            Status($"Selected type: {_currentSpec.Type}");
        }

        private void SetSweepDefaults()
        {
            VoltVBox.Text = "3.0";
            PhiDegBox.Text = "45.0";
            NModesBox.Text = "8";
            MinTEFracBox.Text = "0.85";
            TimeLimitBox.Text = "3600";
            TopKBox.Text = "3";
            Al2O3ThkBox.Text = "0.026";
            BtoThkBox.Text = "0.150";
            SpacerThkBox.Text = "0.050";
            TopWidthBox.Text = "1.000";
            TopHeightBox.Text = "0.150";
            ElecGapBox.Text = "4.400";
        }

        private static void SetElementsEnabled(IEnumerable<FrameworkElement> elements, bool enabled)
        {
            foreach (var el in elements)
            {
                el.IsEnabled = enabled;
                el.Opacity = enabled ? 1.0 : 0.45;

                if (el is System.Windows.Controls.TextBox tb)
                    tb.IsReadOnly = !enabled;
            }
        }

        // ---------- Load / Save / Validate ----------

        private void Load_Click(object sender, RoutedEventArgs e)
        {
            try
            {
                var dlg = new OpenFileDialog
                {
                    Title = "Load config.json",
                    Filter = "JSON files (*.json)|*.json|All files (*.*)|*.*",
                    InitialDirectory = DefaultConfigsDir()
                };

                if (dlg.ShowDialog() != true) return;

                var cfg = ConfigIO.Load(dlg.FileName);
                ApplyToUI(cfg);

                Status($"Loaded: {dlg.FileName}");
            }
            catch (Exception ex)
            {
                MessageBox.Show(ex.Message, "Load failed", MessageBoxButton.OK, MessageBoxImage.Error);
                Status("Load failed.");
            }
        }

        private void Save_Click(object sender, RoutedEventArgs e)
        {
            try
            {
                var issues = ValidateUI();
                if (issues.Count > 0)
                {
                    MessageBox.Show(string.Join("\n", issues), "Validation failed", MessageBoxButton.OK, MessageBoxImage.Warning);
                    Status("Save blocked: validation failed.");
                    return;
                }

                var dlg = new SaveFileDialog
                {
                    Title = "Save config.json",
                    Filter = "JSON files (*.json)|*.json|All files (*.*)|*.*",
                    InitialDirectory = DefaultConfigsDir(),
                    FileName = "config.json"
                };

                if (dlg.ShowDialog() != true) return;

                var cfg = ReadFromUI();
                ConfigIO.Save(dlg.FileName, cfg);

                Status($"Saved: {dlg.FileName}");
            }
            catch (Exception ex)
            {
                MessageBox.Show(ex.Message, "Save failed", MessageBoxButton.OK, MessageBoxImage.Error);
                Status("Save failed.");
            }
        }

        private void Validate_Click(object sender, RoutedEventArgs e)
        {
            var issues = ValidateUI();
            if (issues.Count == 0)
            {
                MessageBox.Show("OK ✅", "Validate", MessageBoxButton.OK, MessageBoxImage.Information);
                Status("Validate OK.");
            }
            else
            {
                MessageBox.Show(string.Join("\n", issues), "Validate", MessageBoxButton.OK, MessageBoxImage.Warning);
                Status("Validate failed.");
            }
        }

        private void BrowseOutputDir_Click(object sender, RoutedEventArgs e)
        {
            using var dlg = new Forms.FolderBrowserDialog
            {
                Description = "Select output folder (outside repo is recommended)",
                UseDescriptionForTitle = true
            };

            if (!string.IsNullOrWhiteSpace(OutputDirBox.Text) && Directory.Exists(OutputDirBox.Text))
                dlg.SelectedPath = OutputDirBox.Text;

            if (dlg.ShowDialog() == Forms.DialogResult.OK)
                OutputDirBox.Text = dlg.SelectedPath;
        }

        private void BrowseSaveJson_Click(object sender, RoutedEventArgs e)
        {
            var dlg = new SaveFileDialog
            {
                Title = "Select output JSON path for sweep report",
                Filter = "JSON files (*.json)|*.json|All files (*.*)|*.*",
                FileName = "sweep_report.json"
            };

            if (!string.IsNullOrWhiteSpace(OutputDirBox.Text) && Directory.Exists(OutputDirBox.Text))
                dlg.InitialDirectory = OutputDirBox.Text;

            if (dlg.ShowDialog() == true)
                SaveJsonBox.Text = dlg.FileName;
        }

        private void Run_Click(object sender, RoutedEventArgs e)
        {
            MessageBox.Show("Run not implemented yet. Next step: start python + stdout progress.", "Info");
        }

        private void OpenOutputDir_Click(object sender, RoutedEventArgs e)
        {
            var dir = GetEffectiveOutputDir();
            if (!Directory.Exists(dir))
            {
                MessageBox.Show($"Output directory not found:\n{dir}", "Open Output", MessageBoxButton.OK, MessageBoxImage.Warning);
                return;
            }
            Process.Start("explorer.exe", dir);
        }

        private void OpenLatest_Click(object sender, RoutedEventArgs e)
        {
            var dir = GetEffectiveOutputDir();
            if (!Directory.Exists(dir))
            {
                MessageBox.Show($"Output directory not found:\n{dir}", "Open Latest", MessageBoxButton.OK, MessageBoxImage.Warning);
                return;
            }
            var latest = new DirectoryInfo(dir).GetDirectories()
                .OrderByDescending(d => d.LastWriteTime).FirstOrDefault();
            if (latest == null)
            {
                MessageBox.Show("No run subdirectories found yet.", "Open Latest", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            Process.Start("explorer.exe", latest.FullName);
        }

        private string GetEffectiveOutputDir()
        {
            var configured = OutputDirBox.Text.Trim();
            if (!string.IsNullOrWhiteSpace(configured) && Directory.Exists(configured))
                return configured;
            return Path.Combine(FindProjectRoot(), "output");
        }

        // ---------- Mapping between UI and Config ----------

        private void ApplyToUI(Config cfg)
        {
            // type (ComboBox) — must come first so ApplySolverSpecFromSelection fires
            TypeCombo.SelectedValue = cfg.Type;

            SchemaBox.Text = cfg.SchemaVersion;
            WavelengthBox.Text = cfg.WavelengthUm.ToString(CultureInfo.InvariantCulture);

            // LHS fast fields
            NConfigsBox.Text = cfg.NConfigsPerType?.ToString(CultureInfo.InvariantCulture) ?? "";
            DeviceFlatCheck.IsChecked  = cfg.DeviceTypes?.Contains("flat")  == true;
            DeviceRidgeCheck.IsChecked = cfg.DeviceTypes?.Contains("ridge") == true;
            SeedBox.Text = cfg.RandomSeed?.ToString(CultureInfo.InvariantCulture) ?? "";
            WorkersBox.Text = cfg.Workers?.ToString(CultureInfo.InvariantCulture) ?? "";

            if (cfg.Voltages != null)
            {
                VlowBox.Text  = cfg.Voltages.Low.ToString(CultureInfo.InvariantCulture);
                VhighBox.Text = cfg.Voltages.High.ToString(CultureInfo.InvariantCulture);
            }
            else
            {
                VlowBox.Text = "";
                VhighBox.Text = "";
            }

            OutputDirBox.Text  = cfg.Output.OutputDir ?? "";
            DirPrefixBox.Text  = cfg.Output.DirPrefix ?? "";
            SaveJsonBox.Text   = cfg.Output.SaveJson  ?? "";

            if (cfg.ParameterRanges != null)
            {
                SetRange(BtoTMinBox, BtoTMaxBox, cfg.ParameterRanges.BtoThicknessUm);
                SetRange(WMinBox, WMaxBox, cfg.ParameterRanges.WidthUm);
                SetRange(HMinBox, HMaxBox, cfg.ParameterRanges.HeightUm);
                SetRange(GapMinBox, GapMaxBox, cfg.ParameterRanges.ElectrodeGapUm);
                SetRange(EHMinBox, EHMaxBox, cfg.ParameterRanges.ElectrodeHeightUm);
                SetRange(RidgeMinBox, RidgeMaxBox, cfg.ParameterRanges.RidgeAngleDeg);
                SetRange(CryMinBox, CryMaxBox, cfg.ParameterRanges.CrystalAngleDeg);
            }

            // Sweep fields — reverse-derive topology from structure_family
            ApplyStructureFamilyToUI(cfg.StructureFamily, cfg.TopCoreMaterial, cfg.SpacerMaterial);

            VoltVBox.Text       = cfg.VoltageV?.ToString(CultureInfo.InvariantCulture) ?? "";
            PhiDegBox.Text      = cfg.PhiDeg?.ToString(CultureInfo.InvariantCulture) ?? "";
            NModesBox.Text      = cfg.NModes?.ToString(CultureInfo.InvariantCulture) ?? "";
            MinTEFracBox.Text   = cfg.MinTeFraction?.ToString(CultureInfo.InvariantCulture) ?? "";
            TimeLimitBox.Text   = cfg.TimeLimitSec?.ToString(CultureInfo.InvariantCulture) ?? "";
            TopKBox.Text        = cfg.TopK?.ToString(CultureInfo.InvariantCulture) ?? "";
            SweepSeedBox.Text   = cfg.SweepRandomSeed?.ToString(CultureInfo.InvariantCulture) ?? "";
            OptGapCheck.IsChecked = cfg.OptGap == true;

            if (cfg.Geometry != null)
            {
                Al2O3ThkBox.Text  = cfg.Geometry.Al2O3ThicknessUm.ToString(CultureInfo.InvariantCulture);
                BtoThkBox.Text    = cfg.Geometry.BtoThicknessUm.ToString(CultureInfo.InvariantCulture);
                SpacerThkBox.Text = cfg.Geometry.SpacerThicknessUm.ToString(CultureInfo.InvariantCulture);
                TopWidthBox.Text  = cfg.Geometry.TopWidthUm.ToString(CultureInfo.InvariantCulture);
                TopHeightBox.Text = cfg.Geometry.TopHeightUm.ToString(CultureInfo.InvariantCulture);
                ElecGapBox.Text   = cfg.Geometry.ElectrodeGapUm.ToString(CultureInfo.InvariantCulture);
            }

            ApplySolverSpecFromSelection(); // re-apply enable/disable + prefix policy
        }

        private static void SetRange(System.Windows.Controls.TextBox minBox, System.Windows.Controls.TextBox maxBox, double[]? arr)
        {
            if (arr == null || arr.Length < 2)
            {
                minBox.Text = "";
                maxBox.Text = "";
                return;
            }
            minBox.Text = arr[0].ToString(CultureInfo.InvariantCulture);
            maxBox.Text = arr[1].ToString(CultureInfo.InvariantCulture);
        }

        private Config ReadFromUI()
        {
            var cfg = new Config();
            var enabled = _currentSpec?.EnabledFields ?? new HashSet<FieldKey>();

            cfg.Type          = (string?)TypeCombo.SelectedValue ?? "";
            cfg.SchemaVersion = SchemaBox.Text.Trim();
            cfg.WavelengthUm  = ParseDouble(WavelengthBox.Text);

            cfg.Output.OutputDir = OutputDirBox.Text.Trim();
            cfg.Output.DirPrefix = DirPrefixBox.Text.Trim();
            cfg.Output.SaveJson  = SaveJsonBox.Text.Trim();

            bool isLhs    = enabled.Contains(FieldKey.NConfigsPerType);
            bool isSweep  = enabled.Contains(FieldKey.Sweep_StackTopology);

            // LHS fast fields
            if (isLhs)
            {
                cfg.NConfigsPerType = ParseInt(NConfigsBox.Text);

                cfg.DeviceTypes = new List<string>();
                if (DeviceFlatCheck.IsChecked  == true) cfg.DeviceTypes.Add("flat");
                if (DeviceRidgeCheck.IsChecked == true) cfg.DeviceTypes.Add("ridge");

                cfg.RandomSeed = TryParseInt(SeedBox.Text, out var seed) ? seed : (int?)null;
                cfg.Workers    = string.IsNullOrWhiteSpace(WorkersBox.Text) ? null : ParseInt(WorkersBox.Text);

                cfg.Voltages = new Voltages
                {
                    Low  = ParseDouble(VlowBox.Text),
                    High = ParseDouble(VhighBox.Text)
                };

                cfg.ParameterRanges = new ParameterRanges
                {
                    BtoThicknessUm  = ReadRange(BtoTMinBox, BtoTMaxBox),
                    WidthUm         = ReadRange(WMinBox, WMaxBox),
                    HeightUm        = ReadRange(HMinBox, HMaxBox),
                    ElectrodeGapUm  = ReadRange(GapMinBox, GapMaxBox),
                    ElectrodeHeightUm = ReadRange(EHMinBox, EHMaxBox),
                    RidgeAngleDeg   = ReadRange(RidgeMinBox, RidgeMaxBox),
                    CrystalAngleDeg = ReadRange(CryMinBox, CryMaxBox)
                };
            }
            else
            {
                cfg.NConfigsPerType = null;
                cfg.DeviceTypes     = null;
                cfg.RandomSeed      = null;
                cfg.Workers         = null;
                cfg.Voltages        = null;
                cfg.ParameterRanges = null;
            }

            // Sweep fields
            if (isSweep)
            {
                string? topCore = ComboReadString(TopCoreCombo);
                string? spacer  = ComboReadString(SpacerCombo);
                bool isSandwich = StackTopologyCombo.SelectedItem?.ToString() == "Sandwich";
                cfg.StructureFamily = DeriveStructureFamily(topCore, spacer, isSandwich);
                cfg.TopCoreMaterial = topCore;
                cfg.SpacerMaterial  = isSandwich ? spacer : "air"; // Patch always uses air
                cfg.VoltageV        = ParseDouble(VoltVBox.Text);
                cfg.PhiDeg          = ParseDouble(PhiDegBox.Text);
                cfg.NModes          = ParseInt(NModesBox.Text);
                cfg.MinTeFraction   = ParseDouble(MinTEFracBox.Text);
                cfg.TimeLimitSec    = ParseDouble(TimeLimitBox.Text);
                cfg.TopK            = ParseInt(TopKBox.Text);
                cfg.SweepRandomSeed = string.IsNullOrWhiteSpace(SweepSeedBox.Text) ? null : ParseInt(SweepSeedBox.Text);
                cfg.OptGap          = enabled.Contains(FieldKey.Sweep_OptGap) ? (bool?)OptGapCheck.IsChecked : null;

                cfg.Geometry = new SweepGeometry
                {
                    Al2O3ThicknessUm = ParseDouble(Al2O3ThkBox.Text),
                    BtoThicknessUm   = ParseDouble(BtoThkBox.Text),
                    SpacerThicknessUm = ParseDouble(SpacerThkBox.Text),
                    TopWidthUm       = ParseDouble(TopWidthBox.Text),
                    TopHeightUm      = ParseDouble(TopHeightBox.Text),
                    ElectrodeGapUm   = ParseDouble(ElecGapBox.Text)
                };
            }
            else
            {
                cfg.StructureFamily = null;
                cfg.TopCoreMaterial = null;
                cfg.SpacerMaterial  = null;
                cfg.VoltageV        = null;
                cfg.PhiDeg          = null;
                cfg.NModes          = null;
                cfg.MinTeFraction   = null;
                cfg.TimeLimitSec    = null;
                cfg.TopK            = null;
                cfg.SweepRandomSeed = null;
                cfg.OptGap          = null;
                cfg.Geometry        = null;
            }

            return cfg;
        }

        private static double[] ReadRange(System.Windows.Controls.TextBox minBox, System.Windows.Controls.TextBox maxBox)
        {
            return new[]
            {
                ParseDouble(minBox.Text),
                ParseDouble(maxBox.Text)
            };
        }

        // ---------- Validation ----------

        private List<string> ValidateUI()
        {
            var issues = new List<string>();
            var enabled = _currentSpec?.EnabledFields ?? new HashSet<FieldKey>();

            void Req(FieldKey key, string name, Func<bool> check)
            {
                if (!enabled.Contains(key)) return;
                if (!check()) issues.Add(name);
            }

            // Universal
            Req(FieldKey.Type, "type: must be selected", () => TypeCombo.SelectedValue is string s && !string.IsNullOrWhiteSpace(s));
            Req(FieldKey.SchemaVersion, "schema_version: cannot be empty", () => !string.IsNullOrWhiteSpace(SchemaBox.Text));
            Req(FieldKey.WavelengthUm, "wavelength_um: must be double > 0", () => TryParseDouble(WavelengthBox.Text, out var wl) && wl > 0);

            // LHS fast
            Req(FieldKey.NConfigsPerType, "n_configs_per_type: must be int > 0", () => TryParseInt(NConfigsBox.Text, out var v) && v > 0);
            Req(FieldKey.DeviceTypes, "device_types: select at least one", () => (DeviceFlatCheck.IsChecked == true) || (DeviceRidgeCheck.IsChecked == true));
            Req(FieldKey.RandomSeed, "random_seed: must be int", () => TryParseInt(SeedBox.Text, out _));
            Req(FieldKey.Workers, "workers: must be empty or int > 0", () =>
                string.IsNullOrWhiteSpace(WorkersBox.Text) || (TryParseInt(WorkersBox.Text, out var w) && w > 0));

            Req(FieldKey.VoltLow,  "voltages_V.low: must be double",  () => TryParseDouble(VlowBox.Text,  out _));
            Req(FieldKey.VoltHigh, "voltages_V.high: must be double", () => TryParseDouble(VhighBox.Text, out _));
            if (enabled.Contains(FieldKey.VoltLow) && enabled.Contains(FieldKey.VoltHigh))
                if (TryParseDouble(VlowBox.Text, out var lo) && TryParseDouble(VhighBox.Text, out var hi))
                    if (!(lo < hi)) issues.Add("voltages_V: require low < high");

            ValidateRange(enabled, issues, FieldKey.Range_BtoThickness,    "bto_thickness_um",    BtoTMinBox, BtoTMaxBox);
            ValidateRange(enabled, issues, FieldKey.Range_Width,            "width_um",            WMinBox,    WMaxBox);
            ValidateRange(enabled, issues, FieldKey.Range_Height,           "height_um",           HMinBox,    HMaxBox);
            ValidateRange(enabled, issues, FieldKey.Range_ElectrodeGap,     "electrode_gap_um",    GapMinBox,  GapMaxBox);
            ValidateRange(enabled, issues, FieldKey.Range_ElectrodeHeight,  "electrode_height_um", EHMinBox,   EHMaxBox);
            ValidateRange(enabled, issues, FieldKey.Range_RidgeAngle,       "ridge_angle_deg",     RidgeMinBox, RidgeMaxBox);
            ValidateRange(enabled, issues, FieldKey.Range_CrystalAngle,     "crystal_angle_deg",   CryMinBox,  CryMaxBox);

            // Sweep
            Req(FieldKey.Sweep_StackTopology,   "stack_topology: must be selected",    () => StackTopologyCombo.SelectedItem != null);
            Req(FieldKey.Sweep_TopCoreMaterial, "top_core_material: must be selected", () => ComboReadString(TopCoreCombo) != null);
            Req(FieldKey.Sweep_SpacerMaterial,  "spacer_material: must be selected when topology is Sandwich",
                () => StackTopologyCombo.SelectedItem?.ToString() == "Patch" || ComboReadString(SpacerCombo) != null);
            Req(FieldKey.Sweep_VoltageV,        "voltage_v: must be double > 0",  () => TryParseDouble(VoltVBox.Text, out var v) && v > 0);
            Req(FieldKey.Sweep_PhiDeg,          "phi_deg: must be double 0–90",   () => TryParseDouble(PhiDegBox.Text, out var p) && p >= 0 && p <= 90);
            Req(FieldKey.Sweep_NModes,          "n_modes: must be int > 0",        () => TryParseInt(NModesBox.Text, out var n) && n > 0);
            Req(FieldKey.Sweep_MinTeFraction,   "min_te_fraction: must be 0–1",    () => TryParseDouble(MinTEFracBox.Text, out var f) && f >= 0 && f <= 1);
            Req(FieldKey.Sweep_TimeLimitSec,    "time_limit_sec: must be double > 0", () => TryParseDouble(TimeLimitBox.Text, out var t) && t > 0);
            Req(FieldKey.Sweep_TopK,            "top_k: must be int > 0",          () => TryParseInt(TopKBox.Text, out var k) && k > 0);
            Req(FieldKey.Sweep_SweepRandomSeed, "sweep_random_seed: must be empty or int", () =>
                string.IsNullOrWhiteSpace(SweepSeedBox.Text) || TryParseInt(SweepSeedBox.Text, out _));

            Req(FieldKey.Geom_Al2O3Thickness, "al2o3_thickness_um: must be double > 0", () => TryParseDouble(Al2O3ThkBox.Text, out var v) && v > 0);
            Req(FieldKey.Geom_BtoThickness,   "bto_thickness_um: must be double > 0",   () => TryParseDouble(BtoThkBox.Text,   out var v) && v > 0);
            Req(FieldKey.Geom_SpacerThickness, "spacer_thickness_um: must be double >= 0", () => TryParseDouble(SpacerThkBox.Text, out var v) && v >= 0);
            Req(FieldKey.Geom_TopWidth,        "top_width_um: must be double > 0",  () => TryParseDouble(TopWidthBox.Text,  out var v) && v > 0);
            Req(FieldKey.Geom_TopHeight,       "top_height_um: must be double > 0", () => TryParseDouble(TopHeightBox.Text, out var v) && v > 0);
            Req(FieldKey.Geom_ElectrodeGap,    "electrode_gap_um: must be double > 0", () => TryParseDouble(ElecGapBox.Text, out var v) && v > 0);

            return issues;
        }

        private static void ValidateRange(HashSet<FieldKey> enabled, List<string> issues,
            FieldKey key, string name, System.Windows.Controls.TextBox minBox, System.Windows.Controls.TextBox maxBox)
        {
            if (!enabled.Contains(key)) return;
            if (!TryParseDouble(minBox.Text, out var a)) { issues.Add($"{name}.min: must be double"); return; }
            if (!TryParseDouble(maxBox.Text, out var b)) { issues.Add($"{name}.max: must be double"); return; }
            if (!(a < b)) issues.Add($"{name}: require min < max");
        }

        // ---------- Structure family helpers ----------

        /// <summary>
        /// Derives the canonical structure_family string from GUI topology + material selections.
        /// The result is written to the JSON config so Python can reproduce the run exactly.
        /// </summary>
        private static string DeriveStructureFamily(string? topCore, string? spacer, bool isSandwich)
        {
            string core  = (topCore ?? "sio2").ToLowerInvariant();
            string space = (spacer  ?? "air").ToLowerInvariant();

            if (!isSandwich)
            {
                return core switch
                {
                    "sio2"  => "sio2_patch",
                    "al2o3" => "patch_al2o3",
                    "sin"   => "patch_sin",
                    _       => "custom"
                };
            }

            // Sandwich — look for a named family first
            if (core == "sio2"  && space == "air")   return "sio2_air_bto";
            if (core == "al2o3" && space == "sio2")  return "sandwich_al2o3_sio2";
            if (core == "sio2"  && space == "al2o3") return "sandwich_sio2_al2o3";

            // Any other combination → custom (Python accepts it)
            return "custom";
        }

        /// <summary>
        /// Reverse-derives topology + material GUI state from a loaded structure_family value.
        /// Also accepts explicit top_core/spacer overrides for the "custom" case.
        /// </summary>
        private void ApplyStructureFamilyToUI(string? family, string? topCore, string? spacer)
        {
            string fam = (family ?? "").ToLowerInvariant().Trim();

            switch (fam)
            {
                case "sio2_patch":
                case "patch_sio2":
                    ComboSelectString(StackTopologyCombo, "Patch");
                    ComboSelectString(TopCoreCombo, "sio2");
                    break;
                case "patch_al2o3":
                    ComboSelectString(StackTopologyCombo, "Patch");
                    ComboSelectString(TopCoreCombo, "al2o3");
                    break;
                case "patch_sin":
                    ComboSelectString(StackTopologyCombo, "Patch");
                    ComboSelectString(TopCoreCombo, "sin");
                    break;
                case "sio2_air_bto":
                    ComboSelectString(StackTopologyCombo, "Sandwich");
                    ComboSelectString(TopCoreCombo, "sio2");
                    ComboSelectString(SpacerCombo, "air");
                    break;
                case "al2o3_sio2_bto":
                case "sandwich_al2o3_sio2":
                    ComboSelectString(StackTopologyCombo, "Sandwich");
                    ComboSelectString(TopCoreCombo, "al2o3");
                    ComboSelectString(SpacerCombo, "sio2");
                    break;
                case "sandwich_sio2_al2o3":
                    ComboSelectString(StackTopologyCombo, "Sandwich");
                    ComboSelectString(TopCoreCombo, "sio2");
                    ComboSelectString(SpacerCombo, "al2o3");
                    break;
                default:
                    // "custom" or unknown — use whatever is in top_core_material / spacer_material
                    ComboSelectString(StackTopologyCombo, "Sandwich");
                    ComboSelectString(TopCoreCombo,  topCore ?? "sio2");
                    ComboSelectString(SpacerCombo, spacer ?? "air");
                    break;
            }

            ApplyTopologySpacerRule();
        }

        // ---------- Combo helpers ----------

        private static void ComboSelectString(object comboObj, string? value)
        {
            if (value == null) return;
            if (comboObj is System.Windows.Controls.ComboBox combo)
            {
                foreach (var item in combo.Items)
                {
                    if (item?.ToString() == value)
                    {
                        combo.SelectedItem = item;
                        return;
                    }
                }
            }
        }

        private static string? ComboReadString(object comboObj)
        {
            if (comboObj is System.Windows.Controls.ComboBox combo)
                return combo.SelectedItem?.ToString();
            return null;
        }

        // ---------- Utils ----------

        private void Status(string msg) => StatusText.Text = msg;

        private static string DefaultConfigsDir()
        {
            var root = FindProjectRoot();
            var dir = Path.Combine(root, "configs");
            return Directory.Exists(dir) ? dir : Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
        }

        private static string FindProjectRoot()
        {
            var dir = new DirectoryInfo(AppContext.BaseDirectory);
            for (int i = 0; i < 8 && dir != null; i++)
            {
                if (dir.GetFiles("*.csproj").Any() ||
                    (Directory.Exists(Path.Combine(dir.FullName, "configs")) &&
                     Directory.Exists(Path.Combine(dir.FullName, "py"))))
                    return dir.FullName;
                dir = dir.Parent;
            }
            return AppContext.BaseDirectory;
        }

        private static int ParseInt(string s)
        {
            if (!TryParseInt(s, out var v)) throw new FormatException($"Invalid int: {s}");
            return v;
        }

        private static double ParseDouble(string s)
        {
            if (!TryParseDouble(s, out var v)) throw new FormatException($"Invalid double: {s}");
            return v;
        }

        private static bool TryParseInt(string s, out int v)
            => int.TryParse(s.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out v);

        private static bool TryParseDouble(string s, out double v)
            => double.TryParse(s.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out v);
    }
}
