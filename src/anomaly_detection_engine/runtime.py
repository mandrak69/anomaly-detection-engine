import sqlite3
from dataclasses import dataclass
from pathlib import Path

from anomaly_detection_engine.config import DB_BUSY_TIMEOUT_SECONDS, AppConfig
from anomaly_detection_engine.observability.metrics import IngestionMetrics
from anomaly_detection_engine.storage.collector_run_repository import CollectorRunRepository
from anomaly_detection_engine.storage.database import create_connection, initialize_database
from anomaly_detection_engine.storage.movement_repository import MovementRepository
from anomaly_detection_engine.storage.odds_repository import OddsRepository
from anomaly_detection_engine.storage.raw_payload_repository import RawPayloadRepository
from anomaly_detection_engine.storage.signal_repository import SignalRepository


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
    metrics: IngestionMetrics


def build_runtime(config: AppConfig) -> Runtime:
    if config.db_path != ":memory:":
        Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = create_connection(config.db_path, timeout=DB_BUSY_TIMEOUT_SECONDS)
    initialize_database(connection)

    return Runtime(
        connection=connection,
        odds_repository=OddsRepository(connection),
        collector_run_repository=CollectorRunRepository(connection),
        raw_payload_repository=RawPayloadRepository(connection),
        signal_repository=SignalRepository(connection),
        movement_repository=MovementRepository(connection),
        metrics=IngestionMetrics(),
    )
