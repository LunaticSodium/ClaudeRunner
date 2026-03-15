# -*- mode: python ; coding: utf-8 -*-
# claude-runner.spec
# PyInstaller spec file for claude-runner.
#
# Usage:
#   pyinstaller claude-runner.spec
#
# This spec file is an alternative to build_exe.py for users who want
# fine-grained control over the build (e.g., adding extra binaries,
# tweaking UPX compression, or embedding a manifest).
#
# Generated for PyInstaller >= 6.0.

import sys
from pathlib import Path

block_cipher = None

ROOT = Path(SPECPATH)  # directory containing this .spec file

# ---------------------------------------------------------------------------
# Hidden imports -- submodules that PyInstaller cannot find via static analysis
# ---------------------------------------------------------------------------
hidden_imports = [
    "claude_runner",
    "claude_runner.main",
    "claude_runner.cli",
    "claude_runner.runner",
    "claude_runner.config",
    "claude_runner.project",
    "claude_runner.sandbox",
    "claude_runner.sandbox.docker_sandbox",
    "claude_runner.sandbox.native_sandbox",
    "claude_runner.rate_limit",
    "claude_runner.notifications",
    "claude_runner.notifications.desktop",
    "claude_runner.notifications.email",
    "claude_runner.notifications.webhook",
    "claude_runner.context",
    "claude_runner.status",
    "claude_runner.logs",
    # Third-party
    "click",
    "click.testing",
    "yaml",
    "pydantic",
    "pydantic.v1",
    "rich",
    "rich.console",
    "rich.table",
    "rich.progress",
    "rich.logging",
    "httpx",
    "httpx._transports.default",
    "plyer",
    "plyer.platforms.win.notification",
]

# ---------------------------------------------------------------------------
# Data files
# Tuples are (source, dest_directory_inside_bundle).
# Source paths are relative to SPECPATH (the repo root).
# ---------------------------------------------------------------------------
import glob as _glob

datas = []

# Dockerfile
dockerfile = ROOT / "docker" / "Dockerfile"
if dockerfile.exists():
    datas.append((str(dockerfile), "docker"))

# Project YAML templates
for yaml_file in sorted((ROOT / "projects").glob("*.yaml")):
    datas.append((str(yaml_file), "projects"))

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    [str(ROOT / "claude_runner" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude large stdlib modules that claude-runner does not use
        "tkinter",
        "unittest",
        "xmlrpc",
        "distutils",
        "email.mime.audio",
        "email.mime.image",
        "email.mime.multipart",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ---------------------------------------------------------------------------
# PYZ archive (bytecode)
# ---------------------------------------------------------------------------
pyz = PYZ(
    a.pure,
    a.zipped_data,
    cipher=block_cipher,
)

# ---------------------------------------------------------------------------
# EXE
# ---------------------------------------------------------------------------
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="claude-runner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,          # Set to True on release builds to reduce size
    upx=True,             # Set to False if UPX is not installed
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,         # CLI tool -- keep the console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Uncomment and point to a .ico file to embed an icon:
    # icon=str(ROOT / "assets" / "claude-runner.ico"),
    version_info={
        "version": (0, 1, 0, 0),
        "CompanyName": "",
        "FileDescription": "claude-runner -- autonomous Claude Code orchestrator",
        "InternalName": "claude-runner",
        "LegalCopyright": "MIT",
        "OriginalFilename": "claude-runner.exe",
        "ProductName": "claude-runner",
        "ProductVersion": "0.1.0",
    } if sys.platform == "win32" else None,
)
