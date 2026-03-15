"""
Sandbox factory. Returns the appropriate sandbox backend based on config.

Usage:
    from claude_runner.sandbox import create_sandbox, resolve_working_dir
    sandbox = create_sandbox(project_book, config, api_key, book_path=path)
    sandbox.setup()
    process = sandbox.launch_claude(prompt, on_line, on_exit)
    sandbox.teardown()
"""

import logging
from pathlib import Path
from typing import Optional

from .docker_sandbox import DockerSandbox
from .native_sandbox import NativeSandbox

logger = logging.getLogger(__name__)


def resolve_working_dir(
    project_book,
    book_path: Optional[Path] = None,
) -> Path:
    """Return the effective working directory for a task.

    Resolution order (first match wins):

    1. ``project_book.sandbox.working_dir`` — explicitly set in the YAML.
    2. Sibling folder derived from the YAML filename stem — the convention
       for the one-YAML-one-folder layout (e.g. ``pj1.yaml`` → ``./pj1/``).
    3. ``~/claude-runner/workspace`` — last-resort fallback with a warning.

    The returned directory is created (with parents) if it does not yet exist.
    """
    pb_sandbox = getattr(project_book, "sandbox", None)
    explicit = getattr(pb_sandbox, "working_dir", None) if pb_sandbox is not None else None

    if explicit is not None:
        wd = Path(explicit)
    elif book_path is not None:
        bp = Path(book_path)
        wd = bp.parent / bp.stem
        logger.info(
            "sandbox.working_dir not set — using sibling folder: %s", wd
        )
    else:
        wd = Path.home() / "claude-runner" / "workspace"
        logger.warning(
            "sandbox.working_dir not set and no book_path known — "
            "falling back to %s", wd
        )

    if not wd.exists():
        logger.info("Working directory %s does not exist — creating it.", wd)
        wd.mkdir(parents=True, exist_ok=True)
    elif not wd.is_dir():
        raise ValueError(
            f"sandbox.working_dir path exists but is not a directory: {wd!r}"
        )

    return wd


def create_sandbox(project_book, config, api_key: str, book_path: Optional[Path] = None):
    """
    Returns DockerSandbox if docker backend is configured and Docker is available,
    otherwise falls back to NativeSandbox with a warning log.

    Backend resolution order (highest priority first):
      1. project_book.sandbox.backend  (per-task override)
      2. config.sandbox_backend        (global default, usually "auto")

    Parameters
    ----------
    project_book:
        Parsed project configuration object (ProjectBook).
    config:
        Global claude-runner configuration object.
    api_key:
        Anthropic API key passed through to the sandbox environment.
    book_path:
        Path to the source YAML file.  Used by resolve_working_dir() to
        derive the default working directory when sandbox.working_dir is
        not set in the project book.

    Returns
    -------
    DockerSandbox | NativeSandbox
    """
    # Per-task backend wins over global config.
    pb_sandbox = getattr(project_book, "sandbox", None)
    pb_backend = getattr(pb_sandbox, "backend", None) if pb_sandbox is not None else None

    if pb_backend and pb_backend != "auto":
        backend = pb_backend.lower()
        logger.info("Using project-book sandbox backend: %r", backend)
    else:
        backend = getattr(config, "sandbox_backend", "auto").lower()

    if backend == "native":
        logger.warning(
            "Sandbox backend is 'native'. "
            "NativeSandbox provides significantly weaker isolation than DockerSandbox."
        )
        return NativeSandbox(project_book, config, api_key, book_path=book_path)

    if backend in ("docker", "auto"):
        if DockerSandbox.check_available():
            logger.info("Docker is available — using DockerSandbox.")
            return DockerSandbox(project_book, config, api_key, book_path=book_path)

        if backend == "docker":
            raise RuntimeError(
                "Sandbox backend is set to 'docker' but Docker Desktop is not running. "
                "Please start Docker Desktop and try again, or set sandbox.backend: native "
                "to use the soft-sandbox fallback."
            )

        logger.warning(
            "Docker is not available. Falling back to NativeSandbox. "
            "This provides significantly weaker isolation. "
            "Start Docker Desktop to enable the hard sandbox."
        )
        return NativeSandbox(project_book, config, api_key, book_path=book_path)

    raise ValueError(
        f"Unknown sandbox backend {backend!r}. "
        "Valid options are: 'docker', 'native', 'auto'."
    )


__all__ = [
    "create_sandbox",
    "resolve_working_dir",
    "DockerSandbox",
    "NativeSandbox",
]
