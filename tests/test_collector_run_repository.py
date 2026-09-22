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


def test_start_inserts_a_running_row_with_no_finished_at():
    connection = create_test_connection()
    repository = CollectorRunRepository(connection)

    repository.start(
        CollectorRun(
            id="run-1",
            source="mozzart-file:mozzart",
            started_at=datetime(2026, 9, 22, 8, 0, 0, tzinfo=UTC),
            status=CollectorRunStatus.RUNNING,
            records_received=0,
            records_accepted=0,
            records_rejected=0,
            provider_id="mozzart",
        )
    )

    found = repository.find_by_id("run-1")
    assert found is not None
    assert found.status == CollectorRunStatus.RUNNING
    assert found.finished_at is None
    assert found.provider_id == "mozzart"


def test_finish_updates_the_existing_running_row_in_place():
    # Proves finish() is an UPDATE, not a second INSERT -- start() then
    # finish() on the same run.id must leave exactly one row, with the
    # RUNNING row's original started_at preserved.
    connection = create_test_connection()
    repository = CollectorRunRepository(connection)
    started_at = datetime(2026, 9, 22, 8, 0, 0, tzinfo=UTC)

    repository.start(
        CollectorRun(
            id="run-1",
            source="mozzart-file:mozzart",
            started_at=started_at,
            status=CollectorRunStatus.RUNNING,
            records_received=0,
            records_accepted=0,
            records_rejected=0,
        )
    )
    repository.finish(
        CollectorRun(
            id="run-1",
            source="mozzart-file:mozzart",
            started_at=started_at,
            finished_at=datetime(2026, 9, 22, 8, 0, 4, tzinfo=UTC),
            status=CollectorRunStatus.SUCCESS,
            records_received=5,
            records_accepted=5,
            records_rejected=0,
        )
    )

    found = repository.find_by_id("run-1")
    assert found is not None
    assert found.status == CollectorRunStatus.SUCCESS
    assert found.finished_at is not None
    assert found.records_accepted == 5
    assert found.started_at == started_at

    all_rows = connection.execute("SELECT COUNT(*) FROM collector_runs").fetchone()[0]
    assert all_rows == 1


def test_recover_stale_running_fails_a_run_older_than_the_threshold():
    connection = create_test_connection()
    repository = CollectorRunRepository(connection)
    repository.start(
        CollectorRun(
            id="run-old",
            source="mozzart-file:mozzart",
            started_at=datetime(2026, 9, 22, 8, 0, 0, tzinfo=UTC),
            status=CollectorRunStatus.RUNNING,
            records_received=0,
            records_accepted=0,
            records_rejected=0,
        )
    )

    recovered = repository.recover_stale_running(
        older_than=datetime(2026, 9, 22, 9, 0, 0, tzinfo=UTC),
        recovered_at=datetime(2026, 9, 22, 9, 30, 0, tzinfo=UTC),
    )

    assert recovered == ["run-old"]
    found = repository.find_by_id("run-old")
    assert found is not None
    assert found.status == CollectorRunStatus.FAILED
    assert found.error_type == "process_interrupted"
    assert found.error_message is not None
    assert found.finished_at == datetime(2026, 9, 22, 9, 30, 0, tzinfo=UTC)


def test_recover_stale_running_leaves_a_recent_running_row_alone():
    # Could genuinely belong to a sibling watch_capture.py-spawned
    # process still in the middle of its own run -- see
    # FixtureCatalog's own docstring on concurrent processes sharing one
    # database file. Must not be marked FAILED out from under it.
    connection = create_test_connection()
    repository = CollectorRunRepository(connection)
    repository.start(
        CollectorRun(
            id="run-recent",
            source="mozzart-file:mozzart",
            started_at=datetime(2026, 9, 22, 9, 15, 0, tzinfo=UTC),
            status=CollectorRunStatus.RUNNING,
            records_received=0,
            records_accepted=0,
            records_rejected=0,
        )
    )

    recovered = repository.recover_stale_running(
        older_than=datetime(2026, 9, 22, 9, 0, 0, tzinfo=UTC),
        recovered_at=datetime(2026, 9, 22, 9, 30, 0, tzinfo=UTC),
    )

    assert recovered == []
    found = repository.find_by_id("run-recent")
    assert found is not None
    assert found.status == CollectorRunStatus.RUNNING
    assert found.finished_at is None


def test_recover_stale_running_does_not_touch_already_finished_runs():
    connection = create_test_connection()
    repository = CollectorRunRepository(connection)
    repository.save(_make_run("run-done", "mozzart-file:mozzart", datetime(2026, 1, 1, tzinfo=UTC)))

    recovered = repository.recover_stale_running(
        older_than=datetime(2026, 9, 22, 9, 0, 0, tzinfo=UTC),
        recovered_at=datetime(2026, 9, 22, 9, 30, 0, tzinfo=UTC),
    )

    assert recovered == []
    found = repository.find_by_id("run-done")
    assert found is not None
    assert found.status == CollectorRunStatus.SUCCESS


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
