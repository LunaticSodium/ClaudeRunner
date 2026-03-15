"""
Sandbox factory. Returns the appropriate sandbox backend based on config.

Usage:
    from claude_runner.sandbox import create_sandbox
    sandbox = create_sandbox(project_book, config, api_key)
    sandbox.setup()
    process = sandbox.launch_claude(prompt, on_line, on_exit)
    sandbox.teardown()
"""

import logging

from .docker_sandbox import DockerSandbox
from .native_sandbox import NativeSandbox

logger = logging.getLogger(__name__)


def create_sandbox(project_book, config, api_key: str):
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
        return NativeSandbox(project_book, config, api_key)

    if backend in ("docker", "auto"):
        if DockerSandbox.check_available():
            logger.info("Docker is available — using DockerSandbox.")
            return DockerSandbox(project_book, config, api_key)

        if backend == "docker":
            # User explicitly requested Docker; surface the error rather than silently downgrading.
            raise RuntimeError(
                "Sandbox backend is set to 'docker' but Docker Desktop is not running. "
                "Please start Docker Desktop and try again, or set sandbox.backend: native "
                "to use the soft-sandbox fallback."
            )

        # backend == "auto" and Docker is unavailable — degrade gracefully.
        logger.warning(
            "Docker is not available. Falling back to NativeSandbox. "
            "This provides significantly weaker isolation. "
            "Start Docker Desktop to enable the hard sandbox."
        )
        return NativeSandbox(project_book, config, api_key)

    raise ValueError(
        f"Unknown sandbox backend {backend!r}. "
        "Valid options are: 'docker', 'native', 'auto'."
    )


__all__ = [
    "create_sandbox",
    "DockerSandbox",
    "NativeSandbox",
]
