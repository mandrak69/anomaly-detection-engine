import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from anomaly_detection_engine.config import DB_BUSY_TIMEOUT_SECONDS, AppConfig
from anomaly_detection_engine.observability.metrics import IngestionMetrics
from anomaly_detection_engine.storage.collector_run_repository import CollectorRunRepository
from anomaly_detection_engine.storage.database import create_connection, initialize_database
from anomaly_detection_engine.storage.event_status_repository import EventStatusRepository
from anomaly_detection_engine.storage.movement_repository import MovementRepository
from anomaly_detection_engine.storage.odds_repository import OddsRepository
from anomaly_detection_engine.storage.raw_payload_repository import RawPayloadRepository
from anomaly_detection_engine.storage.signal_repository import SignalRepository

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    """Every wired-up dependency a poll/analysis cycle needs, built once
    by build_runtime() -- plain data, no framework/DI container."""

    connection: sqlite3.Connection
    odds_repository: OddsRepository
    collector_run_repository: CollectorRunRepository
    raw_payload_repository: RawPayloadRepository
    signal_repository: SignalRepository
    movement_repository: MovementRepository
    event_status_repository: EventStatusRepository
    metrics: IngestionMetrics


def build_runtime(config: AppConfig) -> Runtime:
    if config.db_path != ":memory:":
        Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = create_connection(config.db_path, timeout=DB_BUSY_TIMEOUT_SECONDS)
    initialize_database(connection)

    collector_run_repository = CollectorRunRepository(connection)
    # Once per process start, not once per poll cycle: a RUNNING row left
    # behind by *this same process*'s own previous life (killed or
    # crashed before finish()) is only ever discoverable at the next
    # startup -- see CollectorRunRepository.recover_stale_running for why
    # older_than exists (multiple concurrent processes can share one
    # database file) and why this doesn't just mark every RUNNING row.
    now = datetime.now(UTC)
    recovered_ids = collector_run_repository.recover_stale_running(
        older_than=now - config.stale_running_threshold, recovered_at=now
    )
    if recovered_ids:
        logger.warning(
            "runtime.stale_collector_runs_recovered",
            extra={"run_ids": recovered_ids, "count": len(recovered_ids)},
        )

    return Runtime(
        connection=connection,
        odds_repository=OddsRepository(connection),
        collector_run_repository=collector_run_repository,
        raw_payload_repository=RawPayloadRepository(connection),
        signal_repository=SignalRepository(connection),
        movement_repository=MovementRepository(connection),
        event_status_repository=EventStatusRepository(connection),
        metrics=IngestionMetrics(),
    )
