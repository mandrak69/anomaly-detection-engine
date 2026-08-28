from collections import Counter
from dataclasses import dataclass, field

from anomaly_detection_engine.models.collector_run import CollectorRun


@dataclass
class IngestionMetrics:
    """In-process counters accumulated across OddsIngestionService.run() calls.

    This is an accumulator, not an exporter -- a real deployment would
    ship `snapshot()` (or the structured log lines from
    observability.logging_config) to whatever metrics backend it uses
    (StatsD, Prometheus, CloudWatch, ...). Scoped to one process's
    lifetime; nothing here is persisted.
    """

    total_runs: int = 0
    total_received: int = 0
    total_accepted: int = 0
    total_rejected: int = 0
    runs_by_status: Counter = field(default_factory=Counter)
    rejections_by_reason: Counter = field(default_factory=Counter)

    def record_run(self, run: CollectorRun, rejection_reasons: list[str]) -> None:
        self.total_runs += 1
        self.total_received += run.records_received
        self.total_accepted += run.records_accepted
        self.total_rejected += run.records_rejected
        self.runs_by_status[run.status.value] += 1

        for reason in rejection_reasons:
            # Keep only the validation-stage prefix ("structural",
            # "semantic", "identity") as the label -- the full reason
            # string carries per-record detail (specific error codes,
            # match ids) that isn't a useful metric dimension.
            stage = reason.split(":", 1)[0]
            self.rejections_by_reason[stage] += 1

    def snapshot(self) -> dict:
        return {
            "total_runs": self.total_runs,
            "total_received": self.total_received,
            "total_accepted": self.total_accepted,
            "total_rejected": self.total_rejected,
            "runs_by_status": dict(self.runs_by_status),
            "rejections_by_reason": dict(self.rejections_by_reason),
        }
