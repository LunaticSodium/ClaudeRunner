# claude_runner/project.py
#
# Pydantic v2 schema for claude-runner "project books".
#
# A project book is a YAML file that fully describes a long-running Claude
# Code task: what to do, where to run it, how to sandbox it, how long to
# wait, and how to notify stakeholders at key lifecycle events.
#
# All models use ``extra='forbid'`` so that typos in field names surface
# immediately as validation errors rather than silently being ignored.
#
# Usage
# -----
#   from claude_runner.project import load_project_book
#   book = load_project_book(Path("projects/refactor-auth.yaml"))

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Custom YAML loader — YAML 1.2-style booleans only
#
# PyYAML's default SafeLoader follows YAML 1.1, where bare words like
# "on", "off", "yes", "no" are parsed as booleans.  That breaks the common
# project-book pattern ``notify:\n  on: [...]`` because the key ``on``
# becomes Python True.
#
# We replace the built-in bool resolver with one that only matches the
# YAML 1.2 canonical forms: true / True / TRUE and false / False / FALSE.
# Everything else (on, off, yes, no, …) stays as a plain string.
# ---------------------------------------------------------------------------

class _Yaml12Loader(yaml.SafeLoader):
    pass


# Remove the inherited YAML-1.1 boolean resolver and register a YAML-1.2 one.
_Yaml12Loader.yaml_implicit_resolvers = {
    k: [(tag, regexp) for tag, regexp in resolvers
        if tag != "tag:yaml.org,2002:bool"]
    for k, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

ResumeStrategy = Literal["continue", "restate", "summarize"]
NotifyEvent = Literal["start", "rate_limit", "resume", "complete", "error"]

# ---------------------------------------------------------------------------
# Sandbox sub-models
# ---------------------------------------------------------------------------


class ReadonlyMount(BaseModel):
    """A host path exposed inside the container as a read-only bind mount.

    Attributes
    ----------
    path:
        Absolute path on the *host* filesystem that should be mounted.
    mount_as:
        Absolute path inside the container where the mount will appear.
        Typically a POSIX path such as ``/ref/api-specs``.
    """

    model_config = ConfigDict(extra="forbid")

    path: Path
    mount_as: str = Field(
        ...,
        description="Absolute container path where this directory will be mounted read-only.",
    )

    @field_validator("path", mode="before")
    @classmethod
    def coerce_path(cls, v: Any) -> Path:
        return Path(v)


class NetworkConfig(BaseModel):
    """Egress firewall rules for the task's sandbox container.

    Attributes
    ----------
    allow:
        Explicit list of hostnames or IP addresses permitted outbound.
        Wildcards are not supported; use exact hostnames.
    deny_all_others:
        When ``True``, all egress not in *allow* is blocked (strict mode).
        When ``False`` (default), the container has full outbound access so
        Claude Code can reach api.anthropic.com and package registries.
    """

    model_config = ConfigDict(extra="forbid")

    allow: list[str] = Field(default_factory=list)
    deny_all_others: bool = False


class SandboxConfig(BaseModel):
    """Sandbox / container configuration for the task.

    Attributes
    ----------
    backend:
        Sandbox backend to use for this task.  ``"docker"`` requires Docker
        Desktop to be running.  ``"native"`` runs Claude Code directly on the
        host (no container).  ``"auto"`` (default) picks Docker when available
        and falls back to native.
    working_dir:
        The directory on the *host* that will be bind-mounted as the working
        directory inside the container.  Must exist at the time the project
        book is loaded.
    readonly_mounts:
        Additional host paths mounted read-only inside the container.
        Useful for reference material (API specs, documentation) that Claude
        should be able to read but not modify.
    network:
        Egress firewall configuration.  Defaults to full outbound access
        (``deny_all_others=False``).  Set ``deny_all_others: true`` with an
        explicit ``allow`` list for strict network isolation.
    env:
        Additional environment variables injected into the container.
        ``ANTHROPIC_API_KEY`` and ``GIT_TOKEN`` are managed automatically
        and cannot be overridden here.
    """

    model_config = ConfigDict(extra="forbid")

    backend: Literal["auto", "docker", "native"] = Field(
        default="auto",
        description=(
            "Sandbox backend: 'docker' (requires Docker Desktop), "
            "'native' (runs on host, no container), or 'auto' (docker if available)."
        ),
    )
    working_dir: Optional[Path] = Field(
        default=None,
        description=(
            "Host-side working directory for the task.  When omitted, claude-runner "
            "automatically uses a sibling folder named after the YAML file "
            "(e.g. my-task.yaml → ./my-task/).  The folder is created if it does not exist."
        ),
    )
    readonly_mounts: list[ReadonlyMount] = Field(default_factory=list)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Additional environment variables injected into the sandbox container. "
            "These are merged on top of the system defaults; they cannot override "
            "ANTHROPIC_API_KEY or GIT_TOKEN which are managed by claude-runner."
        ),
    )

    @field_validator("working_dir", mode="before")
    @classmethod
    def coerce_working_dir(cls, v: Any) -> Optional[Path]:
        return Path(v) if v is not None else None

    @field_validator("working_dir")
    @classmethod
    def working_dir_must_be_dir(cls, v: Optional[Path]) -> Optional[Path]:
        """If working_dir is set, validate it is (or can become) a directory."""
        if v is None:
            return None
        if v.exists() and not v.is_dir():
            raise ValueError(
                f"sandbox.working_dir exists but is not a directory: {v!r}."
            )
        return v


# ---------------------------------------------------------------------------
# Milestone model
# ---------------------------------------------------------------------------


class Milestone(BaseModel):
    """A detectable progress event in the task output.

    When claude-runner's output scanner matches *pattern* in Claude's stdout
    stream, it emits a notification with *message* and records the milestone
    in the task's state file.

    Attributes
    ----------
    pattern:
        A plain substring (or simple regex) that must appear in Claude's
        output for this milestone to trigger.
    message:
        Human-readable summary emitted in notifications and logs when the
        milestone fires.
    """

    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(..., description="Substring or regex to match in Claude's output.")
    message: str = Field(..., description="Human-readable label for this milestone.")


# ---------------------------------------------------------------------------
# Context-window management
# ---------------------------------------------------------------------------


class ContextConfig(BaseModel):
    """Controls how claude-runner manages Claude's context window.

    Attributes
    ----------
    checkpoint_threshold_tokens:
        Estimated token count at which claude-runner should write a
        checkpoint and (optionally) summarise prior work before continuing.
        A lower value increases checkpoint frequency and reduces the risk of
        hitting the model's hard context limit unexpectedly.
    reset_on_rate_limit:
        When ``True``, treat a rate-limit response as a signal to checkpoint
        and optionally compress the context before retrying.
    inject_log_on_resume:
        When ``True``, prepend a structured summary of prior progress (from
        the state file) at the top of the context window when resuming after
        an interruption.
    """

    model_config = ConfigDict(extra="forbid")

    checkpoint_threshold_tokens: int = Field(
        default=150_000,
        gt=0,
        description="Token budget before a forced context checkpoint.",
    )
    reset_on_rate_limit: bool = True
    inject_log_on_resume: bool = True


# ---------------------------------------------------------------------------
# Execution sub-model
# ---------------------------------------------------------------------------


class ExecutionConfig(BaseModel):
    """Runtime execution parameters for the task.

    Attributes
    ----------
    timeout_hours:
        Hard wall-clock limit for the entire task, in hours.  claude-runner
        will send SIGTERM to Claude when this limit is reached and mark the
        task as timed-out.  Use 0 to disable (not recommended for unattended
        runs).
    max_rate_limit_waits:
        Maximum number of consecutive Anthropic rate-limit (429) responses
        to tolerate before the task is aborted.  Overrides the global
        config default for this specific task.
    resume_strategy:
        How to continue after an interruption.  See ``ResumeStrategy``.
    skip_permissions:
        Pass ``--dangerously-skip-permissions`` to Claude Code.  Allows the
        model to execute filesystem and shell actions without interactive
        approval prompts.  **Only safe inside a Docker sandbox.**
        When left unset (``None``), the value is resolved by the parent
        ``ProjectBook`` validator: ``True`` if a Docker sandbox is configured,
        ``False`` otherwise.  Set explicitly to override the default.
    context:
        Context-window management settings.
    milestones:
        Ordered list of detectable progress markers.
    """

    model_config = ConfigDict(extra="forbid")

    timeout_hours: float = Field(
        default=12.0,
        ge=0,
        description="Hard wall-clock limit in hours (0 = unlimited).",
    )
    max_rate_limit_waits: int = Field(
        default=20,
        ge=0,
        description="Consecutive rate-limit responses before task is aborted.",
    )
    resume_strategy: ResumeStrategy = "continue"
    skip_permissions: bool | None = Field(
        default=None,
        description=(
            "Pass --dangerously-skip-permissions to Claude Code. "
            "Defaults to True when a Docker sandbox is configured, False otherwise. "
            "Set explicitly to override."
        ),
    )
    context: ContextConfig = Field(default_factory=ContextConfig)
    milestones: list[Milestone] = Field(default_factory=list)
    silence_timeout_minutes: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Minutes of output silence before the runner sends a 'continue' probe "
            "and, if still silent, declares a hung-process error. "
            "Defaults to 5 minutes when not set."
        ),
    )


# ---------------------------------------------------------------------------
# Output / Git sub-models
# ---------------------------------------------------------------------------


class GitOutputConfig(BaseModel):
    """Automatic git commit / push behaviour after task completion.

    Attributes
    ----------
    enabled:
        Master switch.  When ``False`` all other git settings are ignored.
    branch_prefix:
        Prefix for auto-created branches.  The task name (slugified) is
        appended: ``claude-task/refactor-authentication-module``.
    auto_push:
        Whether to ``git push`` after the commit.  Requires the working
        directory to have a remote configured.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    branch_prefix: str = "claude-task/"
    auto_push: bool = False


class OutputConfig(BaseModel):
    """Controls what happens to task artefacts after completion.

    Attributes
    ----------
    git:
        Git commit/push settings.
    log_dir:
        Override the global ``log_dir`` for this specific task.
        Useful when project books are version-controlled alongside source
        code and logs should live in the project tree.
    """

    model_config = ConfigDict(extra="forbid")

    git: GitOutputConfig = Field(default_factory=GitOutputConfig)
    log_dir: Path | None = Field(
        default=None,
        description="Per-task log directory (overrides global config).",
    )

    @field_validator("log_dir", mode="before")
    @classmethod
    def coerce_log_dir(cls, v: Any) -> Path | None:
        return Path(v) if v is not None else None


# ---------------------------------------------------------------------------
# Notification sub-models
# ---------------------------------------------------------------------------


class NotifyChannel(BaseModel):
    """A single notification destination.

    The ``type`` field is a discriminator; the remaining fields are specific
    to each channel type and are passed through to the Apprise backend.

    Supported types
    ---------------
    ``email``
        Requires ``to`` (recipient address).  SMTP settings are read from
        the global config or environment variables.
    ``desktop``
        Uses the OS notification system (Windows toast on Windows, libnotify
        on Linux).  No additional fields required.
    ``webhook``
        HTTP POST to ``url`` with a JSON payload describing the event.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["email", "desktop", "webhook"]
    # email fields
    to: str | None = Field(default=None, description="Recipient address for email channels.")
    # webhook fields
    url: str | None = Field(default=None, description="Endpoint URL for webhook channels.")

    @model_validator(mode="after")
    def validate_channel_fields(self) -> "NotifyChannel":
        if self.type == "email" and not self.to:
            raise ValueError("notify channel of type 'email' requires a 'to' address.")
        if self.type == "webhook" and not self.url:
            raise ValueError("notify channel of type 'webhook' requires a 'url'.")
        return self


class NotifyConfig(BaseModel):
    """Notification configuration for the task lifecycle.

    Attributes
    ----------
    on:
        List of lifecycle events that trigger notifications.
        Valid values: ``start``, ``rate_limit``, ``resume``, ``complete``,
        ``error``.
    channels:
        List of notification destinations.  At least one channel is
        required if *on* is non-empty.
    """

    model_config = ConfigDict(extra="forbid")

    on: list[NotifyEvent] = Field(
        default_factory=list,
        description="Lifecycle events that trigger notifications.",
    )
    channels: list[NotifyChannel] = Field(default_factory=list)

    @model_validator(mode="after")
    def channels_required_when_events_set(self) -> "NotifyConfig":
        if self.on and not self.channels:
            logger.warning(
                "notify.on lists events %s but no channels are configured — "
                "notifications will be silently dropped.",
                self.on,
            )
        return self


# ---------------------------------------------------------------------------
# Acceptance criteria sub-models
# ---------------------------------------------------------------------------


class AcceptanceCheck(BaseModel):
    """A single acceptance criterion evaluated after task completion.

    Attributes
    ----------
    type:
        ``file_exists``  — assert a path exists inside the working directory.
        ``file_contains`` — assert a file's text matches *pattern* (regex).
        ``command``      — run a shell command; assert its exit code.
        ``llm_judge``    — ask the model to evaluate *prompt*; assert "pass".
    path:
        Relative path (from working directory) used by ``file_exists`` and
        ``file_contains`` checks.
    pattern:
        Python regex searched inside the file for ``file_contains`` checks.
    run:
        Shell command string executed for ``command`` checks.
    expect_exit:
        Expected exit code for ``command`` checks (default 0).
    prompt:
        Instruction sent to the LLM judge.  The relevant file contents are
        appended automatically when *path* is also set.
    expect:
        Expected LLM verdict for ``llm_judge`` checks: ``"pass"`` (default)
        or ``"fail"``.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["file_exists", "file_contains", "command", "llm_judge"]
    path: Optional[str] = None
    pattern: Optional[str] = None
    run: Optional[str] = None
    expect_exit: Optional[int] = 0
    prompt: Optional[str] = None
    expect: Optional[Literal["pass", "fail"]] = "pass"


class AcceptanceCriteria(BaseModel):
    """Post-completion acceptance gate for a claude-runner task.

    Attributes
    ----------
    checks:
        Ordered list of acceptance checks.  All must pass for the gate to
        succeed.  Execution stops at the first failure.
    on_failure:
        Action taken when one or more checks fail:
        ``"retry"``  — re-run Claude Code with a correction prompt (up to
                       *max_retries* times).
        ``"notify"`` — dispatch an error notification but do not retry.
        ``"fail"``   — mark the task as failed immediately (no notification
                       beyond the standard error event).
    max_retries:
        Maximum number of retry attempts when *on_failure* is ``"retry"``.
        Ignored for other *on_failure* values.
    """

    model_config = ConfigDict(extra="forbid")

    checks: list[AcceptanceCheck] = Field(default_factory=list)
    on_failure: Literal["retry", "notify", "fail"] = "fail"
    max_retries: int = Field(default=1, ge=0)


# ---------------------------------------------------------------------------
# Top-level ProjectBook
# ---------------------------------------------------------------------------


class ProjectBook(BaseModel):
    """Complete specification for a single claude-runner task.

    A project book is typically stored as a YAML file and loaded via
    :func:`load_project_book`.  All fields map directly to the YAML keys
    shown in the spec.

    Attributes
    ----------
    name:
        Short human-readable task name.  Used in log file names, git branch
        names, and notification subjects.
    description:
        Multi-line plain-text description.  Displayed in the TUI header and
        included in email notifications.
    prompt:
        The full task instruction sent to Claude Code.  May be several
        paragraphs long.  Newlines and indentation are preserved.
    sandbox:
        Sandbox / container configuration.  Optional; when omitted, the
        ``native`` backend is implied (no container isolation).
    execution:
        Runtime execution parameters.
    output:
        Artefact output configuration (git, log directory).
    notify:
        Notification configuration.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="Short task name.")
    description: str = Field(default="", description="Multi-line task description.")
    prompt: str = Field(..., min_length=1, description="Full task prompt sent to Claude Code.")
    context_anchors: str | None = Field(
        default=None,
        description=(
            "Optional persistent instructions prepended verbatim to every prompt "
            "sent to Claude Code: the initial prompt, every resume injection, and "
            "every context checkpoint prompt.  Write once here; claude-runner "
            "injects silently.  Does not appear in notifications or email content."
        ),
    )
    sandbox: SandboxConfig | None = Field(
        default=None,
        description="Sandbox configuration (omit for native/unsandboxed execution).",
    )
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    acceptance_criteria: Optional[AcceptanceCriteria] = Field(
        default=None,
        description=(
            "Optional post-completion acceptance gate.  When set, claude-runner "
            "evaluates all checks after ##RUNNER:COMPLETE## is detected.  "
            "On failure the task is retried, notified, or failed per on_failure."
        ),
    )

    @model_validator(mode="after")
    def resolve_skip_permissions(self) -> "ProjectBook":
        """Resolve skip_permissions to a concrete bool based on sandbox mode.

        - Docker sandbox present → default True (safe inside container)
        - Native / no sandbox   → default False (requires explicit opt-in)
        - Explicit value in YAML → respected as-is regardless of sandbox mode
        """
        if self.execution.skip_permissions is None:
            self.execution.skip_permissions = self.sandbox is not None
        return self

    @model_validator(mode="after")
    def warn_skip_permissions_without_docker(self) -> "ProjectBook":
        """Emit a log warning (not an error) when skip_permissions is enabled
        without the Docker sandbox, because this grants Claude unrestricted
        access to the host filesystem without any containment boundary."""
        if self.execution.skip_permissions and self.sandbox is None:
            logger.warning(
                "Project book %r has execution.skip_permissions=true but no sandbox "
                "is configured.  Claude Code will run with unrestricted host access.  "
                "Consider adding a sandbox block with backend='docker'.",
                self.name,
            )
        return self

    @classmethod
    def from_yaml(cls, path) -> "ProjectBook":
        """Load and validate a project book from a YAML file.

        Convenience classmethod that delegates to :func:`load_project_book`.
        """
        return load_project_book(Path(path))


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_project_book(path: Path) -> ProjectBook:
    """Load and validate a project book YAML file.

    Parameters
    ----------
    path:
        Absolute or relative path to the ``.yaml`` project book file.

    Returns
    -------
    ProjectBook
        A fully-validated project book model.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    yaml.YAMLError
        If the file is not valid YAML.
    pydantic.ValidationError
        If the YAML structure does not conform to the :class:`ProjectBook`
        schema (unknown fields, wrong types, failed validators, etc.).

    Examples
    --------
    >>> book = load_project_book(Path("projects/refactor-auth.yaml"))
    >>> print(book.name)
    Refactor authentication module
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Project book not found: {path!r}")

    logger.debug("Loading project book from %s", path)

    with path.open("r", encoding="utf-8") as fh:
        content = fh.read()

    # Handle multi-document YAML (files with --- separators, e.g. examples.yaml).
    # Load all documents and take the first; warn when more are present so the
    # user knows to copy a single document to its own file to run the others.
    all_docs: list[Any] = [
        d for d in yaml.load_all(content, Loader=_Yaml12Loader) if d is not None  # noqa: S506
    ]
    if not all_docs:
        raw = {}
    elif len(all_docs) > 1:
        logger.warning(
            "%s contains %d YAML documents — loading the first one only. "
            "Copy the document you want to run into its own .yaml file.",
            path.name,
            len(all_docs),
        )
        raw = all_docs[0]
    else:
        raw = all_docs[0]

    if not isinstance(raw, dict):
        raise yaml.YAMLError(
            f"Expected a YAML mapping at the top level of {path}, "
            f"got {type(raw).__name__}."
        )

    # Pydantic v2 validation — raises pydantic.ValidationError on failure.
    book = ProjectBook.model_validate(raw)

    logger.info("Loaded project book %r from %s", book.name, path)
    return book
