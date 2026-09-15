import sqlite3
from datetime import UTC, datetime

from anomaly_detection_engine.models.collector_run import CollectorRun, CollectorRunStatus
from anomaly_detection_engine.storage.collector_run_repository import CollectorRunRepository
from anomaly_detection_engine.storage.database import configure_connection, initialize_database


def create_test_connection():
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    return connection


def test_saves_and_finds_collector_run():
    connection = create_test_connection()
    repository = CollectorRunRepository(connection)

    run = CollectorRun(
        id="run-001",
        source="json:odds_sample.json",
        started_at=datetime(2026, 8, 27, 8, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 27, 8, 0, 4, tzinfo=UTC),
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


def _make_run(run_id: str, source: str, started_at: datetime) -> CollectorRun:
    return CollectorRun(
        id=run_id,
        source=source,
        started_at=started_at,
        finished_at=started_at,
        status=CollectorRunStatus.SUCCESS,
        records_received=0,
        records_accepted=0,
        records_rejected=0,
        collector_version="0.1.0",
    )


def test_find_latest_by_source_returns_none_when_never_run():
    connection = create_test_connection()
    repository = CollectorRunRepository(connection)

    assert repository.find_latest_by_source("the-odds-api:soccer_epl") is None


def test_find_latest_by_source_returns_the_most_recent_run():
    connection = create_test_connection()
    repository = CollectorRunRepository(connection)

    repository.save(_make_run("run-1", "the-odds-api:soccer_epl", datetime(2026, 9, 1, tzinfo=UTC)))
    repository.save(_make_run("run-2", "the-odds-api:soccer_epl", datetime(2026, 9, 3, tzinfo=UTC)))
    repository.save(_make_run("run-3", "the-odds-api:soccer_epl", datetime(2026, 9, 2, tzinfo=UTC)))

    latest = repository.find_latest_by_source("the-odds-api:soccer_epl")

    assert latest is not None
    assert latest.id == "run-2"


def test_find_latest_by_source_ignores_other_sources():
    connection = create_test_connection()
    repository = CollectorRunRepository(connection)

    repository.save(_make_run("run-1", "api-football", datetime(2026, 9, 3, tzinfo=UTC)))
    repository.save(_make_run("run-2", "the-odds-api:soccer_epl", datetime(2026, 9, 1, tzinfo=UTC)))

    latest = repository.find_latest_by_source("the-odds-api:soccer_epl")

    assert latest is not None
    assert latest.id == "run-2"
