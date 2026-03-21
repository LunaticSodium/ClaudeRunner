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
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import ConfigError

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
NotifyEvent = Literal[
    "start", "rate_limit", "resume", "complete", "error", "model_switch",
    "supervisor_accident", "intake_pass", "intake_partial", "intake_fail",
    "preflight_finding", "preflight_action", "kpi_warning", "intervention",
    "escalate_to_human",
]

# ---------------------------------------------------------------------------
# Model-schedule sub-models
# ---------------------------------------------------------------------------


class ModelAction(BaseModel):
    """The model to switch to when a rule fires.

    Attributes
    ----------
    model_id:
        Anthropic model identifier, e.g. ``"claude-haiku-4-5-20251001"``.
    message:
        Optional human-readable explanation logged when the switch fires.
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(..., description="Anthropic model ID to switch to.")
    message: str | None = Field(
        default=None,
        description="Optional log/notification message emitted when this action fires.",
    )


class Trigger(BaseModel):
    """Conditions that must ALL be satisfied for a PhaseRule to fire.

    Phase and token conditions are evaluated together (AND logic).  A rule
    with multiple Trigger entries fires when ANY trigger matches (OR logic).

    Attributes
    ----------
    phase_gte:
        Minimum phase number detected from git commit messages (inclusive).
    phase_lte:
        Maximum phase number (inclusive).
    token_pct_gte:
        Minimum context-window utilisation fraction (0.0 – 1.0, inclusive).
    token_pct_lte:
        Maximum context-window utilisation fraction (inclusive).
    """

    model_config = ConfigDict(extra="forbid")

    phase_gte: int | None = Field(default=None, ge=0, description="Phase >= this value.")
    phase_lte: int | None = Field(default=None, ge=0, description="Phase <= this value.")
    token_pct_gte: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Context utilisation >= this fraction.",
    )
    token_pct_lte: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Context utilisation <= this fraction.",
    )

    def matches(self, phase: int, token_pct: float) -> bool:
        """Return True when all non-None conditions are satisfied."""
        if self.phase_gte is not None and phase < self.phase_gte:
            return False
        if self.phase_lte is not None and phase > self.phase_lte:
            return False
        if self.token_pct_gte is not None and token_pct < self.token_pct_gte:
            return False
        if self.token_pct_lte is not None and token_pct > self.token_pct_lte:
            return False
        return True


class PhaseRule(BaseModel):
    """A single model-switch rule: fire *action* when any *trigger* matches.

    Attributes
    ----------
    triggers:
        List of :class:`Trigger` objects evaluated with OR logic.  The rule
        fires when at least one trigger matches the current phase/token state.
    action:
        The :class:`ModelAction` applied when the rule fires.
    """

    model_config = ConfigDict(extra="forbid")

    triggers: list[Trigger] = Field(
        ..., min_length=1, description="At least one trigger required."
    )
    action: ModelAction


class ModelSchedule(BaseModel):
    """Phase-aware model-switching schedule for a task.

    The runner's background ModelWatchdog evaluates these rules against the
    current phase (derived from ``PHASE-{N}:`` git commit prefixes) and context
    utilisation.  When a rule fires, the model is switched on the next Claude
    Code restart.

    Attributes
    ----------
    rules:
        Ordered list of :class:`PhaseRule` objects.  Each rule fires at most
        once per session.  Rules are evaluated in list order.
    poll_interval_seconds:
        How often (seconds) the watchdog polls git log for phase advances.
        Shorter intervals detect phase changes faster at the cost of more
        subprocess spawns.  Default: 15 s.
    """

    model_config = ConfigDict(extra="forbid")

    rules: list[PhaseRule] = Field(
        ..., min_length=1, description="At least one rule required."
    )
    poll_interval_seconds: float = Field(
        default=15.0,
        gt=0,
        description="Polling interval for git log phase detection (seconds).",
    )


# ---------------------------------------------------------------------------
# CCCS preset config
# ---------------------------------------------------------------------------


class CccsConfig(BaseModel):
    """Protocol selector: activates the *cccs* protocol on the feature machine.

    When present (and ``enabled`` is True) on a :class:`ProjectBook`, the runner
    loads the named preset from ``claude_runner/presets/<preset>.cccs.toml``,
    validates the project YAML against its schema, and injects a rendered
    CLAUDE.md fragment before the first ``claude -p`` call.

    Omitting this field (or setting ``enabled: false``) selects the *universal*
    protocol — no pre-session standards injection.

    Independent of the runway axis: works with both ``dash`` and ``marathon``.
    """

    enabled: bool = Field(
        default=True,
        description="Set to false to skip CCCS injection for this run.",
    )
    preset: str = Field(
        default="cccs-v1.0",
        description=(
            "Short name of the built-in preset file to load "
            "(resolved to claude_runner/presets/<preset>.cccs.toml)."
        ),
    )
    profile: str | None = Field(
        default=None,
        description=(
            "Tail profile to activate (e.g. 'scisim', 'engineering'). "
            "When None the preset's default_profile is used."
        ),
    )


# ---------------------------------------------------------------------------
# Supervisor protocol config
# ---------------------------------------------------------------------------


class SupervisorProtocolConfig(BaseModel):
    """Hardcoded behavioral layer for Marathon.

    Once enabled, all mechanisms are mandatory and cannot be disabled,
    overridden, or bypassed by Claude Code, any Dash agent, or any internal
    script.  Mutually exclusive with ``cccs``.

    v2.0 additions: supervisor_model, intervention limits, budget system.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    supervisor_model: str = Field(
        default="",
        description=(
            "Model ID for supervisor reasoning (intake, preflight, thinking manual). "
            "e.g. 'claude-opus-4-6'. If empty, resolved to best available at bootup. "
            "Supervisor thinks deeply but briefly — needs reasoning power, not token endurance."
        ),
    )
    self_check_limit: int = Field(default=10, ge=1)
    confirm_timeout_minutes: int = Field(default=5, ge=1)
    intervention_limit: int = Field(
        default=3, ge=1,
        description="Max autonomous interventions per worker before escalating to human.",
    )
    intervention_cooldown_min: int = Field(
        default=30, ge=1,
        description="Minimum minutes between interventions on the same worker.",
    )
    initial_budget_points: int = Field(
        default=10, ge=1,
        description="Starting accident point budget for the supervisor.",
    )
    audit_dir: str = "audit/"


# ---------------------------------------------------------------------------
# v2.0: Domain anchors and physics constraints
# ---------------------------------------------------------------------------


class DomainAnchor(BaseModel):
    """A published reference result for validation (Element 5).

    Declared in project book, compared against worker output during supervision.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., description="Paper/report citation.")
    configuration: str = Field(..., description="What was measured.")
    metric: str = Field(..., description="What the number represents.")
    value: float = Field(..., description="The numerical target.")
    unit: str = Field(..., description="Unit of measurement.")
    tolerance_pct: float = Field(
        default=50.0,
        description="How far off (%) before flagging.",
    )


class PhysicsConstraint(BaseModel):
    """A hard physical invariant that no output may violate (Element 6).

    Declared in project book — NO hardcoded domain constants in runner code.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Short identifier (e.g. 'positive_vpiL').")
    check: str = Field(
        ...,
        description="Expression from project book (e.g. '0 < vpiL < 100').",
    )
    message: str = Field(..., description="Explanation if violated.")


class IntakeSpec(BaseModel):
    """Optional section for intake validation hints (§8).

    All fields are optional — the LLM evaluates completeness against the
    preset file, not against this schema.  This just provides structured hints.
    """

    model_config = ConfigDict(extra="forbid")

    design_space_description: str | None = None
    objectives: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    output_spec: str | None = None
    domain_anchors: list[DomainAnchor] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# v2.0: Worker handle for multi-worker dispatch
# ---------------------------------------------------------------------------


class WorkerConfig(BaseModel):
    """Configuration for a single Dash worker dispatched by the supervisor."""

    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(..., description="Unique worker identifier (e.g. 'D1').")
    project_book_path: str = Field(..., description="Path to this worker's project book YAML.")
    working_dir: str = Field(..., description="Path to this worker's working directory.")
    model_id: str = Field(default="", description="Model for this worker (empty = default).")


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
    working_dir: Path | None = Field(
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
    allow_self_modification: bool = Field(
        default=False,
        description=(
            "Allow working_dir to resolve inside the claude-runner source tree. "
            "DANGEROUS: only effective when backend is also 'docker'. "
            "Without Docker the safety block in resolve_working_dir() fires regardless."
        ),
    )

    @field_validator("working_dir", mode="before")
    @classmethod
    def coerce_working_dir(cls, v: Any) -> Path | None:
        return Path(v) if v is not None else None

    @field_validator("working_dir")
    @classmethod
    def working_dir_must_be_dir(cls, v: Path | None) -> Path | None:
        """If working_dir is set, validate it is (or can become) a directory."""
        if v is None:
            return None
        if v.exists() and not v.is_dir():
            raise ValueError(
                f"sandbox.working_dir exists but is not a directory: {v!r}."
            )
        if not v.exists():
            v.mkdir(parents=True, exist_ok=True)
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
    silence_timeout_minutes: int | None = Field(
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
        Whether to ``git push`` after the commit.  When ``remote_url`` is
        set the runner configures the remote automatically; otherwise the
        working directory must already have ``origin`` configured.
    remote_url:
        HTTPS remote URL to push to, e.g.
        ``https://github.com/org/repo.git``.  When set the runner will
        ``git init`` if needed, add/update ``origin`` to this URL, and
        inject ``git_token`` from config for authentication.  Credentials
        are passed via the URL (``https://<token>@host/...``) so they
        never touch disk outside the runner process.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    branch_prefix: str = "claude-task/"
    auto_push: bool = False
    remote_url: str | None = None


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
    def validate_channel_fields(self) -> NotifyChannel:
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
    def channels_required_when_events_set(self) -> NotifyConfig:
        if self.on and not self.channels:
            logger.warning(
                "notify.on lists events %s but no channels are configured — "
                "notifications will be silently dropped.",
                self.on,
            )
        return self


# ---------------------------------------------------------------------------
# Implementation constraints sub-models
# ---------------------------------------------------------------------------


class ConstraintVerifyBackend(str, Enum):
    """Backend used to verify an implementation constraint."""

    file_contains = "file_contains"
    llm_judge = "llm_judge"


class ImplementationConstraint(BaseModel):
    """A verifiable algorithmic requirement for the task output.

    Attributes
    ----------
    id:
        Short unique identifier for this constraint (e.g. ``"use-redis"``).
    description:
        Human-readable description injected into the initial prompt and
        visible in reports.
    verify_with:
        Verification backend.  ``file_contains`` runs a regex/grep search;
        ``llm_judge`` asks a lightweight model to confirm compliance.
    file:
        Relative path (from working directory) of the file to inspect.
        Required for ``file_contains``; optional for ``llm_judge``.
    pattern:
        Python regex pattern searched in *file*.  Required for
        ``file_contains``.
    prompt:
        Instruction sent to the LLM judge.  Required for ``llm_judge``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    verify_with: ConstraintVerifyBackend
    # file_contains fields
    file: str | None = None
    pattern: str | None = None
    # llm_judge fields
    prompt: str | None = None


# ---------------------------------------------------------------------------
# Preflight sub-model
# ---------------------------------------------------------------------------


class PreflightConfig(BaseModel):
    """Optional pre-flight checks run before Claude Code subprocess is spawned.

    Attributes
    ----------
    required_env:
        List of environment variable names that must be set.  A missing
        variable causes a hard ``PreflightError`` before Claude is launched.
    skip:
        When ``True`` all preflight checks are skipped for this project book.
        Equivalent to passing ``--skip-preflight`` on the CLI.
    """

    model_config = ConfigDict(extra="forbid")

    required_env: list[str] = Field(default_factory=list)
    skip: bool = False


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
    path: str | None = None
    pattern: str | None = None
    run: str | None = None
    expect_exit: int | None = 0
    prompt: str | None = None
    expect: Literal["pass", "fail"] | None = "pass"


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
    marathon_mode: bool = Field(
        default=False,
        description=(
            "Runway selector.  False = 'dash' runway: phase-aware model switching active, "
            "ModelWatchdog polls git log and switches models on PHASE-N: triggers.  "
            "True = 'marathon' runway: single model for the entire session, no watchdog, "
            "no mid-session switches — designed for long unattended runs that must survive "
            "server restarts and API outages without intervention.  "
            "Independent of the protocol axis (cccs / universal)."
        ),
    )
    model_schedule: ModelSchedule | None = Field(
        default=None,
        description=(
            "Phase-aware model-switching schedule (dash runway only).  When set and "
            "marathon_mode is False, the runner injects the phase contract into CLAUDE.md "
            "and starts a background ModelWatchdog that switches models based on git commit "
            "phase markers and context utilisation triggers.  Ignored on marathon runway."
        ),
    )
    cccs: CccsConfig | None = Field(
        default=None,
        description=(
            "Protocol selector.  None = 'universal' protocol: no pre-session standards "
            "injection.  When set = 'cccs' protocol: loads the named .cccs.toml preset, "
            "validates the project YAML against its schema, and injects a rendered CLAUDE.md "
            "fragment before the first claude invocation.  "
            "Independent of the runway axis (dash / marathon).  "
            "Set enabled: false to temporarily revert to universal without removing the block."
        ),
    )
    sandbox: SandboxConfig | None = Field(
        default=None,
        description="Sandbox configuration (omit for native/unsandboxed execution).",
    )
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    acceptance_criteria: AcceptanceCriteria | None = Field(
        default=None,
        description=(
            "Optional post-completion acceptance gate.  When set, claude-runner "
            "evaluates all checks after ##RUNNER:COMPLETE## is detected.  "
            "On failure the task is retried, notified, or failed per on_failure."
        ),
    )
    preflight: PreflightConfig | None = Field(
        default=None,
        description=(
            "Optional pre-flight checks evaluated before Claude Code is spawned.  "
            "Fails hard on missing required_env variables; warns on other issues."
        ),
    )
    implementation_constraints: list[ImplementationConstraint] = Field(
        default_factory=list,
        description=(
            "Verifiable algorithmic requirements injected into the initial prompt and "
            "verified automatically after acceptance checks complete."
        ),
    )
    supervisor_protocol: SupervisorProtocolConfig = Field(
        default_factory=SupervisorProtocolConfig,
        description=(
            "Supervisor protocol configuration.  When enabled=true, activates the "
            "hardcoded marathon behavioral layer.  Mutually exclusive with cccs."
        ),
    )
    intake: IntakeSpec | None = Field(
        default=None,
        description=(
            "Optional intake validation hints (§8). Domain anchors, objectives, "
            "constraints, design space description. LLM evaluates project book "
            "against the preset file using these as structured hints."
        ),
    )
    physics_constraints: list[PhysicsConstraint] = Field(
        default_factory=list,
        description=(
            "Hard physical invariants that no output may violate (Element 6). "
            "Declared here, not hardcoded in the runner. The runner provides the "
            "evaluation engine; the project book provides the constraints."
        ),
    )

    @model_validator(mode="after")
    def validate_supervisor_cccs_exclusive(self) -> ProjectBook:
        """Raise ConfigError if both supervisor_protocol and cccs are enabled."""
        cccs_enabled = self.cccs is not None and self.cccs.enabled
        sp_enabled = self.supervisor_protocol.enabled
        if sp_enabled and cccs_enabled:
            raise ConfigError(
                "supervisor_protocol and cccs cannot both be enabled — "
                "they are mutually exclusive protocol layers."
            )
        return self

    @model_validator(mode="after")
    def resolve_skip_permissions(self) -> ProjectBook:
        """Resolve skip_permissions to a concrete bool based on sandbox mode.

        - Docker sandbox present → default True (safe inside container)
        - Native / no sandbox   → default False (requires explicit opt-in)
        - Explicit value in YAML → respected as-is regardless of sandbox mode
        """
        if self.execution.skip_permissions is None:
            self.execution.skip_permissions = self.sandbox is not None
        return self

    @model_validator(mode="after")
    def warn_skip_permissions_without_docker(self) -> ProjectBook:
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
    def from_yaml(cls, path) -> ProjectBook:
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
