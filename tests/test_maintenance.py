import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from anomaly_detection_engine.config import AppConfig
from anomaly_detection_engine.maintenance import run_retention_cleanup
from anomaly_detection_engine.storage.database import configure_connection, initialize_database
from anomaly_detection_engine.storage.time_utils import to_utc_iso

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)


def make_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    return connection


def build_config(
    *,
    raw_payload_days: int = 14,
    source_payload_days: int = 14,
    odds_snapshot_days: int = 90,
    movement_days: int = 180,
    signal_history_days: int = 180,
) -> AppConfig:
    return AppConfig(
        db_path=":memory:",
        odds_source="demo",
        sport_key="soccer_epl",
        odds_api_mode="auto",
        odds_api_capture_dir=None,
        mozzart_capture_dir=None,
        mozzart_mode="manual",
        mozzart_prematch_capture_dir=None,
        meridianbet_capture_dir=None,
        meridianbet_mode="manual",
        min_value_gap_percent=Decimal("15.0"),
        signal_ttl=timedelta(hours=3),
        max_quote_age=timedelta(hours=1),
        max_observation_spread=timedelta(minutes=30),
        odds_api_key=None,
        odds_api_min_interval=timedelta(hours=4),
        api_football_key=None,
        stale_running_threshold=timedelta(minutes=60),
        raw_payload_retention=timedelta(days=raw_payload_days),
        collector_run_source_payload_retention=timedelta(days=source_payload_days),
        odds_snapshot_retention=timedelta(days=odds_snapshot_days),
        movement_retention=timedelta(days=movement_days),
        signal_history_retention=timedelta(days=signal_history_days),
    )


def seed_collector_run(
    connection, run_id: str, *, finished_at: datetime | None, source_payload: str | None = "{}"
) -> None:
    connection.execute(
        """
        INSERT INTO collector_runs (
            id, source, started_at, finished_at, status,
            records_received, records_accepted, records_rejected, source_payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, "mozzart-file:mozzart", to_utc_iso(NOW),
            to_utc_iso(finished_at) if finished_at else None,
            "success" if finished_at else "running",
            1, 1, 0, source_payload,
        ),
    )
    connection.commit()


def seed_raw_payload(connection, run_id: str, *, received_at: datetime) -> None:
    connection.execute(
        """
        INSERT INTO raw_payloads (collector_run_id, source, payload, accepted, received_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, "Mozzart", "{}", 1, to_utc_iso(received_at)),
    )
    connection.commit()


def seed_odds_snapshot(
    connection, event_id: str, *, observed_at: datetime, collector_run_id: str | None = None
) -> None:
    connection.execute(
        """
        INSERT INTO odds_snapshots (
            event_id, bookmaker_id, bookmaker_name, market_type, market_period,
            outcome, odds, observed_at, market_phase, collector_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id, "bet1", "Bet1", "three_way", "full_time",
            "1", "2.10", to_utc_iso(observed_at), "pre_match", collector_run_id,
        ),
    )
    connection.commit()


def seed_movement(connection, event_id: str, *, detected_at: datetime) -> None:
    connection.execute(
        """
        INSERT INTO movements (
            event_id, market_type, market_period, outcome, bookmaker_id, bookmaker_name,
            previous_odds, current_odds, change_percent, previous_observed_at,
            current_observed_at, detected_at, market_phase
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id, "three_way", "full_time", "1", "bet1", "Bet1",
            "2.00", "2.10", "5.0", to_utc_iso(detected_at), to_utc_iso(detected_at),
            to_utc_iso(detected_at), "pre_match",
        ),
    )
    connection.commit()


def seed_signal(connection, signal_id: str) -> None:
    connection.execute(
        """
        INSERT INTO signals (
            id, signal_type, event_id, market_type, market_period,
            market_phase, outcome, status, edge_percent, peak_edge_percent, details,
            first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id, "SUREBET", "event-1", "three_way", "full_time",
            "pre_match", None, "ACTIVE", "10.0", "10.0", "{}",
            to_utc_iso(NOW), to_utc_iso(NOW),
        ),
    )
    connection.commit()


def seed_signal_history(connection, signal_id: str, *, recorded_at: datetime) -> None:
    connection.execute(
        """
        INSERT INTO signal_history
            (signal_id, event_type, status, edge_percent, details, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (signal_id, "created", "ACTIVE", "10.0", "{}", to_utc_iso(recorded_at)),
    )
    connection.commit()


def test_deletes_raw_payloads_older_than_the_configured_retention():
    connection = make_connection()
    seed_collector_run(connection, "run-1", finished_at=NOW)
    seed_raw_payload(connection, "run-1", received_at=NOW - timedelta(days=30))
    seed_raw_payload(connection, "run-1", received_at=NOW - timedelta(days=1))

    summary = run_retention_cleanup(connection, build_config(raw_payload_days=14), now=NOW)

    assert summary.raw_payloads_deleted == 1
    remaining = connection.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0]
    assert remaining == 1


def test_clears_only_the_source_payload_column_not_the_whole_row():
    connection = make_connection()
    seed_collector_run(connection, "run-old", finished_at=NOW - timedelta(days=30))

    summary = run_retention_cleanup(
        connection, build_config(source_payload_days=14), now=NOW
    )

    assert summary.collector_run_source_payloads_cleared == 1
    row = connection.execute(
        "SELECT * FROM collector_runs WHERE id = 'run-old'"
    ).fetchone()
    assert row is not None  # the row itself survives
    assert row["source_payload"] is None
    assert row["status"] == "success"  # every other field untouched


def test_never_clears_source_payload_on_a_still_running_run():
    connection = make_connection()
    seed_collector_run(connection, "run-stuck", finished_at=None)
    # started_at (see seed_collector_run) is NOW, but finished_at is NULL --
    # a RUNNING row must never be touched regardless of age.

    summary = run_retention_cleanup(
        connection, build_config(source_payload_days=0), now=NOW + timedelta(days=365)
    )

    assert summary.collector_run_source_payloads_cleared == 0
    row = connection.execute("SELECT * FROM collector_runs WHERE id = 'run-stuck'").fetchone()
    assert row["source_payload"] == "{}"


def test_deletes_odds_snapshots_older_than_the_configured_retention():
    connection = make_connection()
    seed_collector_run(connection, "run-1", finished_at=NOW)
    seed_odds_snapshot(
        connection, "event-1", observed_at=NOW - timedelta(days=120), collector_run_id="run-1",
    )
    seed_odds_snapshot(
        connection, "event-1", observed_at=NOW - timedelta(days=1), collector_run_id="run-1",
    )

    summary = run_retention_cleanup(connection, build_config(odds_snapshot_days=90), now=NOW)

    assert summary.odds_snapshots_deleted == 1
    remaining = connection.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
    assert remaining == 1
    # The parent collector_runs row is untouched by deleting old snapshots.
    assert connection.execute("SELECT COUNT(*) FROM collector_runs").fetchone()[0] == 1


def test_deletes_movements_older_than_the_configured_retention():
    connection = make_connection()
    seed_movement(connection, "event-1", detected_at=NOW - timedelta(days=200))
    seed_movement(connection, "event-1", detected_at=NOW - timedelta(days=1))

    summary = run_retention_cleanup(connection, build_config(movement_days=180), now=NOW)

    assert summary.movements_deleted == 1
    remaining = connection.execute("SELECT COUNT(*) FROM movements").fetchone()[0]
    assert remaining == 1


def test_deletes_signal_history_older_than_the_configured_retention():
    connection = make_connection()
    seed_signal(connection, "signal-1")
    seed_signal_history(connection, "signal-1", recorded_at=NOW - timedelta(days=200))
    seed_signal_history(connection, "signal-1", recorded_at=NOW - timedelta(days=1))

    summary = run_retention_cleanup(connection, build_config(signal_history_days=180), now=NOW)

    assert summary.signal_history_deleted == 1
    remaining = connection.execute("SELECT COUNT(*) FROM signal_history").fetchone()[0]
    assert remaining == 1
    # The parent signals row survives.
    assert connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1


def test_dry_run_reports_counts_without_deleting_anything():
    connection = make_connection()
    seed_collector_run(connection, "run-1", finished_at=NOW - timedelta(days=30))
    seed_raw_payload(connection, "run-1", received_at=NOW - timedelta(days=30))
    seed_odds_snapshot(
        connection, "event-1", observed_at=NOW - timedelta(days=120), collector_run_id="run-1",
    )
    seed_movement(connection, "event-1", detected_at=NOW - timedelta(days=200))
    seed_signal(connection, "signal-1")
    seed_signal_history(connection, "signal-1", recorded_at=NOW - timedelta(days=200))

    summary = run_retention_cleanup(connection, build_config(), now=NOW, dry_run=True)

    assert summary.raw_payloads_deleted == 1
    assert summary.collector_run_source_payloads_cleared == 1
    assert summary.odds_snapshots_deleted == 1
    assert summary.movements_deleted == 1
    assert summary.signal_history_deleted == 1

    # Nothing actually removed.
    assert connection.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM movements").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM signal_history").fetchone()[0] == 1
    row = connection.execute(
        "SELECT source_payload FROM collector_runs WHERE id='run-1'"
    ).fetchone()
    assert row["source_payload"] is not None


def test_total_rows_affected_sums_every_count():
    connection = make_connection()
    seed_collector_run(connection, "run-1", finished_at=NOW - timedelta(days=30))
    seed_raw_payload(connection, "run-1", received_at=NOW - timedelta(days=30))
    seed_odds_snapshot(
        connection, "event-1", observed_at=NOW - timedelta(days=120), collector_run_id="run-1",
    )

    summary = run_retention_cleanup(connection, build_config(), now=NOW)

    assert summary.total_rows_affected == (
        summary.raw_payloads_deleted
        + summary.collector_run_source_payloads_cleared
        + summary.odds_snapshots_deleted
        + summary.movements_deleted
        + summary.signal_history_deleted
    )
    assert summary.total_rows_affected == 3  # raw_payload + source_payload cleared + snapshot


def test_nothing_within_retention_is_touched():
    connection = make_connection()
    seed_collector_run(connection, "run-1", finished_at=NOW)
    seed_raw_payload(connection, "run-1", received_at=NOW - timedelta(days=1))
    seed_odds_snapshot(
        connection, "event-1", observed_at=NOW - timedelta(days=1), collector_run_id="run-1",
    )
    seed_movement(connection, "event-1", detected_at=NOW - timedelta(days=1))
    seed_signal(connection, "signal-1")
    seed_signal_history(connection, "signal-1", recorded_at=NOW - timedelta(days=1))

    summary = run_retention_cleanup(connection, build_config(), now=NOW)

    assert summary.total_rows_affected == 0
