import sqlite3
from datetime import UTC, datetime, timedelta

from anomaly_detection_engine.provider_health import provider_health_report
from anomaly_detection_engine.storage.database import create_connection, initialize_database

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def make_connection(tmp_path) -> sqlite3.Connection:
    connection = create_connection(tmp_path / "provider-health.db")
    initialize_database(connection)
    connection.row_factory = sqlite3.Row
    return connection


def add_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    source: str,
    started_at: datetime,
    status: str,
    source_payload: str | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO collector_runs (
            id, source, started_at, finished_at, status,
            records_received, records_accepted, records_rejected,
            error_type, error_message, source_payload
        ) VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?)
        """,
        (
            run_id,
            source,
            started_at.isoformat(),
            started_at.isoformat(),
            status,
            error_type,
            error_message,
            source_payload,
        ),
    )
    connection.commit()


def test_last_fresh_capture_is_distinct_from_mere_success(tmp_path):
    connection = make_connection(tmp_path)
    add_run(
        connection,
        run_id="r1",
        source="mozzart-file:mozzart",
        started_at=NOW - timedelta(days=8),
        status="success",
        source_payload='{"items": []}',
    )
    for i in range(5):
        add_run(
            connection,
            run_id=f"r{i + 2}",
            source="mozzart-file:mozzart",
            started_at=NOW - timedelta(hours=i),
            status="success",
            source_payload=None,
        )

    [report] = provider_health_report(connection, now=NOW)

    assert report.last_run_status == "success"
    assert report.last_success_at == NOW
    assert report.last_fresh_capture_at == NOW - timedelta(days=8)
    assert report.is_stale(now=NOW, stale_after_seconds=24 * 3600) is True
    assert report.is_stale(now=NOW, stale_after_seconds=10 * 24 * 3600) is False


def test_never_captured_is_stale_and_distinct_from_known_staleness(tmp_path):
    connection = make_connection(tmp_path)
    add_run(
        connection,
        run_id="r1",
        source="meridianbet-file:meridianbet",
        started_at=NOW,
        status="success",
        source_payload=None,
    )

    [report] = provider_health_report(connection, now=NOW)

    assert report.last_fresh_capture_at is None
    assert report.staleness_seconds(now=NOW) is None
    assert report.is_stale(now=NOW, stale_after_seconds=10**9) is True


def test_partial_status_counts_as_success_not_failure(tmp_path):
    connection = make_connection(tmp_path)
    add_run(
        connection,
        run_id="r1",
        source="api-football",
        started_at=NOW,
        status="partial",
        source_payload="{}",
    )

    [report] = provider_health_report(connection, now=NOW, window_hours=24)

    assert report.last_success_at == NOW
    assert report.consecutive_failures == 0
    assert report.failures_in_window == 0


def test_consecutive_failures_counts_only_the_trailing_streak(tmp_path):
    connection = make_connection(tmp_path)
    add_run(
        connection, run_id="r1", source="x", started_at=NOW - timedelta(hours=3), status="success"
    )
    add_run(
        connection, run_id="r2", source="x", started_at=NOW - timedelta(hours=2),
        status="failed", error_type="timeout", error_message="boom",
    )
    add_run(
        connection, run_id="r3", source="x", started_at=NOW - timedelta(hours=1),
        status="failed", error_type="timeout", error_message="boom again",
    )
    add_run(
        connection, run_id="r4", source="x", started_at=NOW,
        status="failed", error_type="timeout", error_message="boom once more",
    )

    [report] = provider_health_report(connection, now=NOW)

    assert report.consecutive_failures == 3
    assert report.last_error_type == "timeout"
    assert report.last_error_message == "boom once more"


def test_window_counts_exclude_runs_outside_the_window(tmp_path):
    connection = make_connection(tmp_path)
    add_run(
        connection, run_id="old", source="x", started_at=NOW - timedelta(hours=48), status="failed"
    )
    add_run(connection, run_id="new", source="x", started_at=NOW, status="success")

    [report] = provider_health_report(connection, now=NOW, window_hours=24)

    assert report.runs_in_window == 1
    assert report.failures_in_window == 0


def test_multiple_sources_are_each_reported_independently(tmp_path):
    connection = make_connection(tmp_path)
    add_run(connection, run_id="a1", source="api-football", started_at=NOW, status="success")
    add_run(
        connection, run_id="b1", source="the-odds-api:soccer_epl", started_at=NOW,
        status="failed",
    )

    reports = {
        report.source: report for report in provider_health_report(connection, now=NOW)
    }

    assert set(reports) == {"api-football", "the-odds-api:soccer_epl"}
    assert reports["api-football"].last_run_status == "success"
    assert reports["the-odds-api:soccer_epl"].last_run_status == "failed"
