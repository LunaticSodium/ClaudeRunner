# claude_runner/config.py
#
# Global configuration and secrets management for claude-runner.
#
# Configuration is assembled from two optional YAML files stored under the
# user's home directory:
#
#   ~/.claude-runner/config.yaml   — non-secret runtime settings
#   ~/.claude-runner/secrets.yaml  — API key (fallback when env vars absent)
#
# The ANTHROPIC_API_KEY is resolved through a four-level priority chain so
# that the most-local / most-explicit value always wins:
#
#   Priority 1  ANTHROPIC_API_KEY already in the current process environment
#               (set before launching claude-runner, e.g. in the shell or a
#               .env file sourced by the caller).
#   Priority 2  Any other os.environ entry — on Windows this includes the
#               system-wide and user-level persistent environment variables
#               that were already expanded into the process when it started.
#               (In practice this collapses with Priority 1; it is listed
#               separately to document that we never re-read the Windows
#               registry at runtime — os.environ is the single source.)
#   Priority 3  ``api_key`` field inside ~/.claude-runner/secrets.yaml.
#   Priority 4  Windows Credential Manager via the ``keyring`` library
#               (optional dependency).  The credential is stored/retrieved
#               under service="claude-runner", username="anthropic_api_key".

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# OAuth sentinel
#
# Returned by Config.get_api_key() when Claude Code has a valid OAuth session
# (Pro/Max account login).  Callers that see this value must NOT inject
# ANTHROPIC_API_KEY into subprocess environments; Claude Code will
# authenticate using its own stored session.
# ---------------------------------------------------------------------------
OAUTH_SENTINEL = "__claude_oauth__"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentinel for "not provided in config file" — distinct from None / falsy
# ---------------------------------------------------------------------------
_MISSING = object()


# ---------------------------------------------------------------------------
# Marathon sub-config
# ---------------------------------------------------------------------------


@dataclass
class MarathonConfig:
    """Configuration for marathon (persistent daemon) mode.

    Attributes
    ----------
    enabled:
        Whether marathon mode is active.  Defaults to False (opt-in).
    poll_interval_minutes:
        How often the daemon polls the ntfy cmd channel.  Default 5 minutes.
    """

    enabled: bool = False
    poll_interval_minutes: int = 5

# Default base directory for all claude-runner user data.
_DEFAULT_HOME = Path.home() / ".claude-runner"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when the configuration is invalid or a required value is absent.

    Examples
    --------
    - ANTHROPIC_API_KEY cannot be found through any resolution path.
    - config.yaml contains an unrecognised key (future strict-mode).
    - A required path (e.g. log_dir parent) cannot be created.
    """


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


class Config:
    """Runtime configuration for claude-runner.

    Loads ``~/.claude-runner/config.yaml`` (if present) and merges it with
    compiled-in defaults.  Secrets are resolved lazily via :meth:`get_api_key`
    so that the object can be constructed even when no key is available yet
    (useful for ``claude-runner config set`` style commands).

    Attributes
    ----------
    sandbox_backend:
        Which sandbox implementation to use when running Claude Code.
        ``"docker"`` (default) runs tasks inside an isolated container;
        ``"native"`` runs them directly on the host (no isolation).
    log_dir:
        Directory where per-task log files are written.
        Default: ``~/.claude-runner/logs/``.
    state_dir:
        Directory for persistent task state (resume checkpoints, etc.).
        Default: ``~/.claude-runner/state/``.
    tui:
        Whether to render the Rich-based terminal UI.  Set to ``False`` to
        emit plain-text log lines instead (useful when stdout is not a TTY
        or when captured by a CI system).
    resume_strategy:
        Global default for how interrupted tasks are resumed.
        ``"continue"`` — append a short continuation prompt.
        ``"restate"``  — re-send the full original task prompt.
        ``"summarize"`` — inject a summary of prior work before continuing.
        Overridden per-task by the project book's ``execution.resume_strategy``.
    max_rate_limit_waits:
        How many consecutive Anthropic rate-limit responses to tolerate before
        the task is considered failed.  0 = fail immediately on first limit.
    docker_base_image:
        Docker image used as the base for task containers.  Must have
        ``node``, ``git``, and the Claude CLI pre-installed.
    docker_socket:
        Named-pipe path to the Docker Engine on Windows.
        Override if using a non-default Docker Desktop installation or a
        Podman-compatible socket.
    """

    # ------------------------------------------------------------------
    # Compiled-in defaults
    # ------------------------------------------------------------------

    sandbox_backend: str = "docker"
    log_dir: Path = _DEFAULT_HOME / "logs"
    state_dir: Path = _DEFAULT_HOME / "state"
    tui: bool = True
    resume_strategy: str = "continue"
    max_rate_limit_waits: int = 20
    docker_base_image: str = "claude-runner-base:latest"
    docker_socket: str = "npipe:////./pipe/docker_engine"

    # Valid choices for resume_strategy — validated on load.
    _VALID_RESUME_STRATEGIES: frozenset[str] = frozenset(
        {"continue", "restate", "summarize"}
    )

    # Marathon mode sub-config (always present; opt-in via marathon.enabled).
    marathon: MarathonConfig = None  # type: ignore[assignment]  # set in __init__

    # Global feature defaults — can be overridden per project book.
    # cccs_enabled: inject the CCCS C# standards preset into every run by default.
    # marathon_mode_default: skip phase-aware model switching for every run by default.
    cccs_enabled: bool = False
    marathon_mode_default: bool = False

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        # Set instance defaults from class-level attributes so that each
        # Config instance is independent (mutations don't affect the class).
        self.sandbox_backend = Config.sandbox_backend
        self.log_dir = Config.log_dir
        self.state_dir = Config.state_dir
        self.tui = Config.tui
        self.marathon = MarathonConfig()
        self.cccs_enabled = Config.cccs_enabled
        self.marathon_mode_default = Config.marathon_mode_default
        self.resume_strategy = Config.resume_strategy
        self.max_rate_limit_waits = Config.max_rate_limit_waits
        self.docker_base_image = Config.docker_base_image
        self.docker_socket = Config.docker_socket

        # Internal cache for the resolved API key (avoids repeated keyring
        # lookups which may be slow on first call).
        self._api_key_cache: str | None = None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def load(cls) -> "Config":
        """Load configuration from ``~/.claude-runner/config.yaml``.

        Missing file → silently return defaults.
        Present file → merge user values on top of defaults.

        Raises
        ------
        ConfigError
            If config.yaml exists but cannot be parsed as valid YAML, or if
            it contains an invalid value for a known setting (e.g. an
            unrecognised ``resume_strategy``).
        """
        cfg = cls()
        config_path = _DEFAULT_HOME / "config.yaml"

        if not config_path.exists():
            logger.debug("No config file found at %s; using defaults.", config_path)
            return cfg

        logger.debug("Loading config from %s", config_path)
        try:
            raw: dict[str, Any] = _load_yaml_file(config_path) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(
                f"Failed to parse config file {config_path}: {exc}"
            ) from exc

        cfg._apply_dict(raw, source=str(config_path))
        return cfg

    # ------------------------------------------------------------------
    # API key resolution
    # ------------------------------------------------------------------

    def get_api_key(self) -> str:
        """Resolve ANTHROPIC_API_KEY through the four-level priority chain.

        The result is cached in ``_api_key_cache`` after the first successful
        resolution so that repeated calls (e.g. after a rate-limit back-off
        loop) do not incur repeated I/O.

        Returns
        -------
        str
            The raw API key string.

        Raises
        ------
        ConfigError
            If no key can be found through any of the four resolution paths.
        """
        if self._api_key_cache:
            return self._api_key_cache

        # ------------------------------------------------------------------
        # Priority 1 & 2: environment variable (covers both the current-shell
        # export and any Windows system/user env vars already in the process).
        # ------------------------------------------------------------------
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if key:
            logger.debug("ANTHROPIC_API_KEY resolved from environment variable.")
            self._api_key_cache = key
            return key

        # ------------------------------------------------------------------
        # Priority 3: ~/.claude-runner/secrets.yaml
        # ------------------------------------------------------------------
        secrets_path = _DEFAULT_HOME / "secrets.yaml"
        if secrets_path.exists():
            try:
                secrets: dict[str, Any] = _load_yaml_file(secrets_path) or {}
                key = str(secrets.get("api_key", "")).strip()
                if key:
                    logger.debug(
                        "ANTHROPIC_API_KEY resolved from secrets file %s.", secrets_path
                    )
                    self._api_key_cache = key
                    return key
            except yaml.YAMLError as exc:
                logger.warning(
                    "Failed to parse secrets file %s: %s — skipping.", secrets_path, exc
                )

        # ------------------------------------------------------------------
        # Priority 4: Windows Credential Manager via keyring (optional dep).
        # ------------------------------------------------------------------
        key = _resolve_from_keyring()
        if key:
            logger.debug("ANTHROPIC_API_KEY resolved from Windows Credential Manager.")
            self._api_key_cache = key
            return key

        # ------------------------------------------------------------------
        # Priority 5: Claude Code OAuth session (Pro/Max account login).
        # If a valid session is found, skip API key injection entirely.
        # ------------------------------------------------------------------
        if _detect_oauth_session():
            logger.info(
                "Claude Code OAuth session detected — API key injection skipped. "
                "Claude Code will authenticate using its stored session."
            )
            self._api_key_cache = OAUTH_SENTINEL
            return OAUTH_SENTINEL

        # ------------------------------------------------------------------
        # Nothing found — surface a helpful error.
        # ------------------------------------------------------------------
        raise ConfigError(
            "ANTHROPIC_API_KEY could not be found.  Provide it by one of:\n"
            "  1. Setting the ANTHROPIC_API_KEY environment variable.\n"
            "  2. Adding 'api_key: sk-...' to ~/.claude-runner/secrets.yaml.\n"
            "  3. Running: claude-runner configure  (stores in Credential Manager).\n"
            "  4. Logging in to Claude Code with a Pro/Max account: run 'claude' once."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_dict(self, data: dict[str, Any], source: str) -> None:
        """Merge a parsed YAML dict into this Config instance.

        Only recognised fields are applied; unrecognised keys emit a warning
        rather than raising, to stay forward-compatible with newer config
        versions being read by an older claude-runner binary.
        """
        _str_fields = {"sandbox_backend", "resume_strategy", "docker_base_image", "docker_socket"}
        _path_fields = {"log_dir", "state_dir"}
        _bool_fields = {"tui", "cccs_enabled", "marathon_mode_default"}
        _int_fields = {"max_rate_limit_waits"}

        known_fields = _str_fields | _path_fields | _bool_fields | _int_fields

        for key, value in data.items():
            if key not in known_fields:
                logger.warning(
                    "Unrecognised key %r in %s — ignoring.", key, source
                )
                continue

            if key in _str_fields:
                setattr(self, key, str(value))
            elif key in _path_fields:
                setattr(self, key, Path(value).expanduser())
            elif key in _bool_fields:
                setattr(self, key, bool(value))
            elif key in _int_fields:
                try:
                    setattr(self, key, int(value))
                except (TypeError, ValueError) as exc:
                    raise ConfigError(
                        f"Invalid value for {key!r} in {source}: expected integer, got {value!r}"
                    ) from exc

        # Post-load validation
        if self.resume_strategy not in self._VALID_RESUME_STRATEGIES:
            raise ConfigError(
                f"Invalid resume_strategy {self.resume_strategy!r} in {source}.  "
                f"Must be one of: {sorted(self._VALID_RESUME_STRATEGIES)}."
            )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"Config("
            f"sandbox_backend={self.sandbox_backend!r}, "
            f"log_dir={self.log_dir}, "
            f"state_dir={self.state_dir}, "
            f"tui={self.tui}, "
            f"resume_strategy={self.resume_strategy!r}, "
            f"max_rate_limit_waits={self.max_rate_limit_waits}, "
            f"docker_base_image={self.docker_base_image!r}"
            f")"
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _load_yaml_file(path: Path) -> dict[str, Any]:
    """Read a YAML file and return its top-level mapping.

    Parameters
    ----------
    path:
        Absolute path to the YAML file.

    Returns
    -------
    dict
        Parsed content, or an empty dict if the file is empty.

    Raises
    ------
    yaml.YAMLError
        Re-raised from PyYAML on parse failure.
    """
    with path.open("r", encoding="utf-8") as fh:
        content = yaml.safe_load(fh)
    if content is None:
        return {}
    if not isinstance(content, dict):
        raise yaml.YAMLError(
            f"Expected a YAML mapping at the top level of {path}, "
            f"got {type(content).__name__}."
        )
    return content  # type: ignore[return-value]


def _detect_oauth_session() -> bool:
    """Return True if Claude Code has a usable authenticated session.

    Detection logic (tried in order, first hit wins)
    -------------------------------------------------
    1. ``~/.claude/.credentials.json`` — classic OAuth token file.
    2. ``claude auth status`` CLI probe — covers claude.ai login via Windows
       Credential Manager or other platform-native storage that doesn't write
       a credentials file.

    All I/O and subprocess errors are caught and treated as "no session".
    """
    import json  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import time  # noqa: PLC0415

    if shutil.which("claude") is None:
        return False

    # --- Method 1: credentials file ---
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if creds_path.exists():
        try:
            data = json.loads(creds_path.read_text(encoding="utf-8"))
            oauth = data.get("claudeAiOauth") or {}
            access_token = (oauth.get("accessToken") or "").strip()
            if access_token:
                expires_at_ms = oauth.get("expiresAt", 0)
                if expires_at_ms:
                    expires_at_s = expires_at_ms / 1000
                    if expires_at_s < time.time():
                        return bool((oauth.get("refreshToken") or "").strip())
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("OAuth credentials file probe failed: %s", exc)

    # --- Method 2: `claude auth status` CLI probe ---
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        output = (result.stdout + result.stderr).strip()
        # `claude auth status` outputs JSON; parse it.
        data = json.loads(output)
        if data.get("loggedIn"):
            logger.debug(
                "Claude auth status: loggedIn=true (method=%s)",
                data.get("authMethod", "unknown"),
            )
            return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("claude auth status probe failed: %s", exc)

    return False


def _resolve_from_keyring() -> str:
    """Attempt to retrieve the API key from the Windows Credential Manager.

    The ``keyring`` package is an optional dependency.  This function
    gracefully handles the case where it is not installed.

    Returns
    -------
    str
        The API key, or an empty string if not found or keyring unavailable.
    """
    try:
        import keyring  # type: ignore[import-untyped]
    except ImportError:
        logger.debug(
            "keyring package not installed; skipping Credential Manager lookup. "
            "Install with: pip install claude-runner[keyring]"
        )
        return ""

    try:
        credential = keyring.get_password("claude-runner", "anthropic_api_key")
        return (credential or "").strip()
    except Exception as exc:  # noqa: BLE001 — keyring can raise various backend errors
        logger.warning("keyring lookup failed: %s — skipping.", exc)
        return ""


def store_api_key_in_keyring(api_key: str) -> None:
    """Persist *api_key* in the Windows Credential Manager.

    Called by ``claude-runner config set-key`` to store the key so that
    future invocations can find it via Priority 4 without requiring an
    environment variable.

    Raises
    ------
    ConfigError
        If the ``keyring`` package is not installed.
    """
    try:
        import keyring  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ConfigError(
            "The keyring package is required to store credentials. "
            "Install with: pip install claude-runner[keyring]"
        ) from exc

    keyring.set_password("claude-runner", "anthropic_api_key", api_key)
    logger.info("API key stored in Windows Credential Manager under 'claude-runner'.")


# Alias for callers that expect the name GlobalConfig (e.g. main.py).
GlobalConfig = Config
