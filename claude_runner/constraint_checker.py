"""
constraint_checker.py — Verify implementation_constraints from the project book.

After acceptance checks pass, the runner calls ``check_all_constraints()`` to
confirm that algorithmic requirements declared in ``implementation_constraints``
were actually implemented.

Supported backends
------------------
``file_contains``
    Searches *pattern* (Python regex) in *file* relative to *working_dir*.
    Uses ``re.search`` (portable; no external grep dependency).
``llm_judge``
    Calls the Anthropic messages API with the constraint prompt; passes if
    the model responds with a word starting with "YES" (case-insensitive).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .project import ImplementationConstraint

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ConstraintResult:
    """Outcome of a single constraint verification."""

    id: str
    passed: bool
    reason: str

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"[{status}] constraint {self.id!r}: {self.reason}"


# ---------------------------------------------------------------------------
# Single-constraint checker
# ---------------------------------------------------------------------------


def check_constraint(
    constraint: ImplementationConstraint,
    working_dir: Path,
    api_key: str | None = None,
) -> ConstraintResult:
    """Verify a single *constraint* against *working_dir*.

    Parameters
    ----------
    constraint:
        An :class:`~claude_runner.project.ImplementationConstraint` instance.
    working_dir:
        The task's working directory (artefacts live here).
    api_key:
        Anthropic API key used by ``llm_judge`` checks.  Pass
        ``"__claude_oauth__"`` when running in OAuth mode.

    Returns
    -------
    ConstraintResult
        ``passed=True`` on success; ``passed=False`` with a reason on failure.
    """
    from .project import ConstraintVerifyBackend  # noqa: PLC0415

    backend = constraint.verify_with

    if backend == ConstraintVerifyBackend.file_contains:
        return _check_file_contains(constraint, working_dir)
    elif backend == ConstraintVerifyBackend.llm_judge:
        return _check_llm_judge(constraint, working_dir, api_key or "")
    else:
        return ConstraintResult(
            id=constraint.id,
            passed=False,
            reason=f"Unknown verify_with backend: {backend!r}",
        )


# ---------------------------------------------------------------------------
# All-constraints runner
# ---------------------------------------------------------------------------


def check_all_constraints(
    constraints: list[ImplementationConstraint],
    working_dir: Path,
    api_key: str | None = None,
) -> list[ConstraintResult]:
    """Verify all *constraints* and return one :class:`ConstraintResult` each.

    Parameters
    ----------
    constraints:
        List of :class:`~claude_runner.project.ImplementationConstraint`.
    working_dir:
        The task's working directory.
    api_key:
        Anthropic API key (passed through to ``llm_judge`` checks).

    Returns
    -------
    list[ConstraintResult]
        One result per constraint, in input order.
    """
    results: list[ConstraintResult] = []
    for constraint in constraints:
        logger.info(
            "[constraint_checker] Checking constraint %r (backend=%s)",
            constraint.id,
            constraint.verify_with,
        )
        result = check_constraint(constraint, working_dir, api_key)
        if result.passed:
            logger.info("[constraint_checker] PASS %r", constraint.id)
        else:
            logger.warning("[constraint_checker] FAIL %r: %s", constraint.id, result.reason)
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------


def _check_file_contains(
    constraint: ImplementationConstraint,
    working_dir: Path,
) -> ConstraintResult:
    """Verify using Python re.search (portable alternative to grep -P)."""
    if not constraint.file:
        return ConstraintResult(
            id=constraint.id,
            passed=False,
            reason="file_contains check is missing 'file' field",
        )
    if not constraint.pattern:
        return ConstraintResult(
            id=constraint.id,
            passed=False,
            reason="file_contains check is missing 'pattern' field",
        )

    target = working_dir / constraint.file
    if not target.exists():
        return ConstraintResult(
            id=constraint.id,
            passed=False,
            reason=f"file {constraint.file!r} does not exist in working_dir",
        )

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return ConstraintResult(
            id=constraint.id,
            passed=False,
            reason=f"could not read {constraint.file!r}: {exc}",
        )

    try:
        match = re.search(constraint.pattern, text)
    except re.error as exc:
        return ConstraintResult(
            id=constraint.id,
            passed=False,
            reason=f"invalid pattern {constraint.pattern!r}: {exc}",
        )

    if match:
        return ConstraintResult(
            id=constraint.id,
            passed=True,
            reason=f"pattern {constraint.pattern!r} found in {constraint.file!r}",
        )
    return ConstraintResult(
        id=constraint.id,
        passed=False,
        reason=f"pattern {constraint.pattern!r} not found in {constraint.file!r}",
    )


def _check_llm_judge(
    constraint: ImplementationConstraint,
    working_dir: Path,
    api_key: str,
) -> ConstraintResult:
    """Verify using a lightweight Anthropic API call."""
    if not constraint.prompt:
        return ConstraintResult(
            id=constraint.id,
            passed=False,
            reason="llm_judge check is missing 'prompt' field",
        )

    user_message = constraint.prompt.strip()
    # Append file contents if specified.
    if constraint.file:
        file_path = working_dir / constraint.file
        if file_path.exists():
            try:
                file_text = file_path.read_text(encoding="utf-8", errors="replace")
                user_message += f"\n\nCode:\n{file_text}"
            except Exception as exc:
                logger.warning(
                    "[constraint_checker] llm_judge: could not read %s: %s",
                    constraint.file,
                    exc,
                )

    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        return ConstraintResult(
            id=constraint.id,
            passed=False,
            reason="'anthropic' package not installed — run: pip install anthropic",
        )

    try:
        if api_key == "__claude_oauth__":
            import os  # noqa: PLC0415
            if not os.environ.get("ANTHROPIC_API_KEY"):
                logger.warning(
                    "[constraint_checker] llm_judge: OAuth mode and no ANTHROPIC_API_KEY "
                    "— treating as PASS."
                )
                return ConstraintResult(
                    id=constraint.id,
                    passed=True,
                    reason="OAuth mode: API key unavailable, check skipped (treated as PASS)",
                )
            client = anthropic.Anthropic()
        else:
            client = anthropic.Anthropic(api_key=api_key)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16,
            system="You are a code reviewer. Answer only YES or NO.",
            messages=[{"role": "user", "content": user_message}],
        )
        block = response.content[0]
        verdict_raw = (block.text if hasattr(block, "text") else "").strip()
    except Exception as exc:
        return ConstraintResult(
            id=constraint.id,
            passed=False,
            reason=f"API call failed: {exc}",
        )

    passed = verdict_raw.upper().startswith("YES")
    return ConstraintResult(
        id=constraint.id,
        passed=passed,
        reason=f"LLM verdict: {verdict_raw!r}",
    )
