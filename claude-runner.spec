# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:\\ClaudeCodeProject\\ClaudeRunner\\claude-runner\\claude_runner\\main.py'],
    pathex=[],
    binaries=[],
    datas=[('docker/Dockerfile', 'docker/'), ('C:\\Users\\20894\\AppData\\Local\\Programs\\Python\\Python311\\Lib\\site-packages\\apprise', 'apprise')],
    hiddenimports=['claude_runner', 'claude_runner.main', 'claude_runner.runner', 'claude_runner.config', 'claude_runner.project', 'claude_runner.notify', 'claude_runner.persistence', 'claude_runner.process', 'claude_runner.rate_limit', 'claude_runner.tui', 'claude_runner.context_manager', 'claude_runner.sandbox', 'claude_runner.sandbox.docker_sandbox', 'claude_runner.sandbox.native_sandbox', 'click', 'yaml', 'pydantic', 'pydantic.v1', 'rich', 'rich.console', 'rich.table', 'rich.progress', 'rich.logging', 'apprise', 'keyring', 'keyring.backends', 'winpty', 'docker'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='claude-runner',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
