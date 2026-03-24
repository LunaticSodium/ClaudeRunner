# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Local_Projects\\BTO_Modulators_Simulatior_runner\\ClaudeRunner\\claude_runner\\main.py'],
    pathex=[],
    binaries=[],
    datas=[('docker/Dockerfile', 'docker/'), ('projects/*.yaml', 'projects/'), ('claude_runner/presets/*', 'claude_runner/presets/'), ('C:\\Apps\\Anaconda3\\Lib\\site-packages\\apprise', 'apprise')],
    hiddenimports=['claude_runner', 'claude_runner.main', 'claude_runner.runner', 'claude_runner.config', 'claude_runner.project', 'claude_runner.notify', 'claude_runner.persistence', 'claude_runner.process', 'claude_runner.rate_limit', 'claude_runner.tui', 'claude_runner.context_manager', 'claude_runner.sandbox', 'claude_runner.sandbox.docker_sandbox', 'claude_runner.sandbox.native_sandbox', 'claude_runner.cccs_parser', 'claude_runner.supervisor_protocol', 'claude_runner.worker_supervisor', 'claude_runner.thinking_manual', 'claude_runner.kpi_collector', 'claude_runner.supervisor_audit', 'claude_runner.ntfy_client', 'claude_runner.inbox', 'claude_runner.git_inbox', 'claude_runner.preflight', 'claude_runner.model_watchdog', 'claude_runner.model_resolver', 'claude_runner.constraint_checker', 'claude_runner.acceptance_runner', 'claude_runner.pipeline', 'claude_runner.autostart', 'claude_runner.daemon', 'click', 'yaml', 'pydantic', 'pydantic.v1', 'rich', 'rich.console', 'rich.table', 'rich.progress', 'rich.logging', 'apprise', 'keyring', 'keyring.backends', 'winpty', 'docker'],
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
