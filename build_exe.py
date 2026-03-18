#!/usr/bin/env python3
"""
build_exe.py -- Build claude-runner.exe using PyInstaller.

Usage:
    python build_exe.py [--clean] [--debug]

Output:
    dist/claude-runner.exe   Single-file standalone executable for Windows.
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
ENTRY_POINT = PROJECT_ROOT / "claude_runner" / "main.py"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
SPEC_FILE = PROJECT_ROOT / "claude-runner.spec"
EXE_NAME = "claude-runner"
VERSION = "1.1.0"

# Submodules that PyInstaller may not detect automatically via static analysis
HIDDEN_IMPORTS = [
    # claude-runner own modules
    "claude_runner",
    "claude_runner.main",
    "claude_runner.runner",
    "claude_runner.config",
    "claude_runner.project",
    "claude_runner.notify",
    "claude_runner.persistence",
    "claude_runner.process",
    "claude_runner.rate_limit",
    "claude_runner.tui",
    "claude_runner.context_manager",
    "claude_runner.sandbox",
    "claude_runner.sandbox.docker_sandbox",
    "claude_runner.sandbox.native_sandbox",
    # Third-party deps that are sometimes missed
    "click",
    "yaml",
    "pydantic",
    "pydantic.v1",
    "rich",
    "rich.console",
    "rich.table",
    "rich.progress",
    "rich.logging",
    "apprise",
    "keyring",
    "keyring.backends",
    "winpty",
    "docker",
]

# Data files to bundle (src_glob;dest_dir  --  Windows semicolon separator)
DATA_FILES_WIN = [
    ("docker/Dockerfile", "docker/"),
    ("projects/*.yaml", "projects/"),
]

# apprise loads notification plugins by scanning its own package directory at
# runtime, which fails inside a PyInstaller onefile exe (filesystem not present).
# We collect the apprise package path here and pass it via --add-data so the
# extracted temp dir contains a real apprise/plugins/ tree.
import importlib.util as _ilu
_apprise_spec = _ilu.find_spec("apprise")
APPRISE_PKG_DIR = str(Path(_apprise_spec.origin).parent) if _apprise_spec else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_platform() -> None:
    if platform.system() != "Windows":
        print(
            "WARNING: This script targets Windows. "
            "The resulting .exe will only run on Windows.\n"
            "         Building on non-Windows is possible but untested.",
            file=sys.stderr,
        )


def _require_pyinstaller() -> str:
    """Return the path to the pyinstaller executable, installing it if needed."""
    exe = shutil.which("pyinstaller")
    if exe:
        return exe

    print("pyinstaller not found in PATH. Attempting to install via pip...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "pyinstaller>=6.0"],
        stdout=subprocess.DEVNULL,
    )
    exe = shutil.which("pyinstaller")
    if not exe:
        # Fall back to running as a module
        return f"{sys.executable} -m PyInstaller"
    return exe


def _clean_artifacts() -> None:
    """Remove previous build/dist directories and any stale .spec files."""
    for path in (DIST_DIR, BUILD_DIR):
        if path.exists():
            print(f"  Removing {path} ...")
            shutil.rmtree(path)
    # Remove auto-generated spec only; leave the committed one untouched
    auto_spec = PROJECT_ROOT / f"{EXE_NAME}.spec"
    if auto_spec.exists() and auto_spec != SPEC_FILE:
        auto_spec.unlink()
    print("  Clean complete.")


def _resolve_data_args() -> list[str]:
    """
    Expand glob patterns in DATA_FILES_WIN and return a flat list of
    --add-data arguments suitable for subprocess.
    """
    sep = ";" if platform.system() == "Windows" else ":"
    args: list[str] = []
    for src_pattern, dest_dir in DATA_FILES_WIN:
        args += ["--add-data", f"{src_pattern}{sep}{dest_dir}"]
    # Bundle the entire apprise package so its plugin scanner finds a real filesystem tree.
    if APPRISE_PKG_DIR and Path(APPRISE_PKG_DIR).is_dir():
        args += ["--add-data", f"{APPRISE_PKG_DIR}{sep}apprise"]
    return args


def _build(debug: bool) -> int:
    pyinstaller = _require_pyinstaller()

    cmd: list[str] = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        f"--name={EXE_NAME}",
        "--noconfirm",
    ]

    if debug:
        cmd += ["--log-level", "DEBUG"]
    else:
        cmd += ["--log-level", "WARN"]

    # Hidden imports
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]

    # Data files
    cmd += _resolve_data_args()

    # Icon (optional -- skip if not present rather than failing the build)
    icon_path = PROJECT_ROOT / "assets" / "claude-runner.ico"
    if icon_path.exists():
        cmd += ["--icon", str(icon_path)]
    else:
        print(
            f"  NOTE: Icon not found at {icon_path}. "
            "Building without a custom icon."
        )

    # Entry point
    cmd.append(str(ENTRY_POINT))

    print("\nRunning PyInstaller:")
    print("  " + " ".join(cmd))
    print()

    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    return result.returncode


def _print_success() -> None:
    exe_path = DIST_DIR / f"{EXE_NAME}.exe"
    print()
    print("=" * 60)
    print("Build succeeded.")
    print(f"  Executable : {exe_path}")
    print()
    print("Usage examples:")
    print(f"  {exe_path} --help")
    print(f"  {exe_path} configure")
    print(f"  {exe_path} run my-project")
    print(f"  {exe_path} status")
    print("=" * 60)
    print()
    print("To distribute, copy dist\\claude-runner.exe to any Windows machine.")
    print("No Python installation is required on the target machine.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the claude-runner standalone Windows executable.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        default=False,
        help="Remove build/ and dist/ before building (default: False).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable PyInstaller DEBUG log level and skip --strip.",
    )
    args = parser.parse_args()

    _check_platform()

    print(f"claude-runner build script  (version {VERSION})")
    print(f"  Project root : {PROJECT_ROOT}")
    print(f"  Entry point  : {ENTRY_POINT}")
    print()

    if not ENTRY_POINT.exists():
        print(
            f"ERROR: Entry point not found: {ENTRY_POINT}\n"
            "       Make sure you are running this script from the repository root.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.clean:
        print("Cleaning previous artifacts...")
        _clean_artifacts()

    print("Starting PyInstaller build...")
    rc = _build(debug=args.debug)

    if rc != 0:
        print(f"\nERROR: PyInstaller exited with code {rc}.", file=sys.stderr)
        print(
            "Tip: Re-run with --debug for a detailed log:\n"
            "       python build_exe.py --debug",
            file=sys.stderr,
        )
        sys.exit(rc)

    _print_success()


if __name__ == "__main__":
    main()
