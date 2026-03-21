"""
claude_runner/kpi_collector.py

KPI signal collection and normalization for worker supervision (§10).

Collects metrics from running workers and derives performance signals.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class WorkerMetrics:
    """Raw metrics collected from a running worker."""

    worker_id: str
    elapsed_s: float = 0.0
    expected_duration_s: float = 0.0
    phases_completed: int = 0
    phases_total: int = 0
    last_commit_age_s: float = 0.0
    expected_commit_interval_s: float = 300.0  # 5 min default
    output_file_count: int = 0
    error_count: int = 0
    rate_limit_count: int = 0
    context_compaction_count: int = 0
    process_cpu_pct: float = 0.0
    process_memory_mb: float = 0.0
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class DerivedSignals:
    """Derived performance signals from raw metrics."""

    progress_rate: float = 0.0      # phases_completed / elapsed normalized against expected
    silence_ratio: float = 0.0      # last_commit_age / expected_commit_interval
    peer_rank: float | None = None  # percentile among parallel workers (0.0 - 1.0)
    is_underperforming: bool = False
    reason: str = ""


@dataclass
class KPIAssessment:
    """Full assessment combining raw metrics and derived signals."""

    worker_id: str
    metrics: WorkerMetrics
    signals: DerivedSignals
    underperforming: bool = False
    severity: str = "normal"  # normal | warning | critical


class KPICollector:
    """Collects and normalizes KPI signals from running workers.

    Metrics are collected per-worker and compared against expectations
    (from project book) and peers (parallel workers).
    """

    def __init__(self) -> None:
        self._history: dict[str, list[WorkerMetrics]] = {}

    def record(self, metrics: WorkerMetrics) -> None:
        """Record a metrics snapshot for a worker."""
        if metrics.worker_id not in self._history:
            self._history[metrics.worker_id] = []
        self._history[metrics.worker_id].append(metrics)

    def assess(self, metrics: WorkerMetrics) -> KPIAssessment:
        """Assess a worker's performance from its current metrics.

        Returns a KPIAssessment with derived signals and underperformance flag.
        """
        self.record(metrics)
        signals = self._derive_signals(metrics)
        severity = self._classify_severity(signals)
        underperforming = severity in ("warning", "critical")

        return KPIAssessment(
            worker_id=metrics.worker_id,
            metrics=metrics,
            signals=signals,
            underperforming=underperforming,
            severity=severity,
        )

    def assess_peer_group(
        self,
        all_metrics: list[WorkerMetrics],
    ) -> list[KPIAssessment]:
        """Assess a group of parallel workers, including peer ranking."""
        assessments = []
        # Calculate progress rates for peer comparison
        rates = []
        for m in all_metrics:
            signals = self._derive_signals(m)
            rates.append((m.worker_id, signals.progress_rate))
            assessments.append((m, signals))

        # Sort by progress rate and assign peer ranks
        rates.sort(key=lambda x: x[1])
        rank_map = {}
        for i, (wid, _) in enumerate(rates):
            rank_map[wid] = i / max(len(rates) - 1, 1)

        results = []
        for m, signals in assessments:
            signals.peer_rank = rank_map.get(m.worker_id)
            severity = self._classify_severity(signals)
            results.append(KPIAssessment(
                worker_id=m.worker_id,
                metrics=m,
                signals=signals,
                underperforming=severity in ("warning", "critical"),
                severity=severity,
            ))

        return results

    def _derive_signals(self, m: WorkerMetrics) -> DerivedSignals:
        """Derive performance signals from raw metrics."""
        # Progress rate: normalized against expected
        if m.expected_duration_s > 0 and m.elapsed_s > 0 and m.phases_total > 0:
            expected_progress = m.elapsed_s / m.expected_duration_s
            actual_progress = m.phases_completed / m.phases_total
            progress_rate = actual_progress / max(expected_progress, 0.01)
        else:
            progress_rate = 1.0  # No expectation → assume on track

        # Silence ratio
        if m.expected_commit_interval_s > 0:
            silence_ratio = m.last_commit_age_s / m.expected_commit_interval_s
        else:
            silence_ratio = 0.0

        signals = DerivedSignals(
            progress_rate=progress_rate,
            silence_ratio=silence_ratio,
        )

        # Determine underperformance
        reasons = []
        if progress_rate < 0.5 and m.elapsed_s > 600:  # <50% expected after 10 min
            reasons.append(f"progress_rate={progress_rate:.2f} (<0.5)")
        if silence_ratio > 3.0 and m.process_cpu_pct < 1.0:
            reasons.append(f"silence_ratio={silence_ratio:.1f} (>3x expected, CPU idle)")
        if m.error_count > 3:
            reasons.append(f"error_count={m.error_count}")

        if reasons:
            signals.is_underperforming = True
            signals.reason = "; ".join(reasons)

        return signals

    def _classify_severity(self, signals: DerivedSignals) -> str:
        """Classify severity based on derived signals."""
        if not signals.is_underperforming:
            return "normal"
        if signals.progress_rate < 0.25 or signals.silence_ratio > 5.0:
            return "critical"
        return "warning"
