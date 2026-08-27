from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class CollectorRunStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class CollectorRun:
    id: str
    source: str
    started_at: datetime
    finished_at: datetime
    status: CollectorRunStatus
    records_received: int
    records_accepted: int
    records_rejected: int
    collector_version: str | None = None
    error_type: str | None = None
    error_message: str | None = None

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def acceptance_rate(self) -> float:
        if self.records_received == 0:
            return 0.0

        return self.records_accepted / self.records_received