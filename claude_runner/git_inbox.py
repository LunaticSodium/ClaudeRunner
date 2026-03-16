"""
claude_runner/git_inbox.py

Git repository as file transfer channel (Feature A2).

Implements fetch_branch(branch_ref) which:
  1. Reads the GitHub token from Windows Credential Manager.
  2. Clones the specified branch (shallow) into a temp directory.
  3. Scans for *.yaml files and validates each as a ProjectBook.
  4. Enqueues valid project books via daemon.enqueue().
  5. Logs a warning for invalid files (does not raise).
  6. Cleans up the temp directory.

Credential storage
------------------
keyring service name : "claude-runner-github-token"
keyring username     : "token"

Repo URL
--------
Read from Windows Credential Manager via the same keyring service
(username: "repo_url").  If not found, falls back to the env var
CLAUDE_RUNNER_GITHUB_REPO_URL, then a hard-coded None (which causes
the function to log an error and skip).

Branch pattern (enforced by pipeline.py)
-----------------------------------------
  task/<name>  or  inbox/<iso-timestamp>
"""
from __future__ import annotations

import logging
import pathlib
import shutil
import subprocess
import tempfile
from typing import Optional

import yaml
from pydantic import ValidationError

from .project import ProjectBook

logger = logging.getLogger(__name__)

_KEYRING_SERVICE_GITHUB = "claude-runner-github-token"
_GITHUB_TOKEN_USERNAME = "token"
_GITHUB_REPO_URL_USERNAME = "repo_url"


def fetch_branch(branch_ref: str, daemon) -> None:
    """
    Clone *branch_ref* from the configured GitHub repo and enqueue valid YAML
    project books found in the branch.

    Parameters
    ----------
    branch_ref:
        Branch name matching ``task/<name>`` or ``inbox/<iso-timestamp>``.
    daemon:
        MarathonDaemon instance exposing an ``enqueue(book, path)`` method.

    Behaviour
    ---------
    - If no GitHub token is found in Credential Manager → log error, return.
    - If no repo URL is configured → log error, return.
    - For each ``*.yaml`` in the cloned branch:
        - If valid ProjectBook → enqueue.
        - If invalid → log warning, continue.
    - Always cleans up the temp directory.
    """
    token = _get_github_token()
    if not token:
        logger.error(
            "A2: No GitHub token found in Credential Manager "
            "(service=%r, username=%r).  Skipping fetch.",
            _KEYRING_SERVICE_GITHUB,
            _GITHUB_TOKEN_USERNAME,
        )
        return

    repo_url = _get_repo_url()
    if not repo_url:
        logger.error(
            "A2: No GitHub repo URL configured.  "
            "Set via keyring (service=%r, username=%r) or env var "
            "CLAUDE_RUNNER_GITHUB_REPO_URL.  Skipping fetch.",
            _KEYRING_SERVICE_GITHUB,
            _GITHUB_REPO_URL_USERNAME,
        )
        return

    # Embed the token into the URL for authentication.
    auth_url = _embed_token(repo_url, token)

    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="cr-git-inbox-"))
    try:
        try:
            _clone_branch(auth_url, branch_ref, tmp_dir)
        except subprocess.CalledProcessError as exc:
            logger.error(
                "A2: git clone failed for branch %r (exit %d): %s",
                branch_ref, exc.returncode, (exc.stderr or "").strip()[:200],
            )
            return
        yaml_files = list(tmp_dir.glob("**/*.yaml"))
        logger.info("A2: found %d yaml file(s) in branch %r.", len(yaml_files), branch_ref)

        for yaml_path in yaml_files:
            _try_enqueue(yaml_path, daemon)
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.debug("A2: cleaned up temp dir %s.", tmp_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("A2: failed to clean up temp dir %s: %s", tmp_dir, exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_github_token() -> Optional[str]:
    """Retrieve the GitHub token from Windows Credential Manager."""
    try:
        import keyring  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("A2: keyring not installed — cannot retrieve GitHub token.")
        return None
    try:
        value = keyring.get_password(_KEYRING_SERVICE_GITHUB, _GITHUB_TOKEN_USERNAME)
        return (value or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("A2: keyring lookup for GitHub token failed: %s", exc)
        return None


def _get_repo_url() -> Optional[str]:
    """Retrieve the GitHub repo URL from keyring, then environment variable."""
    # Try keyring first.
    try:
        import keyring  # type: ignore[import-untyped]
        value = keyring.get_password(_KEYRING_SERVICE_GITHUB, _GITHUB_REPO_URL_USERNAME)
        if value and value.strip():
            return value.strip()
    except Exception:  # noqa: BLE001
        pass

    # Fallback: environment variable.
    import os  # noqa: PLC0415
    env_url = os.environ.get("CLAUDE_RUNNER_GITHUB_REPO_URL", "").strip()
    return env_url or None


def _embed_token(repo_url: str, token: str) -> str:
    """Insert *token* into *repo_url* for authenticated git operations."""
    if repo_url.startswith("https://"):
        return "https://" + token + "@" + repo_url[len("https://"):]
    # Already has auth or uses SSH — pass through unchanged.
    return repo_url


def _clone_branch(auth_url: str, branch_ref: str, dest: pathlib.Path) -> None:
    """
    Run ``git clone --branch <branch_ref> --depth 1 <auth_url> <dest>``.

    Raises
    ------
    subprocess.CalledProcessError
        If git clone fails.
    """
    cmd = [
        "git", "clone",
        "--branch", branch_ref,
        "--depth", "1",
        auth_url,
        str(dest),
    ]
    logger.info("A2: cloning branch %r into %s.", branch_ref, dest)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    logger.info("A2: clone complete.")


def _try_enqueue(yaml_path: pathlib.Path, daemon) -> None:
    """
    Attempt to load and validate *yaml_path* as a ProjectBook, then enqueue.

    Logs a warning and continues on any error (does not raise).
    """
    try:
        raw_text = yaml_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(raw_text)
        if not isinstance(raw, dict):
            logger.warning("A2: %s is not a YAML mapping — skipping.", yaml_path.name)
            return
        book = ProjectBook.model_validate(raw)
    except (yaml.YAMLError, ValidationError, OSError) as exc:
        logger.warning("A2: invalid YAML/ProjectBook in %s — skipping. (%s)", yaml_path.name, exc)
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("A2: unexpected error reading %s — skipping. (%s)", yaml_path.name, exc)
        return

    try:
        daemon.enqueue(book, yaml_path)
        logger.info("A2: enqueued project book %r from %s.", book.name, yaml_path.name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("A2: daemon.enqueue() failed for %s: %s", yaml_path.name, exc)
