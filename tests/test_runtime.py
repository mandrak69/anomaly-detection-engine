import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from anomaly_detection_engine.config import AppConfig
from anomaly_detection_engine.models.collector_run import CollectorRun, CollectorRunStatus
from anomaly_detection_engine.runtime import build_runtime
from anomaly_detection_engine.storage.collector_run_repository import CollectorRunRepository
from anomaly_detection_engine.storage.database import configure_connection, initialize_database


def build_config(db_path: str, *, stale_running_threshold: timedelta) -> AppConfig:
    return AppConfig(
        db_path=db_path,
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
        stale_running_threshold=stale_running_threshold,
    )


def test_build_runtime_recovers_a_running_row_left_by_a_crashed_previous_process(tmp_path):
    # Simulates the real scenario this exists for: a previous process
    # started a run (wrote it as RUNNING), then was killed (an
    # AppHang-style termination this project has hit in practice) before
    # ever calling finish(). A file-based DB, not :memory:, so the
    # "crashed process" and "the next startup" are genuinely two
    # separate connections sharing the same persisted data.
    db_path = str(tmp_path / "test.db")

    crashed_connection = sqlite3.connect(db_path)
    configure_connection(crashed_connection)
    initialize_database(crashed_connection)
    CollectorRunRepository(crashed_connection).start(
        CollectorRun(
            id="run-crashed",
            source="mozzart-file:mozzart",
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=CollectorRunStatus.RUNNING,
            records_received=0,
            records_accepted=0,
            records_rejected=0,
        )
    )
    crashed_connection.close()

    runtime = build_runtime(
        build_config(db_path, stale_running_threshold=timedelta(minutes=60))
    )

    recovered = runtime.collector_run_repository.find_by_id("run-crashed")
    assert recovered is not None
    assert recovered.status == CollectorRunStatus.FAILED
    assert recovered.error_type == "process_interrupted"


def test_build_runtime_leaves_a_recent_running_row_alone(tmp_path):
    db_path = str(tmp_path / "test.db")

    other_connection = sqlite3.connect(db_path)
    configure_connection(other_connection)
    initialize_database(other_connection)
    CollectorRunRepository(other_connection).start(
        CollectorRun(
            id="run-in-progress",
            source="mozzart-file:mozzart-prematch",
            started_at=datetime.now(UTC),
            status=CollectorRunStatus.RUNNING,
            records_received=0,
            records_accepted=0,
            records_rejected=0,
        )
    )
    other_connection.close()

    runtime = build_runtime(
        build_config(db_path, stale_running_threshold=timedelta(minutes=60))
    )

    still_running = runtime.collector_run_repository.find_by_id("run-in-progress")
    assert still_running is not None
    assert still_running.status == CollectorRunStatus.RUNNING
