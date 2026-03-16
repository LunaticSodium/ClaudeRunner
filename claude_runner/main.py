"""
main.py — Click-based CLI entry point for claude-runner.

Commands
--------
    claude-runner run <project_book.yaml> [--tui/--no-tui] [--dry-run]
    claude-runner queue <queue.yaml>
    claude-runner status [--task <name>]
    claude-runner abort [--task <name>]
    claude-runner logs [--task <name>] [--tail N]
    claude-runner validate <project_book.yaml>
    claude-runner configure
    claude-runner docker update
    claude-runner docker status
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import pathlib
import platform
import shutil
import smtplib
import socket
import sys
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ──────────────────────────────────────────────────────────────────────────────
# Lazy imports — deferred so the CLI stays snappy even if optional deps are
# missing.  Required modules are imported at the top of each command function
# so the error surfaces with a helpful message, not an import-time traceback.
# ──────────────────────────────────────────────────────────────────────────────

_console = Console()
_err_console = Console(stderr=True)

logger = logging.getLogger("claude_runner.main")

# Default configuration paths
_DEFAULT_CONFIG_DIR = pathlib.Path.home() / ".claude-runner"
_DEFAULT_SECRETS_FILE = _DEFAULT_CONFIG_DIR / "secrets.yaml"
_DEFAULT_STATE_DIR = _DEFAULT_CONFIG_DIR / "state"
_DEFAULT_LOG_DIR = _DEFAULT_CONFIG_DIR / "logs"
_DEFAULT_TRASH_DIR = _DEFAULT_CONFIG_DIR / "trash"
_DEFAULT_PROJECTS_DIR = _DEFAULT_CONFIG_DIR / "projects"
_INITIALIZED_MARKER = _DEFAULT_CONFIG_DIR / ".initialized"


# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────

def _check_docker_quick() -> bool:
    """Check Docker availability without the full SDK (instant, no timeout)."""
    if sys.platform == "win32":
        try:
            import ctypes
            GENERIC_READ = 0x80000000
            OPEN_EXISTING = 3
            INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
            h = ctypes.windll.kernel32.CreateFileW(
                r"\\.\pipe\docker_engine", GENERIC_READ, 0, None, OPEN_EXISTING, 0, None
            )
            if h != INVALID_HANDLE_VALUE:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return False
    return pathlib.Path("/var/run/docker.sock").exists()


def _find_example_template() -> Optional[pathlib.Path]:
    """Return the path to the bundled examples.yaml template."""
    if getattr(sys, "frozen", False):
        p = pathlib.Path(getattr(sys, "_MEIPASS", "")) / "examples.yaml"
    else:
        p = pathlib.Path(__file__).parent.parent / "projects" / "examples.yaml"
    return p if p.exists() else None


def _copy_example_with_header(src: pathlib.Path, dest: pathlib.Path) -> None:
    """Copy the first YAML document from *src* to *dest*, prepending a usage header.

    If *src* is a multi-document file (sections separated by ``---``), only the
    first document is written so the result is a valid single-document project book.
    """
    header = (
        "# claude-runner example project book\n"
        "# Copy this file, rename it, and edit the 'prompt' field to\n"
        "# describe your task. Then run:\n"
        "#   claude-runner run your-project.yaml\n"
        "#\n"
    )
    content = src.read_text(encoding="utf-8")
    # Split on a '---' document separator that starts at the beginning of a line.
    # Take only the first section so the output is a valid single-document file.
    import re as _re  # noqa: PLC0415
    parts = _re.split(r"(?m)^---[ \t]*$", content)
    first_doc = parts[0].strip()
    dest.write_text(header + first_doc + "\n", encoding="utf-8")


def _project_search_dirs() -> list[pathlib.Path]:
    """Return ordered directories to search for bare project book filenames."""
    dirs: list[pathlib.Path] = [pathlib.Path.cwd()]
    exe_dir = pathlib.Path(sys.argv[0]).parent
    if exe_dir != pathlib.Path.cwd():
        dirs.append(exe_dir)
    dirs.append(_DEFAULT_PROJECTS_DIR)
    return dirs


def _resolve_project_book_path(name: str) -> str:
    """Resolve a bare project book filename by searching known locations.

    If *name* contains a path separator or is absolute it is returned unchanged.
    Otherwise searches: CWD → exe directory → ~/.claude-runner/projects/.
    """
    p = pathlib.Path(name)
    if p.is_absolute() or len(p.parts) > 1:
        return name
    for search_dir in _project_search_dirs():
        candidate = search_dir / p
        if candidate.exists():
            return str(candidate)
    return name  # unchanged; _load_project_book will surface a clear error


def _ensure_initialized() -> None:
    """Idempotent setup: create config dirs, run preflight checks, handle first run.

    Called at the start of every CLI command. Never aborts — only warns.
    """
    # ── Create directories ─────────────────────────────────────────────────────
    for d in (
        _DEFAULT_CONFIG_DIR,
        _DEFAULT_LOG_DIR,
        _DEFAULT_STATE_DIR,
        _DEFAULT_TRASH_DIR,
        _DEFAULT_PROJECTS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)

    # ── First-run detection ────────────────────────────────────────────────────
    first_run = not _INITIALIZED_MARKER.exists()

    # ── Preflight checks (warn only, never abort) ──────────────────────────────

    # 1. Docker Desktop
    if not _check_docker_quick():
        _err_console.print(
            "[bold yellow][WARN][/bold yellow]  Docker Desktop is not running. "
            "Required for [cyan]sandbox: {backend: docker}[/cyan] tasks. "
            "Start it or use [cyan]sandbox: {backend: native}[/cyan]."
        )

    # 2. Claude Code CLI
    if shutil.which("claude") is None:
        _err_console.print(
            "[bold yellow][WARN][/bold yellow]  Claude Code not found on PATH. "
            "Run: [cyan]npm install -g @anthropic-ai/claude-code[/cyan]"
        )

    # 3. API key (env var, secrets file, keyring, or OAuth session)
    api_key_present = bool(_resolve_api_key())
    if not api_key_present:
        _err_console.print(
            "[bold yellow][WARN][/bold yellow]  No API key configured. "
            "Run: [cyan]claude-runner configure[/cyan]"
        )

    # ── First-run actions ──────────────────────────────────────────────────────
    if not first_run:
        return

    # Copy example project book next to the exe (frozen builds only)
    example_dest: Optional[pathlib.Path] = None
    if getattr(sys, "frozen", False):
        exe_dir = pathlib.Path(sys.argv[0]).parent
        if not list(exe_dir.glob("*.yaml")):
            template = _find_example_template()
            if template is not None:
                example_dest = exe_dir / "claude-runner-example.yaml"
                try:
                    _copy_example_with_header(template, example_dest)
                except Exception as exc:
                    logger.debug("Could not copy example template: %s", exc)
                    example_dest = None

    if example_dest is not None:
        _console.print(
            "\n[bold green]First run detected.[/bold green] "
            "An example project book has been copied to:\n"
            f"  [cyan]{example_dest}[/cyan]\n"
            "Edit it to describe your first task, then run:\n"
            "  [cyan]claude-runner run claude-runner-example.yaml[/cyan]\n"
        )
    else:
        _console.print(
            "\n[bold green]First run detected.[/bold green] "
            "Run [cyan]claude-runner configure[/cyan] to set up "
            "notifications and verify your environment.\n"
        )

    _INITIALIZED_MARKER.touch()


def _abort(message: str, exit_code: int = 1) -> None:
    """Print a styled error and exit."""
    _err_console.print(f"[bold red][ERROR][/bold red] {message}")
    sys.exit(exit_code)


def _info(message: str) -> None:
    _console.print(f"[bold cyan][INFO][/bold cyan]  {message}")


def _ok(message: str) -> None:
    _console.print(f"[bold green][OK][/bold green]    {message}")


def _warn(message: str) -> None:
    _console.print(f"[bold yellow][WARN][/bold yellow]  {message}")


_OAUTH_SENTINEL = "__claude_oauth__"


def _resolve_api_key() -> Optional[str]:
    """
    Resolve ANTHROPIC_API_KEY through the 5-source priority chain.

    Returns the literal string ``_OAUTH_SENTINEL`` when a valid Claude Code
    OAuth session is detected (Priority 5), so callers can distinguish between
    "key found" and "OAuth active" without raising.

    Priority 1: ANTHROPIC_API_KEY in current shell environment.
    Priority 2: ANTHROPIC_API_KEY in system-wide Windows registry.
    Priority 3: api_key field in ~/.claude-runner/secrets.yaml.
    Priority 4: Windows Credential Manager (keyring).
    Priority 5: Claude Code OAuth session (~/.claude/.credentials.json).
    """
    # Priority 1 — current environment
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        logger.debug("API key resolved from current environment.")
        return key

    # Priority 2 — system-wide environment (Windows registry)
    if platform.system() == "Windows":
        try:
            import winreg  # type: ignore[import]
            for hive, sub in [
                (winreg.HKEY_CURRENT_USER, r"Environment"),
                (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            ]:
                try:
                    with winreg.OpenKey(hive, sub) as reg_key:
                        value, _ = winreg.QueryValueEx(reg_key, "ANTHROPIC_API_KEY")
                        if value.strip():
                            logger.debug("API key resolved from Windows registry.")
                            return value.strip()
                except FileNotFoundError:
                    pass
        except Exception:
            pass

    # Priority 3 — secrets.yaml
    if _DEFAULT_SECRETS_FILE.exists():
        try:
            import yaml  # type: ignore[import]
            with _DEFAULT_SECRETS_FILE.open("r", encoding="utf-8") as fh:
                secrets = yaml.safe_load(fh) or {}
            key = (secrets.get("api_key") or "").strip()
            if key:
                logger.debug("API key resolved from secrets.yaml.")
                return key
        except Exception as exc:
            logger.warning("Failed to read secrets.yaml: %s", exc)

    # Priority 4 — keyring / Windows Credential Manager
    try:
        import keyring  # type: ignore[import]
        key = (keyring.get_password("claude-runner/anthropic", "api_key") or "").strip()
        if key:
            logger.debug("API key resolved from keyring.")
            return key
    except Exception:
        pass

    # Priority 5 — Claude Code OAuth session
    from claude_runner.config import _detect_oauth_session  # noqa: PLC0415
    if _detect_oauth_session():
        logger.debug("API key resolved via Claude Code OAuth session.")
        return _OAUTH_SENTINEL

    return None


def _load_project_book(path: str):
    """Load and validate a project book, aborting with a clear error on failure."""
    try:
        from claude_runner.project import ProjectBook  # type: ignore[import]
        return ProjectBook.from_yaml(path)
    except ImportError:
        _abort(
            "claude_runner.project module not found. "
            "Ensure claude-runner is installed correctly: pip install -e ."
        )
    except Exception as exc:
        _abort(f"Failed to load project book '{path}': {exc}")


def _load_global_config():
    """Load global config, returning defaults if the file does not exist."""
    try:
        from claude_runner.config import GlobalConfig  # type: ignore[import]
        return GlobalConfig.load()
    except ImportError:
        _abort(
            "claude_runner.config module not found. "
            "Ensure claude-runner is installed correctly: pip install -e ."
        )
    except Exception as exc:
        _abort(f"Failed to load global config: {exc}")


def _find_state_file(task_name: Optional[str]) -> Optional[pathlib.Path]:
    """Locate a state file for the given task name (or the most recent one)."""
    if task_name:
        candidate = _DEFAULT_STATE_DIR / f"{task_name}.json"
        return candidate if candidate.exists() else None

    files = sorted(
        _DEFAULT_STATE_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


# ──────────────────────────────────────────────────────────────────────────────
# Root command group
# ──────────────────────────────────────────────────────────────────────────────

@click.group()
@click.version_option(version="0.3.0", prog_name="claude-runner")
def cli() -> None:
    """
    claude-runner — autonomous Claude Code execution framework.

    Write a project book (YAML), then run:

        claude-runner run projects/my_task.yaml

    \b
    Key features:
      - Automatic rate-limit detection and resume (no human needed)
      - Docker sandbox with network allowlisting
      - Desktop, email, and webhook notifications on task events
      - Context checkpoint injection to keep long tasks coherent
      - context_anchors: persistent per-task instructions prepended to every
        prompt (initial, resume, and checkpoint).  Set once in the project
        book; claude-runner injects silently.  When active, the TUI shows:
        [context anchors: active]

    See 'claude-runner COMMAND --help' for details on each command.
    Run 'claude-runner configure' to set up your API key and notifications.
    """
    # Default to DEBUG until a successful run has been recorded.
    _default_log_level = (
        logging.WARNING
        if (_INITIALIZED_MARKER.parent / ".first_success").exists()
        else logging.DEBUG
    )
    logging.basicConfig(
        level=_default_log_level,
        format="%(levelname)-8s %(name)s: %(message)s",
    )


# ──────────────────────────────────────────────────────────────────────────────
# run
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("run")
@click.argument("project_book", type=click.Path())
@click.option("--tui/--no-tui", default=True, help="Show the Rich terminal UI (default: on).")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Validate and show resolved config without actually running.",
)
@click.option(
    "--verbose", "-v",
    is_flag=True,
    default=False,
    help="Enable verbose (DEBUG) logging.",
)
@click.option(
    "--show-claude",
    is_flag=True,
    default=False,
    help="Open Claude Code in a visible console window. Implies --verbose. For diagnosing process communication issues only.",
)
def run(project_book: str, tui: bool, dry_run: bool, verbose: bool, show_claude: bool) -> None:
    """Run a claude-runner project book.

    PROJECT_BOOK is the path to a .yaml project book file.  A bare filename
    (no path separator) is searched in: current directory → exe directory →
    ~/.claude-runner/projects/.

    Example:

        claude-runner run my_task.yaml --tui
    """
    if verbose or show_claude:
        logging.getLogger().setLevel(logging.DEBUG)

    _ensure_initialized()

    # Resolve bare filename to a full path before loading.
    project_book = _resolve_project_book_path(project_book)

    # ── Load project book ──────────────────────────────────────────────────────
    pb = _load_project_book(project_book)
    config = _load_global_config()

    # ── Dry run ────────────────────────────────────────────────────────────────
    if dry_run:
        _console.print(
            Panel(
                _render_project_book_summary(pb),
                title=f"[bold cyan]DRY RUN[/bold cyan] — {pb.name}",
                border_style="cyan",
            )
        )
        _ok("Project book is valid. No task was launched (--dry-run).")
        return

    # ── Resolve API key ────────────────────────────────────────────────────────
    api_key = _resolve_api_key()
    if not api_key:
        _abort(
            "ANTHROPIC_API_KEY not found in environment, registry, secrets.yaml, "
            "or keyring.\n\nRun:  claude-runner configure\nto set up your API key."
        )

    # ── Check for existing state (offer resume or fresh start) ────────────────
    state_file = _find_state_file(pb.name)
    resume_session = False

    if state_file:
        try:
            with state_file.open("r", encoding="utf-8") as fh:
                saved_state = json.load(fh)
            prev_phase = saved_state.get("phase", "unknown")
            prev_started = saved_state.get("start_time", "unknown")

            _console.print(
                Panel(
                    f"A previous session was found for task [bold]{pb.name}[/bold].\n"
                    f"  Phase: [yellow]{prev_phase}[/yellow]\n"
                    f"  Started: {prev_started}\n\n"
                    "Resume from where it left off, or start fresh?",
                    title="[bold yellow]Previous Session Found[/bold yellow]",
                    border_style="yellow",
                )
            )
            choice = click.prompt(
                "  [R]esume / [F]resh start / [A]bort",
                default="R",
            ).strip().upper()

            if choice == "A":
                _info("Aborted by user.")
                return
            elif choice == "F":
                _info("Starting fresh — previous state will be overwritten.")
                state_file.unlink(missing_ok=True)
                resume_session = False
            else:
                _info("Resuming previous session.")
                resume_session = True

        except Exception as exc:
            _warn(f"Could not read previous state file ({exc}). Starting fresh.")
            resume_session = False

    # ── Set up TUI ─────────────────────────────────────────────────────────────
    tui_manager = None
    if tui:
        try:
            from claude_runner.tui import TUIManager  # type: ignore[import]
            timeout_hours = getattr(pb.execution, "timeout_hours", 4.0)
            tui_manager = TUIManager(
                task_name=pb.name,
                project_book_path=project_book,
                timeout_hours=float(timeout_hours),
            )
            tui_manager.start()
        except ImportError:
            _warn("TUI unavailable (rich not installed). Continuing without TUI.")
            tui_manager = None

    # ── Create runner and execute ──────────────────────────────────────────────
    # Sandbox creation and setup are handled inside runner._initialise().
    try:
        from claude_runner.runner import ClaudeRunner  # type: ignore[import]
        runner = ClaudeRunner(
            project_book=pb,
            config=config,
            tui=tui_manager,
            api_key=api_key,
            resume=resume_session,
            project_book_path=project_book,
            show_claude=show_claude,
        )

        # ── Register cleanup handlers ──────────────────────────────────────
        # Ensure the child Claude Code process is terminated when the parent
        # exits for any reason (normal exit, exception, terminal window close).

        import atexit as _atexit  # noqa: PLC0415
        import signal as _signal  # noqa: PLC0415

        def _emergency_cleanup() -> None:
            """Best-effort child-process termination on unexpected exit."""
            # runner._process is assigned only after launch_claude() succeeds.
            # If startup failed mid-flight, the live handle is on the sandbox.
            proc = getattr(runner, "_process", None)
            if proc is None:
                sandbox = getattr(runner, "_sandbox", None)
                proc = getattr(sandbox, "_process", None)
            if proc is None:
                return
            try:
                if hasattr(proc, "stop"):
                    proc.stop(timeout=3.0)
                elif hasattr(proc, "terminate"):
                    proc.terminate()
            except Exception:
                pass

        _atexit.register(_emergency_cleanup)

        if sys.platform == "win32":
            # SIGBREAK fires when the user closes the console window or presses
            # Ctrl+Break.  Register a handler so the child is not orphaned.
            def _sigbreak_handler(signum, frame) -> None:  # noqa: ANN001
                _emergency_cleanup()
                sys.exit(130)

            try:
                _signal.signal(_signal.SIGBREAK, _sigbreak_handler)
            except (OSError, ValueError):
                pass  # SIGBREAK may be unavailable in some hosted environments.

        result = asyncio.run(runner.run())

    except KeyboardInterrupt:
        _warn("Interrupted by user (Ctrl+C). Task will be left in a resumable state.")
        sys.exit(130)
    except Exception as exc:
        if tui_manager:
            tui_manager.stop()
        _abort(f"Runner error: {exc}")
    finally:
        if tui_manager:
            tui_manager.stop()

    if result.status == "complete":
        _ok(f"Task '{pb.name}' completed successfully.")
        # First success: write marker so subsequent runs use quiet logging.
        _first_success = _INITIALIZED_MARKER.parent / ".first_success"
        if not _first_success.exists():
            try:
                _first_success.touch()
            except OSError:
                pass
    else:
        msg = result.error_message or result.status
        _err_console.print(
            f"[bold red][FAIL][/bold red] Task '{pb.name}' failed: {msg}"
        )
        sys.exit(1)


def _render_project_book_summary(pb) -> Table:
    """Render a summary of a parsed project book for --dry-run output."""
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", width=22)
    t.add_column(style="white")

    t.add_row("Name", str(getattr(pb, "name", "—")))
    t.add_row("Description", str(getattr(pb, "description", "—") or "—").strip()[:80])

    anchors = getattr(pb, "context_anchors", None)
    if anchors:
        t.add_row("Context anchors", "[cyan]active[/cyan]")

    prompt = str(getattr(pb, "prompt", "") or "")
    preview = prompt.strip().replace("\n", " ")[:80]
    if len(prompt.strip()) > 80:
        preview += "…"
    t.add_row("Prompt (preview)", preview)

    # Sandbox
    sandbox_cfg = getattr(pb, "sandbox", None)
    if sandbox_cfg:
        t.add_row("Working dir", str(getattr(sandbox_cfg, "working_dir", "—")))

    # Execution
    exec_cfg = getattr(pb, "execution", None)
    if exec_cfg:
        t.add_row("Timeout", f"{getattr(exec_cfg, 'timeout_hours', '—')}h")
        t.add_row("Max RL waits", str(getattr(exec_cfg, "max_rate_limit_waits", "—")))
        t.add_row("Resume strategy", str(getattr(exec_cfg, "resume_strategy", "—")))

    # Notify
    notify_cfg = getattr(pb, "notify", None)
    if notify_cfg:
        events = getattr(notify_cfg, "on", [])
        t.add_row("Notify on", ", ".join(events) if events else "—")
        channels = getattr(notify_cfg, "channels", [])
        ch_types = [str(getattr(c, "type", c)) for c in channels]
        t.add_row("Channels", ", ".join(ch_types) if ch_types else "—")

    return t


# ──────────────────────────────────────────────────────────────────────────────
# queue
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("queue")
@click.argument("queue_file", type=click.Path(exists=True))
@click.option("--tui/--no-tui", default=True, help="Show TUI for each task.")
def queue(queue_file: str, tui: bool) -> None:
    """Run a queue of project books defined in QUEUE_FILE.

    QUEUE_FILE is a YAML file that lists project books and optional dependencies.
    Tasks are executed sequentially by default, unless concurrency is configured.

    Example queue.yaml:

    \b
        tasks:
          - project_book: projects/task1.yaml
          - project_book: projects/task2.yaml
            depends_on: [task1]
        options:
          fail_fast: true
    """
    try:
        import yaml  # type: ignore[import]
    except ImportError:
        _abort("pyyaml is required. Install with: pip install pyyaml")

    _ensure_initialized()

    with open(queue_file, "r", encoding="utf-8") as fh:
        queue_cfg = yaml.safe_load(fh)

    tasks = queue_cfg.get("tasks", [])
    if not tasks:
        _abort(f"No tasks found in queue file '{queue_file}'.")

    fail_fast: bool = queue_cfg.get("options", {}).get("fail_fast", True)

    _info(f"Queue: {len(tasks)} task(s) to run.")
    failed: list[str] = []

    for i, task_entry in enumerate(tasks, start=1):
        pb_path = task_entry.get("project_book")
        if not pb_path:
            _warn(f"Queue entry {i} has no 'project_book' field — skipping.")
            continue

        pb_path = str(pathlib.Path(pb_path).expanduser())
        if not pathlib.Path(pb_path).exists():
            _warn(f"Project book not found: '{pb_path}' — skipping.")
            failed.append(pb_path)
            if fail_fast:
                break
            continue

        _info(f"[{i}/{len(tasks)}] Running: {pb_path}")

        # Delegate to the run command programmatically
        ctx = click.Context(run)
        try:
            ctx.invoke(run, project_book=pb_path, tui=tui, dry_run=False, verbose=False)
        except SystemExit as exc:
            if exc.code != 0:
                failed.append(pb_path)
                _warn(f"Task failed: {pb_path}")
                if fail_fast:
                    _abort(f"Stopping queue after failure (fail_fast=true).")
                    break

    if failed:
        _err_console.print(
            f"[bold red][FAIL][/bold red] {len(failed)} task(s) failed: "
            + ", ".join(failed)
        )
        sys.exit(1)
    else:
        _ok(f"All {len(tasks)} task(s) completed successfully.")


# ──────────────────────────────────────────────────────────────────────────────
# validate
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("new")
@click.argument("name")
@click.option("--no-git", is_flag=True, default=False, help="Skip 'git init' in the project folder.")
def new(name: str, no_git: bool) -> None:
    """Scaffold a new project: create <name>.yaml and the <name>/ working folder.

    The YAML and folder are created in the current directory (or next to the
    exe when double-clicked).  Running 'claude-runner run <name>.yaml' will
    automatically use <name>/ as the working directory.

    Example:

        claude-runner new my-coding-task
    """
    _ensure_initialized()

    # Resolve the target directory (next to exe when frozen, cwd otherwise).
    if getattr(sys, "frozen", False):
        target_dir = pathlib.Path(sys.argv[0]).parent
    else:
        target_dir = pathlib.Path.cwd()

    yaml_path = target_dir / f"{name}.yaml"
    work_dir = target_dir / name

    if yaml_path.exists():
        _abort(f"'{yaml_path}' already exists. Choose a different name or edit it directly.")
    if work_dir.exists() and list(work_dir.iterdir()):
        _warn(f"Folder '{work_dir}' already exists and is not empty — leaving it as-is.")
    else:
        work_dir.mkdir(parents=True, exist_ok=True)
        _ok(f"Created folder: {work_dir}")

    # Write project YAML template.
    yaml_content = f"""\
# {name} — claude-runner project book
#
# Working directory: ./{name}/  (auto-derived from this filename)
# Run with: claude-runner run {name}.yaml

name: {name}
description: >
  Describe your project here.

prompt: |
  Describe your task here. Be specific about:
  - What you want Claude to build or change
  - The files or directories involved
  - Any constraints or preferences

  When done, commit your changes with a descriptive message.

sandbox:
  backend: native   # change to 'docker' for stronger isolation

execution:
  timeout_hours: 4
  max_rate_limit_waits: 5
  resume_strategy: continue
  skip_permissions: true

notify:
  on: [complete, error]
  channels:
    - type: desktop
"""
    yaml_path.write_text(yaml_content, encoding="utf-8")
    _ok(f"Created project book: {yaml_path}")

    # Create .claude/ directory with settings and CLAUDE.md template.
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    settings_content = """\
{
  "permissions": {
    "allow": [
      "Bash(*)",
      "Read(*)",
      "Write(*)",
      "Edit(*)",
      "MultiEdit(*)",
      "Glob(*)",
      "Grep(*)",
      "LS(*)",
      "TodoRead(*)",
      "TodoWrite(*)"
    ],
    "deny": []
  }
}
"""
    (claude_dir / "settings.json").write_text(settings_content, encoding="utf-8")
    _ok(f"Created: {claude_dir / 'settings.json'}")

    claude_md_content = f"""\
# Project: {name}
# Created by claude-runner

## Context
(Describe your project here — this file is read by Claude Code at session start)

## Constraints
(Any rules Claude should follow in this project)

## Execution guidelines
For any task involving loops, retries, polling, or repeated operations:
- Write a Python script to handle the logic
- Run the script rather than relying on conversational repetition
- The script becomes part of the project and can be reused

This makes your work deterministic, auditable, and testable. For multi-step or iterative work, prefer writing a control script
over performing steps manually turn by turn. Scripts are more
reliable, can be tested via acceptance_criteria, and survive
context resets.
"""
    (claude_dir / "CLAUDE.md").write_text(claude_md_content, encoding="utf-8")
    _ok(f"Created: {claude_dir / 'CLAUDE.md'}")

    # Initialise git repo.
    if not no_git:
        git_dir = work_dir / ".git"
        if git_dir.exists():
            _info(f"Git repo already exists in {work_dir} — skipping git init.")
        else:
            try:
                import subprocess as _sp  # noqa: PLC0415
                _sp.run(["git", "init", str(work_dir)], check=True, capture_output=True)
                # Basic .gitignore
                (work_dir / ".gitignore").write_text(
                    "# claude-runner internals\n.claude-runner/\n", encoding="utf-8"
                )
                # Initial commit with .gitignore and .claude/ scaffold.
                _sp.run(
                    ["git", "add", ".gitignore", ".claude/settings.json", ".claude/CLAUDE.md"],
                    cwd=str(work_dir), check=True, capture_output=True,
                )
                _sp.run(
                    ["git", "commit", "-m", f"Initial scaffold for {name}"],
                    cwd=str(work_dir), check=True, capture_output=True,
                )
                _ok(f"Initialised git repo in: {work_dir}")
            except Exception as exc:
                _warn(f"git init failed ({exc}). You can run it manually: git init {name}/")

    _console.print(
        f"\n[bold green]Project '{name}' ready.[/bold green]\n"
        f"  Edit [cyan]{yaml_path.name}[/cyan] to describe your task, then run:\n"
        f"  [cyan]claude-runner run {name}.yaml[/cyan]\n"
    )


@cli.command("validate")
@click.argument("project_book", type=click.Path(exists=True))
def validate(project_book: str) -> None:
    """Validate a project book without running it.

    Checks schema, required fields, and warns about potentially dangerous
    configurations (e.g. skip_permissions without Docker).

    PROJECT_BOOK is the path to a .yaml project book file.
    """
    _ensure_initialized()
    pb = _load_project_book(project_book)

    _console.print(
        Panel(
            _render_project_book_summary(pb),
            title=f"[bold green]VALID[/bold green] — {pb.name}",
            border_style="green",
        )
    )

    # Warn on dangerous combinations
    exec_cfg = getattr(pb, "execution", None)
    sandbox_cfg = getattr(pb, "sandbox", None)
    skip_perms = getattr(exec_cfg, "skip_permissions", False) if exec_cfg else False
    backend = getattr(sandbox_cfg, "backend", "docker") if sandbox_cfg else "docker"

    if skip_perms and backend == "native":
        _warn(
            "skip_permissions is enabled without a Docker sandbox. "
            "Claude Code will run with full host filesystem access. "
            "This is only safe if you trust the task completely."
        )

    _ok(f"'{project_book}' is a valid project book.")


# ──────────────────────────────────────────────────────────────────────────────
# status
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("status")
@click.option("--task", default=None, help="YAML filename stem to inspect, e.g. 'my-task' for my-task.yaml (defaults to most recent).")
def status(task: Optional[str]) -> None:
    """Check the status of a running or completed task.

    If --task is omitted, shows the most recently modified state file.
    """
    _ensure_initialized()
    state_file = _find_state_file(task)

    if not state_file:
        if task:
            _abort(f"No state file found for task '{task}'.")
        else:
            _abort(
                "No state files found in ~/.claude-runner/state/. "
                "Has a task been run yet?"
            )

    try:
        with state_file.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception as exc:
        _abort(f"Could not read state file: {exc}")

    t = Table(title=f"Task Status — {state_file.stem}", show_header=False, box=None)
    t.add_column(style="dim", width=24)
    t.add_column(style="white")

    for key, value in state.items():
        if key == "phase":
            phase_style = {
                "running": "bold green",
                "waiting": "bold yellow",
                "complete": "bold blue",
                "failed": "bold red",
            }.get(str(value).lower(), "white")
            t.add_row(key, Text(str(value), style=phase_style))
        else:
            t.add_row(key, str(value))

    _console.print(t)


# ──────────────────────────────────────────────────────────────────────────────
# abort
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("abort")
@click.option("--task", default=None, help="YAML filename stem to abort, e.g. 'my-task' for my-task.yaml (defaults to most recent).")
@click.option("--force", is_flag=True, default=False, help="Skip confirmation prompt.")
def abort(task: Optional[str], force: bool) -> None:
    """Abort a running task.

    This sends a termination signal to the Claude Code process and cleans up the
    Docker container (if any).  The state file is preserved so the task can be
    resumed later.
    """
    _ensure_initialized()
    state_file = _find_state_file(task)

    if not state_file:
        _abort(
            f"No state file found for task '{task or 'most recent'}'."
        )

    try:
        with state_file.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception as exc:
        _abort(f"Could not read state file: {exc}")

    task_name = state.get("task_name", state_file.stem)
    phase = state.get("phase", "unknown")

    if phase in ("complete", "failed"):
        _warn(f"Task '{task_name}' is already in state '{phase}'. Nothing to abort.")
        return

    if not force:
        click.confirm(
            f"Abort task '{task_name}' (currently {phase})?",
            default=False,
            abort=True,
        )

    # Signal the runner via the state file's PID if recorded
    pid = state.get("pid")
    if pid:
        import signal as _signal
        try:
            os.kill(int(pid), _signal.SIGTERM)
            _ok(f"Sent SIGTERM to PID {pid}.")
        except ProcessLookupError:
            _warn(f"Process PID {pid} not found (already exited?).")
        except Exception as exc:
            _warn(f"Could not send signal to PID {pid}: {exc}")
    else:
        _warn(
            "No PID recorded in state file. "
            "If the runner is still active, kill it manually."
        )

    # Mark state as aborted
    state["phase"] = "aborted"
    try:
        with state_file.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        _ok(f"State updated to 'aborted' for task '{task_name}'.")
    except Exception as exc:
        _warn(f"Could not update state file: {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# logs
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("logs")
@click.option("--task", default=None, help="YAML filename stem, e.g. 'my-task' for my-task.yaml (defaults to most recent).")
@click.option("--tail", default=50, show_default=True, help="Number of lines to show.")
@click.option("--raw", is_flag=True, default=False, help="Print raw log without formatting.")
def logs(task: Optional[str], tail: int, raw: bool) -> None:
    """View logs from a task's last run.

    Searches ~/.claude-runner/logs/ for a log file matching the task name.
    """
    _ensure_initialized()

    if task:
        candidates = sorted(
            _DEFAULT_LOG_DIR.glob(f"{task}*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    else:
        candidates = sorted(
            _DEFAULT_LOG_DIR.glob("*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    if not candidates:
        _abort(
            f"No log files found in {_DEFAULT_LOG_DIR}"
            + (f" for task '{task}'." if task else ".")
        )

    log_file = candidates[0]
    _info(f"Reading: {log_file}")

    try:
        with log_file.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except Exception as exc:
        _abort(f"Could not read log file: {exc}")

    tail_lines = lines[-tail:] if tail < len(lines) else lines

    if raw:
        for line in tail_lines:
            click.echo(line, nl=False)
    else:
        _console.print(
            Panel(
                "".join(tail_lines),
                title=f"[bold]Logs: {log_file.name}[/bold] (last {len(tail_lines)} lines)",
                border_style="bright_black",
            )
        )


# ──────────────────────────────────────────────────────────────────────────────
# configure
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("configure")
def configure() -> None:
    """Interactive configuration wizard.

    \b
    Guides you through:
      - Choosing your authentication method (API key or Claude.ai account)
      - Setting your ANTHROPIC_API_KEY  (API key users only)
      - Configuring email notifications (with Gmail App Password guide)
      - Testing SMTP connectivity
      - Saving credentials to secrets.yaml or Windows Credential Manager
    """
    _ensure_initialized()

    _console.print(
        Panel(
            "[bold cyan]claude-runner configuration wizard[/bold cyan]\n\n"
            "This wizard will help you configure authentication and optional\n"
            "notification channels. Press Ctrl+C at any time to quit.",
            border_style="cyan",
        )
    )

    secrets: dict = {}

    # ── Load existing secrets ──────────────────────────────────────────────────
    if _DEFAULT_SECRETS_FILE.exists():
        try:
            import yaml  # type: ignore[import]
            with _DEFAULT_SECRETS_FILE.open("r", encoding="utf-8") as fh:
                secrets = yaml.safe_load(fh) or {}
            _info("Existing secrets.yaml loaded.")
        except Exception as exc:
            _warn(f"Could not load existing secrets.yaml: {exc}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Step 1: Detect Claude Code installation and authentication
    # ═══════════════════════════════════════════════════════════════════════════
    _console.rule("[bold]Step 1 of 3: Claude Code Authentication[/bold]")

    import shutil as _shutil  # noqa: PLC0415
    claude_path = _shutil.which("claude")

    if claude_path is None:
        _warn(
            "Claude Code not found on PATH.\n"
            "  Install it first:\n\n"
            "    winget install OpenJS.NodeJS.LTS\n"
            "    npm install -g @anthropic-ai/claude-code\n"
            "    claude          ← completes the one-time login\n\n"
            "  Then re-run: claude-runner configure"
        )
        # Still allow the wizard to continue so email / save steps can run.
        using_oauth = False
        _need_api_key = True
    else:
        _ok(f"Claude Code found: {claude_path}")

        from claude_runner.config import _detect_oauth_session  # noqa: PLC0415
        if _detect_oauth_session():
            _ok(
                "OAuth session active (Claude.ai account detected in "
                "~/.claude/.credentials.json)\n"
                "  claude-runner will authenticate using your Claude.ai session — "
                "no API key needed."
            )
            using_oauth = True
            _need_api_key = False
        else:
            _info("No OAuth session found — checking for an API key…")
            using_oauth = False
            existing_key = _resolve_api_key()
            if existing_key and existing_key != _OAUTH_SENTINEL:
                masked = existing_key[:8] + "…" + existing_key[-4:]
                _ok(f"API key already configured: {masked}")
                if not click.confirm("Update it?", default=False):
                    _need_api_key = False
                else:
                    _need_api_key = True
            else:
                _need_api_key = True

    if _need_api_key and not using_oauth:
        _console.print(
            "\nNo authentication detected.  Provide an Anthropic API key, or\n"
            "log in with a Claude.ai Pro/Max account by running [cyan]claude[/cyan].\n\n"
            "Get an API key at: [link]https://console.anthropic.com/account/keys[/link]\n"
        )
        new_key = click.prompt(
            "Paste your ANTHROPIC_API_KEY",
            hide_input=True,
            confirmation_prompt=False,
        ).strip()

        if not new_key.startswith("sk-"):
            _warn("Key does not start with 'sk-'. Double-check it before saving.")

        secrets["api_key"] = new_key
        _ok("API key noted (will be saved at the end of this wizard).")

    # ═══════════════════════════════════════════════════════════════════════════
    # Step B: Notifications (optional)
    # ═══════════════════════════════════════════════════════════════════════════
    _console.rule("[bold]Step 2 of 3: Notifications (optional)[/bold]")
    _console.print(
        "claude-runner can notify you when a task completes or fails.\n\n"
        "  [bold]1[/bold]  ntfy.sh  [green](recommended — no account required)[/green]\n"
        "  [bold]2[/bold]  Email (SMTP)\n"
        "  [bold]3[/bold]  Skip for now\n"
    )
    notify_choice = click.prompt("Choice", default="1").strip()

    if notify_choice == "1":
        _run_ntfy_guide(secrets)
    elif notify_choice == "2":
        _console.print(
            "\n[dim]Note: password fields are invisible as you type — "
            "this is intentional. If your system auto-fills a password, "
            "clear the field and type it manually to avoid pasting the wrong value.[/dim]\n"
        )
        email_address = click.prompt("Your email address").strip()
        secrets["notify_email"] = email_address

        is_gmail = email_address.lower().endswith(
            ("@gmail.com", "@googlemail.com")
        )

        if is_gmail:
            _run_gmail_app_password_guide(email_address, secrets)
        else:
            _run_generic_smtp_guide(email_address, secrets)
    else:
        _info("Skipping notification configuration.")

    # ═══════════════════════════════════════════════════════════════════════════
    # Step C: Save credentials
    # ═══════════════════════════════════════════════════════════════════════════
    _console.rule("[bold]Step 3 of 3: Save Credentials[/bold]")

    _console.print(
        "\nWhere should credentials be saved?\n\n"
        "  [bold]1[/bold]  ~/.claude-runner/secrets.yaml  (plaintext, file permissions 600)\n"
        "  [bold]2[/bold]  Windows Credential Manager / system keyring  (encrypted, recommended)\n"
    )

    storage_choice = click.prompt("Choice", default="2").strip()

    if storage_choice == "1":
        _save_to_secrets_yaml(secrets)
    else:
        _save_to_keyring(secrets)

    # Write .gitignore template if it doesn't exist
    gitignore_path = _DEFAULT_CONFIG_DIR / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text("secrets.yaml\n*.key\n*.pem\n", encoding="utf-8")
        _ok(f"Created {gitignore_path} (protects secrets from accidental git commits).")

    _console.print(
        Panel(
            "[bold green]Configuration complete![/bold green]\n\n"
            "You can now run:\n\n"
            "  [bold cyan]claude-runner run projects/example_simple.yaml[/bold cyan]",
            border_style="green",
        )
    )


def _run_ntfy_guide(secrets: dict) -> None:
    """
    Set up ntfy.sh push notifications.

    ntfy.sh is a free, open-source pub/sub notification service.
    No account required — just pick a topic name and subscribe on your phone.
    """
    _console.print(
        Panel(
            "[bold]ntfy.sh setup[/bold]\n\n"
            "ntfy.sh is a free push-notification service — no account needed.\n\n"
            "  1. Install the ntfy app on your phone (iOS / Android)\n"
            "     or open https://ntfy.sh in your browser.\n"
            "  2. Subscribe to a topic name that only you know\n"
            "     (treat it like a password — anyone who knows it can send you messages).\n"
            "  3. Enter that topic name below.",
            border_style="cyan",
        )
    )

    topic = click.prompt("Enter your ntfy.sh topic name").strip()
    if not topic:
        _warn("Topic name is empty — skipping ntfy setup.")
        return

    ntfy_url = f"https://ntfy.sh/{topic}"

    # Send a test notification.
    _info(f"Sending test notification to {ntfy_url} …")
    try:
        import urllib.request  # noqa: PLC0415
        import urllib.error    # noqa: PLC0415
        req = urllib.request.Request(
            ntfy_url,
            data=b"claude-runner is configured and ready.",
            headers={
                "Title": "claude-runner: test notification",
                "Priority": "default",
                "Tags": "white_check_mark",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                _ok(f"Test notification sent to {ntfy_url}")
                _console.print(
                    "[dim]Check your ntfy app — you should see a message arrive now.[/dim]"
                )
            else:
                _warn(f"ntfy responded with HTTP {resp.status} — check the topic name.")
    except urllib.error.URLError as exc:
        _warn(f"Could not reach ntfy.sh: {exc}  (saved anyway; will work when online)")

    secrets["notify_webhook_url"] = ntfy_url
    _ok(f"ntfy.sh topic saved: {ntfy_url}")


def _run_gmail_app_password_guide(email_address: str, secrets: dict) -> None:
    """
    Interactive Gmail App Password guide (spec section 4.6, steps 1-4).
    """
    _console.print(
        Panel(
            "[bold]Gmail detected.[/bold] Claude-runner uses App Passwords for Gmail.\n"
            "This avoids OAuth complexity and works reliably with standard SMTP.",
            border_style="cyan",
        )
    )

    # ── Step 1 of 4: Enable 2-Step Verification ────────────────────────────────
    _console.print(
        "\n[bold]Step 1 of 4: Enable 2-Step Verification[/bold]\n"
        "─────────────────────────────────────────\n"
        "Gmail requires 2-Step Verification before App Passwords can be created.\n"
        "If you haven't enabled it yet:\n\n"
        "  [cyan]→ Open in browser:[/cyan] https://myaccount.google.com/security\n"
        "  → Under [italic]'How you sign in to Google'[/italic], click [italic]'2-Step Verification'[/italic]\n"
        "  → Follow the setup flow, then return here.\n"
    )
    choice = click.prompt(
        "Press Enter when ready, or type S to skip if already enabled",
        default="",
    ).strip().upper()

    # ── Step 2 of 4: Create an App Password ───────────────────────────────────
    _console.print(
        "\n[bold]Step 2 of 4: Create an App Password[/bold]\n"
        "──────────────────────────────────────\n"
        "  [cyan]→ Open in browser:[/cyan] https://myaccount.google.com/apppasswords\n"
        "    (If the page is not found, 2-Step Verification may not be active yet.)\n"
        "  → Under [italic]'App name'[/italic], type: [bold]claude-runner[/bold]\n"
        "  → Click [italic]'Create'[/italic]\n"
        "  → Google will show a 16-character password.\n"
        "    [bold yellow]Copy it now — it will not be shown again.[/bold yellow]\n"
    )

    app_password = click.prompt(
        "Paste your App Password here",
        hide_input=True,
        confirmation_prompt=False,
    ).strip().replace(" ", "")  # Google sometimes formats it with spaces

    # ── Step 3 of 4: Verify ───────────────────────────────────────────────────
    _console.print(
        f"\n[bold]Step 3 of 4: Verify[/bold]\n"
        f"────────────────────\n"
        f"Sending test email to [cyan]{email_address}[/cyan] …"
    )

    smtp_ok = _test_smtp(
        host="smtp.gmail.com",
        port=587,
        username=email_address,
        password=app_password,
        to_address=email_address,
    )

    if not smtp_ok:
        _warn(
            "SMTP test failed. Double-check that:\n"
            "  - The App Password was copied correctly (16 characters, no spaces)\n"
            "  - 2-Step Verification is active\n"
            "  - You are using the full Gmail address as the username\n\n"
            "Credentials will be saved anyway — you can re-run 'configure' to retry."
        )
    else:
        _ok(f"Connection successful. Test email sent to {email_address}.")

    # Store
    secrets.update(
        {
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "smtp_username": email_address,
            "smtp_password": app_password,
            "smtp_tls": True,
        }
    )

    _console.print(
        "\n[bold]Step 4 of 4: Save credentials[/bold]\n"
        "───────────────────────────────\n"
        "(Handled in Step 3 of the main wizard below.)\n"
    )


def _run_generic_smtp_guide(email_address: str, secrets: dict) -> None:
    """
    Generic SMTP configuration guide for non-Gmail providers.
    """
    _console.print("\nEnter your SMTP server details:\n")

    smtp_host = click.prompt("SMTP host", default="smtp.example.com").strip()
    smtp_port = click.prompt("SMTP port", default=587, type=int)
    smtp_username = click.prompt("SMTP username", default=email_address).strip()
    smtp_password = click.prompt(
        "SMTP password", hide_input=True, confirmation_prompt=False
    ).strip()
    use_tls = click.confirm("Use STARTTLS?", default=True)

    _console.print(f"\nTesting SMTP connection to {smtp_host}:{smtp_port} …")
    smtp_ok = _test_smtp(
        host=smtp_host,
        port=smtp_port,
        username=smtp_username,
        password=smtp_password,
        to_address=email_address,
    )

    if not smtp_ok:
        _warn(
            "SMTP test failed. Check your credentials and server settings. "
            "Saving anyway — re-run 'configure' to retry."
        )
    else:
        _ok(f"SMTP test successful.")

    secrets.update(
        {
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_username": smtp_username,
            "smtp_password": smtp_password,
            "smtp_tls": use_tls,
        }
    )


def _test_smtp(
    host: str,
    port: int,
    username: str,
    password: str,
    to_address: str,
) -> bool:
    """
    Attempt to connect and authenticate to an SMTP server.

    Sends a minimal test email if authentication succeeds.
    Returns True on success, False on any failure.
    """
    try:
        with smtplib.SMTP(host, port, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(username, password)

            subject = "[claude-runner] SMTP test successful"
            body = (
                "This is a test message from claude-runner.\n\n"
                "If you received this, your email notification settings are correct."
            )
            message = (
                f"From: {username}\r\n"
                f"To: {to_address}\r\n"
                f"Subject: {subject}\r\n"
                f"\r\n"
                f"{body}\r\n"
            )
            server.sendmail(username, [to_address], message)
        return True
    except (smtplib.SMTPException, socket.error, OSError) as exc:
        _warn(f"SMTP error: {exc}")
        return False


def _save_to_secrets_yaml(secrets: dict) -> None:
    """Write secrets to ~/.claude-runner/secrets.yaml with mode 600."""
    try:
        import yaml  # type: ignore[import]
    except ImportError:
        _abort("pyyaml is required. Install with: pip install pyyaml")

    try:
        with _DEFAULT_SECRETS_FILE.open("w", encoding="utf-8") as fh:
            yaml.dump(secrets, fh, default_flow_style=False, allow_unicode=True)

        # Set permissions to 600 (owner read/write only) on POSIX systems
        if platform.system() != "Windows":
            os.chmod(_DEFAULT_SECRETS_FILE, 0o600)

        _ok(f"Secrets saved to: {_DEFAULT_SECRETS_FILE}")
    except Exception as exc:
        _abort(f"Could not save secrets.yaml: {exc}")


def _save_to_keyring(secrets: dict) -> None:
    """
    Store each secrets entry in the system keyring (Windows Credential Manager
    on Windows, Keychain on macOS, libsecret on Linux).
    """
    try:
        import keyring  # type: ignore[import]
    except ImportError:
        _warn(
            "keyring package is not installed. "
            "Falling back to secrets.yaml.\n"
            "Install with: pip install keyring"
        )
        _save_to_secrets_yaml(secrets)
        return

    try:
        service = "claude-runner"
        for key, value in secrets.items():
            if value is not None:
                keyring.set_password(service, key, str(value))
        _ok("Credentials saved to Windows Credential Manager.")
        _console.print(
            "[dim]To view or delete: Start Menu → search 'Credential Manager'\n"
            "→ Windows Credentials → look for 'claude-runner' entries.[/dim]"
        )

        # Also save non-sensitive defaults to secrets.yaml so GlobalConfig can
        # read them without needing keyring at every startup.
        non_sensitive = {
            k: v for k, v in secrets.items()
            if k not in ("api_key", "smtp_password")
        }
        if non_sensitive:
            _save_to_secrets_yaml(non_sensitive)

    except Exception as exc:
        _warn(f"Keyring error ({exc}). Falling back to secrets.yaml.")
        _save_to_secrets_yaml(secrets)


# ──────────────────────────────────────────────────────────────────────────────
# docker subcommand group
# ──────────────────────────────────────────────────────────────────────────────

@cli.group("docker")
def docker_group() -> None:
    """Manage the claude-runner Docker base image."""


@docker_group.command("status")
def docker_status_cmd() -> None:
    """Show Docker Desktop availability and claude-runner-base image status."""
    _ensure_initialized()
    try:
        import docker as docker_sdk  # type: ignore[import]
    except ImportError:
        _abort("docker SDK not installed. Run: pip install docker")

    # ── Docker daemon ──────────────────────────────────────────────────────────
    try:
        client = docker_sdk.from_env()
        info = client.info()
        _ok(f"Docker daemon is running. Server version: {info.get('ServerVersion', '?')}")
    except Exception as exc:
        _err_console.print(f"[bold red][ERROR][/bold red] Docker is not available: {exc}")
        _err_console.print(
            "\nTo fix this:\n"
            "  • On Windows: Start Docker Desktop from the Start menu.\n"
            "  • Then wait for the Docker icon in the system tray to show 'Docker Desktop is running'."
        )
        sys.exit(1)

    # ── Base image ─────────────────────────────────────────────────────────────
    image_name = "claude-runner-base:latest"
    try:
        image = client.images.get(image_name)
        tags = image.tags or ["(untagged)"]
        created = image.attrs.get("Created", "?")[:10]
        size_mb = image.attrs.get("Size", 0) / (1024 * 1024)
        _ok(
            f"Image '{image_name}' found.\n"
            f"  Tags:    {', '.join(tags)}\n"
            f"  Created: {created}\n"
            f"  Size:    {size_mb:.0f} MB"
        )
    except docker_sdk.errors.ImageNotFound:
        _warn(
            f"Image '{image_name}' not found locally.\n"
            "  Run: claude-runner docker update"
        )
    except Exception as exc:
        _warn(f"Could not inspect image: {exc}")


@docker_group.command("update")
@click.option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="Build without using the Docker layer cache.",
)
def docker_update_cmd(no_cache: bool) -> None:
    """Rebuild the claude-runner-base Docker image with the latest pinned versions.

    This does NOT auto-update to unpinned 'latest'. Update the Dockerfile
    version pins deliberately after testing.
    """
    _ensure_initialized()
    try:
        import docker as docker_sdk  # type: ignore[import]
    except ImportError:
        _abort("docker SDK not installed. Run: pip install docker")

    # Find the Dockerfile
    dockerfile_candidates = [
        pathlib.Path(__file__).parent.parent / "docker" / "Dockerfile",
        pathlib.Path.cwd() / "docker" / "Dockerfile",
    ]
    dockerfile_path: Optional[pathlib.Path] = None
    for candidate in dockerfile_candidates:
        if candidate.exists():
            dockerfile_path = candidate
            break

    if dockerfile_path is None:
        _abort(
            "Dockerfile not found. Searched:\n"
            + "\n".join(f"  {p}" for p in dockerfile_candidates)
        )

    _info(f"Building image 'claude-runner-base:latest' from {dockerfile_path} …")
    _info("This may take several minutes on first build (downloading Node.js + Claude Code).")

    try:
        client = docker_sdk.from_env()
    except Exception as exc:
        _abort(f"Cannot connect to Docker: {exc}")

    try:
        image, build_logs = client.images.build(
            path=str(dockerfile_path.parent),
            tag="claude-runner-base:latest",
            nocache=no_cache,
            rm=True,
        )
        # Stream build output
        for log_entry in build_logs:
            stream = log_entry.get("stream", "").rstrip("\n")
            if stream:
                _console.print(f"  [dim]{stream}[/dim]")

        _ok(
            f"Image 'claude-runner-base:latest' built successfully.\n"
            f"  ID: {image.short_id}"
        )
    except docker_sdk.errors.BuildError as exc:
        _abort(f"Docker build failed: {exc}")
    except Exception as exc:
        _abort(f"Unexpected error during docker build: {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# Interactive launcher (double-click / no-args mode)
# ──────────────────────────────────────────────────────────────────────────────

_VERSION = "0.3.0"

_BANNER = f"""
\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557
\u2551      claude-runner v{_VERSION}           \u2551
\u2551  Self-orchestrating Claude Code runner  \u2551
\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d
"""

_MENU = """
  [1] Run a project          (claude-runner run <project.yaml>)
  [2] Configure              (claude-runner configure)
  [3] Check status           (claude-runner status)
  [4] View logs              (claude-runner logs)
  [5] Build Docker image     (claude-runner docker update)
  [Q] Quit

  Choice: """


def _getch() -> str:
    """Read a single keypress on Windows (no Enter needed)."""
    import msvcrt  # noqa: PLC0415
    ch = msvcrt.getch()
    try:
        return ch.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _wait_for_key(prompt: str = "\nPress any key to exit...") -> None:
    print(prompt, end="", flush=True)
    _getch()
    print()


def _bundled_projects_dir() -> Optional[pathlib.Path]:
    """Return the directory to scan for project YAML files.

    In a frozen PyInstaller exe this is the folder containing the exe itself,
    so users can place .yaml files next to claude-runner.exe and have them
    discovered automatically (no bundling needed).
    In development it is the repo root's projects/ folder.
    """
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.argv[0]).parent
    base = pathlib.Path(__file__).parent.parent
    p = base / "projects"
    return p if p.is_dir() else None


def _resolve_project_path(user_input: str) -> str:
    """Resolve a project book path entered in the interactive menu.

    Resolution order:
      1. Absolute path — used as-is.
      2. Relative path that exists under CWD — used as-is.
      3. Relative path resolved against the bundled projects directory.
      4. Fallback — return unchanged (run command will surface the error).
    """
    p = pathlib.Path(user_input)
    if p.is_absolute() or p.exists():
        return str(p)
    projects_dir = _bundled_projects_dir()
    if projects_dir:
        candidate = projects_dir / user_input
        if candidate.exists():
            return str(candidate)
        # Also try treating input as a bare filename inside projects/
        candidate2 = projects_dir / p.name
        if candidate2.exists():
            return str(candidate2)
    return user_input


def _pick_project_interactively() -> Optional[str]:
    """List bundled project YAML files and let the user pick one by number,
    or type a custom path.  Returns the resolved path, or None to cancel.
    """
    projects_dir = _bundled_projects_dir()
    yamls: list[pathlib.Path] = sorted(projects_dir.glob("*.yaml")) if projects_dir else []

    print()
    if yamls:
        print("  Bundled project books:")
        for i, y in enumerate(yamls, 1):
            print(f"    [{i}] {y.name}")
        print("    [C] Enter a custom path")
        print("    [B] Back")
        print()
        print("  Choice: ", end="", flush=True)
        key = _getch().lower()
        print(key)

        if key == "b":
            return None
        if key.isdigit():
            idx = int(key) - 1
            if 0 <= idx < len(yamls):
                return str(yamls[idx])
            print("  Invalid number.")
            _wait_for_key()
            return None
        # Fall through to custom path prompt for 'c' or anything else
    else:
        print("  No bundled project books found.")

    path = input("\n  Project book path: ").strip()
    if not path:
        return None
    return _resolve_project_path(path)


def _run_interactive_menu() -> None:
    """Interactive launcher shown when the exe is double-clicked."""
    import subprocess  # noqa: PLC0415

    # Enable ANSI / UTF-8 in cmd.exe
    if sys.platform == "win32":
        import ctypes  # noqa: PLC0415
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)

    print(_BANNER)
    print(_MENU, end="", flush=True)

    key = _getch().lower()
    print(key)  # echo the choice

    exe = sys.executable if not getattr(sys, "frozen", False) else sys.argv[0]

    if key == "1":
        project_path = _pick_project_interactively()
        if not project_path:
            return
        cmd = [exe, "run", project_path]
    elif key == "2":
        cmd = [exe, "configure"]
    elif key == "3":
        cmd = [exe, "status"]
    elif key == "4":
        cmd = [exe, "logs"]
    elif key == "5":
        cmd = [exe, "docker", "update"]
    else:
        return  # Q or anything else → exit cleanly

    print()
    try:
        subprocess.run(cmd, check=False)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[ERROR] {exc}")

    _wait_for_key()


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    """Package entry point (defined in pyproject.toml [project.scripts])."""
    # Ensure UTF-8 output on Windows regardless of system locale (e.g. GBK).
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass  # Python < 3.7 or non-reconfigurable stream

    # Double-click / no-args mode: show interactive menu instead of help text.
    if not sys.argv[1:] and sys.stdout.isatty():
        _run_interactive_menu()
        return
    cli(prog_name="claude-runner")


if __name__ == "__main__":
    main()
