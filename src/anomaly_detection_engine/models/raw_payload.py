from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RawPayloadRecord:
    id: int
    collector_run_id: str
    source: str
    payload: str
    accepted: bool
    received_at: datetime
    rejection_reason: str | None = None
