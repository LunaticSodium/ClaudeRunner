# claude_runner/__init__.py
#
# Public surface of the claude-runner package.
#
# The installable CLI command is ``claude-runner`` (hyphen), which maps to
# the ``claude_runner`` Python package (underscore).  Keeping the two forms
# straight is important when referencing entry points in pyproject.toml and
# when users import the library programmatically.

"""claude-runner — Self-orchestrating Claude Code execution framework.

Typical programmatic usage::

    from claude_runner import VERSION
    from claude_runner.config import Config
    from claude_runner.project import load_project_book

    cfg = Config.load()
    book = load_project_book("projects/my-task.yaml")
"""

# ---------------------------------------------------------------------------
# Package version — single source of truth.
# pyproject.toml declares the same string; keep them in sync manually or via
# a release script (e.g. ``hatch version``).
# ---------------------------------------------------------------------------

VERSION: str = "0.1.0"

__version__ = VERSION
__all__ = ["VERSION"]
