"""
claude_runner/worker_supervisor.py

Worker Performance Supervision (§10) — active monitoring and intervention.

The supervisor's own performance is judged by how well its workers collectively
perform. It is structurally motivated to find and act on underperformance.

Intervention levels (applied in order):
  1. Re-describe — rewrite worker's project YAML with clearer target
  2. Split — decompose stalled task into two smaller project YAMLs
  3. Restart + reconfig — kill and relaunch with adjusted parameters

Constraints:
  - Process resource gate (F4): check CPU/mem before ANY intervention
  - Intervention limit: 3 per worker before human escalation
  - Cooldown: 30 min between interventions on same worker
  - Cause-aware: must diagnose WHY before selecting intervention
  - Budget: interventions that fail cost points (F2)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .kpi_collector import KPIAssessment, KPICollector, WorkerMetrics
from .supervisor_protocol import (
    SupervisorBudget,
    check_worker_process_alive,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Diagnosis:
    """Root-cause diagnosis for worker underperformance."""

    worker_id: str
    symptom: str                # what the KPI shows
    probable_cause: str         # why (must be stated explicitly)
    confidence: float = 0.5    # 0-1
    recommended_level: int = 1  # 1, 2, or 3
    reasoning: str = ""         # why this level, not another


@dataclass
class InterventionResult:
    """Result of an intervention attempt."""

    worker_id: str
    level: int
    cause: str
    action_taken: str
    success: bool = True
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class WorkerRecord:
    """Tracks intervention history for a single worker."""

    worker_id: str
    interventions: list[InterventionResult] = field(default_factory=list)
    last_intervention_at: float | None = None  # monotonic time
    process_pid: int | None = None

    @property
    def intervention_count(self) -> int:
        return len(self.interventions)


# Cause → intervention level mapping
CAUSE_INTERVENTION_MAP: dict[str, int] = {
    "unclear_requirements": 1,  # Worker may be capable but confused
    "task_too_large": 2,        # Worker making progress but too slowly
    "wrong_model": 3,           # Fundamental mismatch
    "rate_limited": 0,          # External constraint — wait, don't intervene
    "environment_issue": 0,     # Not a worker problem
    "stuck_in_loop": 3,         # Context poisoned
    "unknown": 1,               # Default to least disruptive
}


class WorkerSupervisor:
    """Active worker performance monitoring and intervention system.

    This is where the adversarial incentive lives: the supervisor's own
    performance is judged by how well its workers collectively perform.

    Parameters
    ----------
    config:
        SupervisorProtocolConfig from project book.
    budget:
        SupervisorBudget for point tracking.
    audit_dir:
        Directory for audit logs.
    ntfy_client:
        For publishing notifications. May be None.
    """

    def __init__(
        self,
        config,
        budget: SupervisorBudget,
        audit_dir: Path,
        ntfy_client=None,
    ) -> None:
        self._config = config
        self._budget = budget
        self._audit_dir = Path(audit_dir)
        self._ntfy = ntfy_client
        self._kpi = KPICollector()
        self._workers: dict[str, WorkerRecord] = {}
        self._intervention_limit = getattr(config, "intervention_limit", 3)
        self._cooldown_min = getattr(config, "intervention_cooldown_min", 30)

    def register_worker(self, worker_id: str, pid: int | None = None) -> None:
        """Register a new worker for supervision."""
        self._workers[worker_id] = WorkerRecord(
            worker_id=worker_id,
            process_pid=pid,
        )

    def assess_kpi(
        self,
        worker_id: str,
        metrics: WorkerMetrics,
    ) -> KPIAssessment:
        """Assess a worker's KPI metrics."""
        return self._kpi.assess(metrics)

    def diagnose(
        self,
        worker_id: str,
        assessment: KPIAssessment,
    ) -> Diagnosis:
        """Diagnose the root cause of underperformance.

        Must determine WHY before selecting intervention level.
        Cause determines response, not just severity.
        """
        symptom = assessment.signals.reason or "underperforming"
        metrics = assessment.metrics

        # Determine probable cause from metrics
        if metrics.rate_limit_count > 5:
            cause = "rate_limited"
            reasoning = "High rate-limit count suggests external API constraint"
        elif metrics.error_count > 3:
            cause = "stuck_in_loop"
            reasoning = "Multiple errors suggest context is poisoned"
        elif metrics.context_compaction_count > 5:
            cause = "task_too_large"
            reasoning = "Many compactions suggest task exceeds context window"
        elif assessment.signals.silence_ratio > 3.0 and metrics.process_cpu_pct > 1.0:
            cause = "task_too_large"
            reasoning = "Long silence with CPU active — worker is working but slowly"
        elif assessment.signals.progress_rate < 0.3:
            cause = "unclear_requirements"
            reasoning = "Very low progress rate suggests confusion about goals"
        else:
            cause = "unknown"
            reasoning = "Cannot determine specific cause from available metrics"

        level = CAUSE_INTERVENTION_MAP.get(cause, 1)

        return Diagnosis(
            worker_id=worker_id,
            symptom=symptom,
            probable_cause=cause,
            recommended_level=level,
            reasoning=reasoning,
        )

    def should_intervene(self, worker_id: str, diagnosis: Diagnosis) -> bool:
        """Check all gates before allowing an intervention.

        Gates (all must pass):
        1. Budget check — supervisor has points remaining
        2. Process resource check — worker is not actively computing
        3. Cooldown check — enough time since last intervention
        4. Intervention limit — not exceeded for this worker
        5. Cause check — cause warrants intervention (not rate_limited/env)
        """
        record = self._workers.get(worker_id)
        if record is None:
            logger.warning("should_intervene: unknown worker %s", worker_id)
            return False

        # Gate 1: Budget
        if not self._budget.can_intervene:
            logger.info("Intervention blocked: budget exhausted")
            return False

        # Gate 2: Process resource check (F4 hard gate)
        if record.process_pid is not None:
            status = check_worker_process_alive(record.process_pid)
            if status["is_active"]:
                logger.info(
                    "Intervention blocked: worker %s is active (CPU=%.1f%%, MEM=%.0fMB)",
                    worker_id,
                    status["cpu_percent"],
                    status["memory_mb"],
                )
                return False

        # Gate 3: Cooldown
        if record.last_intervention_at is not None:
            elapsed_min = (time.monotonic() - record.last_intervention_at) / 60
            if elapsed_min < self._cooldown_min:
                logger.info(
                    "Intervention blocked: cooldown (%.0f min < %d min)",
                    elapsed_min,
                    self._cooldown_min,
                )
                return False

        # Gate 4: Intervention limit
        if record.intervention_count >= self._intervention_limit:
            logger.info(
                "Intervention blocked: limit reached (%d/%d)",
                record.intervention_count,
                self._intervention_limit,
            )
            return False

        # Gate 5: Cause check
        if diagnosis.recommended_level == 0:
            logger.info(
                "Intervention blocked: cause '%s' does not warrant intervention",
                diagnosis.probable_cause,
            )
            return False

        return True

    def should_escalate_to_human(self, worker_id: str) -> bool:
        """True if intervention limit reached for this worker."""
        record = self._workers.get(worker_id)
        if record is None:
            return False
        return record.intervention_count >= self._intervention_limit

    def record_intervention(
        self,
        worker_id: str,
        level: int,
        cause: str,
        action: str,
    ) -> InterventionResult:
        """Record an intervention in the worker's history."""
        result = InterventionResult(
            worker_id=worker_id,
            level=level,
            cause=cause,
            action_taken=action,
        )

        record = self._workers.get(worker_id)
        if record is None:
            record = WorkerRecord(worker_id=worker_id)
            self._workers[worker_id] = record

        record.interventions.append(result)
        record.last_intervention_at = time.monotonic()

        return result

    def record_accident(
        self,
        worker_id: str,
        failure_type: str,
        detail: str,
        supervisor_thinking: str = "",
    ) -> tuple[int, int]:
        """Record an accident and deduct budget points.

        Returns (points_deducted, remaining_budget).
        """
        points, remaining = self._budget.deduct_points(
            failure_type=failure_type,
            detail=f"Worker {worker_id}: {detail}",
            supervisor_thinking=supervisor_thinking,
        )
        logger.warning(
            "Accident recorded for worker %s: %s (-%d points, %d remaining)",
            worker_id,
            failure_type,
            points,
            remaining,
        )
        return (points, remaining)

    def get_budget_status(self) -> str:
        """Get formatted budget status for injection into pending.md."""
        return self._budget.format_budget_status()

    def format_escalation_message(
        self,
        worker_id: str,
        diagnosis: Diagnosis,
    ) -> str:
        """Format a human escalation message."""
        record = self._workers.get(worker_id)
        history = ""
        if record:
            for i, intervention in enumerate(record.interventions, 1):
                history += (
                    f"  {i}. L{intervention.level} — {intervention.cause}: "
                    f"{intervention.action_taken}\n"
                )

        return (
            f"ESCALATION — Worker {worker_id}\n"
            f"{'─' * 40}\n"
            f"Symptom: {diagnosis.symptom}\n"
            f"Probable cause: {diagnosis.probable_cause}\n"
            f"Confidence: {diagnosis.confidence:.0%}\n"
            f"Reasoning: {diagnosis.reasoning}\n\n"
            f"Intervention history ({record.intervention_count if record else 0}/"
            f"{self._intervention_limit}):\n"
            f"{history}\n"
            f"Budget: {self._budget.remaining_points}/{self._budget._initial_points}\n\n"
            "Human decision required: continue / abort / manual intervention"
        )
