"""
NativeSandbox — soft-sandbox fallback for systems without Docker.

Uses Claude Code as a local subprocess with its own --sandbox flag (where the
installed Claude Code version supports it) for light process isolation.

WARNING: This provides significantly weaker isolation than DockerSandbox.
         claude-runner logs a warning whenever NativeSandbox is active.

Requirements
------------
- ``claude`` CLI must be on the system PATH.
- Node.js must be installed (required by Claude Code).
- ``ANTHROPIC_API_KEY`` is injected into the subprocess environment; it is
  never written to disk by claude-runner itself.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

_OAUTH_SENTINEL: str = "__claude_oauth__"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy import of ClaudeProcess — lives in the sibling process module.
# We import lazily so that the sandbox package can be imported in environments
# where winpty / ConPTY isn't yet set up (e.g. during unit tests).
# ---------------------------------------------------------------------------

def _import_pipe_process():
    try:
        from claude_runner.process import PipeProcess  # noqa: PLC0415
        return PipeProcess
    except ImportError as exc:
        raise RuntimeError(
            "Could not import PipeProcess from claude_runner.process. "
            "Ensure the 'process' module is present in the claude_runner package."
        ) from exc


# ---------------------------------------------------------------------------
# SandboxError (re-exported so callers can catch a single type)
# ---------------------------------------------------------------------------

class SandboxError(RuntimeError):
    """Raised when the sandbox cannot be created or operated."""


# ---------------------------------------------------------------------------
# NativeSandbox
# ---------------------------------------------------------------------------

class NativeSandbox:
    """
    Soft sandbox fallback for systems without Docker.

    Uses Claude Code's own ``--sandbox`` flag (where available) for process
    isolation. The working directory is set to
    ``project_book.sandbox.working_dir``.

    WARNING: This provides significantly weaker isolation than DockerSandbox.
             claude-runner logs a warning when this backend is used.

    Requirements: Claude Code and Node.js must be installed on the host.
    """

    def __init__(self, project_book, config, api_key: str, book_path=None, *, show_claude: bool = False) -> None:
        self._project_book = project_book
        self._config = config
        self._api_key = api_key
        self._book_path = Path(book_path) if book_path is not None else None
        self._show_claude = show_claude

        # Derive sandbox config ----------------------------------------
        sandbox_cfg = getattr(config, "sandbox", None) or {}
        # Default False on Windows: Claude Code's --sandbox uses OS primitives
        # (Linux namespaces / macOS sandbox) that don't exist on Windows and
        # cause an immediate crash (exit 0xC000013A).
        import sys as _sys  # noqa: PLC0415
        _default_sandbox_flag = _sys.platform != "win32"
        self._use_sandbox_flag: bool = _cfg_get(sandbox_cfg, "use_claude_sandbox_flag", _default_sandbox_flag)
        self._extra_env: dict = _cfg_get(sandbox_cfg, "extra_env", {})

        # Runtime state ------------------------------------------------
        self._env: Optional[dict] = None
        self._process: Optional[object] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """
        Validate that 'claude' is on PATH and Node.js is available.
        Inject ANTHROPIC_API_KEY into the subprocess environment.

        Raises
        ------
        SandboxError
            If 'claude' or 'node' cannot be found on PATH.
        """
        logger.warning(
            "NativeSandbox is active. This provides significantly weaker isolation "
            "than DockerSandbox. Start Docker Desktop to enable the hard sandbox."
        )

        claude_path = shutil.which("claude")
        if claude_path is None:
            raise SandboxError(
                "'claude' was not found on PATH. "
                "Install Claude Code with: npm install -g @anthropic-ai/claude-code"
            )
        logger.info("Found 'claude' at: %s", claude_path)

        node_path = shutil.which("node")
        if node_path is None:
            raise SandboxError(
                "'node' was not found on PATH. "
                "Claude Code requires Node.js. Install it from https://nodejs.org/"
            )
        logger.debug("Found 'node' at: %s", node_path)

        # Check whether the installed Claude Code supports --sandbox.
        self._has_sandbox_flag = self._probe_sandbox_flag(claude_path)
        if self._use_sandbox_flag and not self._has_sandbox_flag:
            logger.warning(
                "The installed Claude Code does not support the --sandbox flag. "
                "Continuing without it."
            )

        # Build the subprocess environment: inherit host env, then overlay.
        # OAuth mode: Claude Code already has a stored session — do not inject
        # ANTHROPIC_API_KEY (the env var would override the OAuth flow).
        self._env = {**os.environ, "TERM": "xterm-256color", **self._extra_env}
        if self._api_key != _OAUTH_SENTINEL:
            self._env["ANTHROPIC_API_KEY"] = self._api_key

        # Claude Code on Windows requires git bash for shell operations.
        # Probe common install locations and inject CLAUDE_CODE_GIT_BASH_PATH
        # if it is not already set.  Without this, 'claude' exits with code 1
        # and the message "Claude Code on Windows requires git-bash".
        if "CLAUDE_CODE_GIT_BASH_PATH" not in self._env:
            _bash_candidates = [
                Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                / "Git" / "usr" / "bin" / "bash.exe",
                Path.home() / "AppData" / "Local" / "Programs"
                / "Git" / "usr" / "bin" / "bash.exe",
            ]
            for _bash in _bash_candidates:
                if _bash.exists():
                    self._env["CLAUDE_CODE_GIT_BASH_PATH"] = str(_bash)
                    logger.info("Auto-detected git bash: %s", _bash)
                    break
            else:
                logger.warning(
                    "git bash not found in common locations. "
                    "Claude Code may fail on Windows. "
                    "Set CLAUDE_CODE_GIT_BASH_PATH to your bash.exe path."
                )

        # Ensure working directory exists.
        working_dir = self.get_working_dir_path()
        working_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Working directory: %s", working_dir)

    def launch_claude(
        self,
        prompt: str,
        on_line: Callable[[str], None],
        on_exit: Callable[[int], None],
    ) -> object:
        """
        Launch Claude Code as a local subprocess using ConPTY (via ClaudeProcess).

        Parameters
        ----------
        prompt:
            The task prompt passed to ``claude -p``.
        on_line:
            Callback invoked with each decoded output line.
        on_exit:
            Callback invoked with the process exit code when it terminates.

        Returns
        -------
        ClaudeProcess
            The live process wrapper.

        Raises
        ------
        SandboxError
            If setup() has not been called.
        """
        if self._env is None:
            raise SandboxError("setup() must be called before launch_claude().")

        PipeProcess = _import_pipe_process()

        cmd = self._build_command(prompt)
        working_dir = self.get_working_dir_path()

        logger.info(
            "Launching Claude (native/pipe): %s  [cwd=%s]",
            " ".join(cmd[:3]),  # omit the prompt from the log line
            working_dir,
        )

        self._process = PipeProcess(
            command=cmd,
            working_dir=working_dir,
            env=self._env,
            on_line=on_line,
            on_exit=on_exit,
            show_console=self._show_claude,
        )
        try:
            self._process.start()
        except Exception:
            # start() already killed the subprocess before raising; clear the
            # reference so teardown() does not try to touch a dead process.
            self._process = None
            raise
        return self._process

    def teardown(self) -> None:
        """
        Terminate the subprocess if still running and release resources.

        This is a best-effort cleanup; errors are logged but not re-raised so
        that teardown never masks an earlier exception.
        """
        if self._process is not None:
            try:
                if hasattr(self._process, "is_alive") and self._process.is_alive():
                    logger.info("Terminating Claude subprocess …")
                    if hasattr(self._process, "terminate"):
                        self._process.terminate()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error terminating Claude subprocess: %s", exc)
            self._process = None

        # NativeSandbox does not create ephemeral directories, so no further
        # filesystem cleanup is required here. If the caller wants to wipe the
        # working directory it should do so explicitly.
        logger.debug("NativeSandbox teardown complete.")

    def get_working_dir_path(self) -> Path:
        """Returns the host-side working directory path for this task."""
        from . import resolve_working_dir  # noqa: PLC0415
        return resolve_working_dir(self._project_book, book_path=self._book_path)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_command(self, prompt: str) -> list[str]:
        """Construct the claude CLI command list."""
        cmd = ["claude", "--dangerously-skip-permissions"]

        if self._use_sandbox_flag and getattr(self, "_has_sandbox_flag", False):
            cmd.append("--sandbox")

        cmd += ["--output-format", "stream-json", "--verbose"]
        cmd += ["-p", prompt]
        return cmd

    @staticmethod
    def _probe_sandbox_flag(claude_path: str) -> bool:
        """
        Return True if the installed Claude Code accepts the --sandbox flag.

        We do this by running ``claude --help`` and checking for "sandbox" in
        the output. This avoids actually launching a session.
        """
        try:
            result = subprocess.run(
                [claude_path, "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            combined = (result.stdout + result.stderr).lower()
            return "--sandbox" in combined or "sandbox" in combined
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not probe claude --help: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _cfg_get(cfg, key: str, default):
    """
    Retrieve a value from a config object that may be a dict or an
    attribute-bearing object (e.g. a dataclass / SimpleNamespace).
    """
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)
