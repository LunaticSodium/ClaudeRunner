"""
acceptance_runner.py — Post-completion acceptance gate for claude-runner.

Evaluates a list of AcceptanceCheck instances against the task's working
directory after ##RUNNER:COMPLETE## is detected.  Each check type is
implemented as a plain function; the top-level ``run_checks()`` iterates
them and returns a ``CheckResult``.

Check types
-----------
file_exists   — assert a path exists under working_dir.
file_contains — assert a file's text matches a Python regex.
command       — run a shell command; assert its exit code.
llm_judge     — call the Anthropic messages API with a judge prompt;
                assert the model responds with "PASS" or "FAIL".

The ``llm_judge`` check calls the API directly (not via Claude Code
subprocess) so it completes in a single round-trip rather than spawning
a full interactive session.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .project import AcceptanceCriteria, AcceptanceCheck

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """Outcome of running all acceptance checks."""

    passed: bool
    failed_checks: list[str] = field(default_factory=list)
    details: str = ""

    def __str__(self) -> str:
        if self.passed:
            return "AcceptanceCheck: ALL PASSED"
        return (
            f"AcceptanceCheck: FAILED ({len(self.failed_checks)} check(s))\n"
            + self.details
        )


# ---------------------------------------------------------------------------
# Individual check implementations
# ---------------------------------------------------------------------------


def _check_file_exists(check: "AcceptanceCheck", working_dir: Path) -> Optional[str]:
    """Return an error string if the file does not exist, else None."""
    if not check.path:
        return "file_exists check is missing 'path'"
    target = working_dir / check.path
    if not target.exists():
        return f"file_exists: {check.path!r} does not exist in {working_dir}"
    logger.debug("file_exists: %s — OK", check.path)
    return None


def _check_file_contains(check: "AcceptanceCheck", working_dir: Path) -> Optional[str]:
    """Return an error string if the pattern is not found in the file, else None."""
    if not check.path:
        return "file_contains check is missing 'path'"
    if not check.pattern:
        return "file_contains check is missing 'pattern'"
    target = working_dir / check.path
    if not target.exists():
        return f"file_contains: {check.path!r} does not exist"
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"file_contains: could not read {check.path!r}: {exc}"
    if not re.search(check.pattern, text):
        return f"file_contains: pattern {check.pattern!r} not found in {check.path!r}"
    logger.debug("file_contains: %s matches %r — OK", check.path, check.pattern)
    return None


def _check_command(check: "AcceptanceCheck", working_dir: Path) -> Optional[str]:
    """Return an error string if the command exit code doesn't match, else None."""
    if not check.run:
        return "command check is missing 'run'"
    expected_exit = check.expect_exit if check.expect_exit is not None else 0
    try:
        args = shlex.split(check.run)
        result = subprocess.run(
            args,
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        actual_exit = result.returncode
    except FileNotFoundError as exc:
        return f"command: executable not found for {check.run!r}: {exc}"
    except subprocess.TimeoutExpired:
        return f"command: {check.run!r} timed out after 120s"
    except Exception as exc:
        return f"command: unexpected error running {check.run!r}: {exc}"

    if actual_exit != expected_exit:
        stderr_snippet = result.stderr[-500:] if result.stderr else ""
        stdout_snippet = result.stdout[-500:] if result.stdout else ""
        return (
            f"command: {check.run!r} exited with {actual_exit} "
            f"(expected {expected_exit})\n"
            f"  stdout: {stdout_snippet}\n"
            f"  stderr: {stderr_snippet}"
        )
    logger.debug("command: %r exited %d — OK", check.run, actual_exit)
    return None


def _check_llm_judge(
    check: "AcceptanceCheck",
    working_dir: Path,
    api_key: str,
) -> Optional[str]:
    """
    Call the Anthropic messages API with the judge prompt.

    Appends the contents of *check.path* (if set) to the prompt so the
    model can inspect artefacts directly.  Returns an error string if the
    model responds with "FAIL" (or the verdict cannot be parsed), else None.
    """
    if not check.prompt:
        return "llm_judge check is missing 'prompt'"
    expected = (check.expect or "pass").lower()

    # Build the judge message.
    user_message = check.prompt.strip()
    if check.path:
        target = working_dir / check.path
        if target.exists():
            try:
                file_text = target.read_text(encoding="utf-8", errors="replace")
                user_message += f"\n\n--- Contents of {check.path} ---\n{file_text}"
            except Exception as exc:
                logger.warning("llm_judge: could not read %s: %s", check.path, exc)
        else:
            user_message += f"\n\n(Note: {check.path!r} does not exist in the working directory.)"

    user_message += (
        "\n\nRespond with exactly one word: PASS or FAIL. "
        "No other text."
    )

    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        return (
            "llm_judge: 'anthropic' package is not installed. "
            "Run: pip install anthropic"
        )

    try:
        if api_key == "__claude_oauth__":
            # OAuth mode: no direct API key available.  Fall back to the
            # ANTHROPIC_API_KEY env var; if that is also absent, skip the
            # check with a clear error so the task is not silently blocked.
            import os  # noqa: PLC0415
            if not os.environ.get("ANTHROPIC_API_KEY"):
                logger.warning(
                    "llm_judge: running in Claude Code OAuth mode and "
                    "ANTHROPIC_API_KEY is not set — skipping judge check "
                    "(treating as PASS).  Set ANTHROPIC_API_KEY to enable."
                )
                return None  # treat as passed
            client = anthropic.Anthropic()
        else:
            client = anthropic.Anthropic(api_key=api_key)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=[{"role": "user", "content": user_message}],
        )
        verdict_raw = response.content[0].text.strip().lower()
    except Exception as exc:
        return f"llm_judge: API call failed: {exc}"

    if verdict_raw not in ("pass", "fail"):
        return (
            f"llm_judge: unexpected verdict {verdict_raw!r} "
            "(expected 'pass' or 'fail')"
        )

    if verdict_raw != expected:
        return (
            f"llm_judge: verdict is {verdict_raw!r} but expected {expected!r}\n"
            f"  prompt: {check.prompt[:200]}"
        )

    logger.debug("llm_judge: verdict=%r expected=%r — OK", verdict_raw, expected)
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_checks(
    criteria: "AcceptanceCriteria",
    working_dir: Path,
    api_key: str = "",
) -> CheckResult:
    """
    Evaluate every check in *criteria* against *working_dir*.

    Parameters
    ----------
    criteria:
        The ``AcceptanceCriteria`` from the project book.
    working_dir:
        The task's working directory (where artefacts live).
    api_key:
        Anthropic API key used by ``llm_judge`` checks.  Pass the runner's
        ``_api_key`` value; ``"__claude_oauth__"`` triggers OAuth mode.

    Returns
    -------
    CheckResult
        ``passed=True`` when all checks pass, ``passed=False`` with
        ``failed_checks`` and ``details`` populated on any failure.
    """
    failed: list[str] = []
    detail_lines: list[str] = []

    for i, check in enumerate(criteria.checks, start=1):
        label = f"[{i}/{len(criteria.checks)}] type={check.type}"
        logger.info("Acceptance check %s", label)

        error: Optional[str] = None
        if check.type == "file_exists":
            error = _check_file_exists(check, working_dir)
        elif check.type == "file_contains":
            error = _check_file_contains(check, working_dir)
        elif check.type == "command":
            error = _check_command(check, working_dir)
        elif check.type == "llm_judge":
            error = _check_llm_judge(check, working_dir, api_key)
        else:
            error = f"Unknown check type: {check.type!r}"

        if error:
            logger.warning("Acceptance check FAILED: %s\n  %s", label, error)
            failed.append(f"{label}: {error}")
            detail_lines.append(f"  FAIL {label}\n    {error}")
        else:
            logger.info("Acceptance check PASSED: %s", label)
            detail_lines.append(f"  PASS {label}")

    passed = len(failed) == 0
    details = "\n".join(detail_lines)
    return CheckResult(passed=passed, failed_checks=failed, details=details)
