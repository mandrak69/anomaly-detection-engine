import sqlite3
from datetime import datetime, timezone

from anomaly_detection_engine.models.collector_run import CollectorRun, CollectorRunStatus
from anomaly_detection_engine.storage.collector_run_repository import CollectorRunRepository
from anomaly_detection_engine.storage.database import initialize_database


def create_test_connection():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    return connection


def test_saves_and_finds_collector_run():
    connection = create_test_connection()
    repository = CollectorRunRepository(connection)

    run = CollectorRun(
        id="run-001",
        source="json:odds_sample.json",
        started_at=datetime(2026, 8, 27, 8, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 8, 27, 8, 0, 4, tzinfo=timezone.utc),
        status=CollectorRunStatus.PARTIAL,
        records_received=6,
        records_accepted=5,
        records_rejected=1,
        collector_version="0.1.0",
    )

    repository.save(run)

    found = repository.find_by_id("run-001")

    assert found is not None
    assert found.source == "json:odds_sample.json"
    assert found.status == CollectorRunStatus.PARTIAL
    assert found.records_accepted == 5
    assert found.records_rejected == 1
    assert found.duration_seconds == 4.0


def test_find_by_id_returns_none_when_missing():
    connection = create_test_connection()
    repository = CollectorRunRepository(connection)

    assert repository.find_by_id("does-not-exist") is None
