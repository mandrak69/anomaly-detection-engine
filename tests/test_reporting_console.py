import sqlite3
from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.config import AppConfig
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.market import DEFAULT_MARKET
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.observability.metrics import IngestionMetrics
from anomaly_detection_engine.reporting.console import print_reports
from anomaly_detection_engine.runtime import Runtime
from anomaly_detection_engine.storage.collector_run_repository import CollectorRunRepository
from anomaly_detection_engine.storage.database import configure_connection, initialize_database
from anomaly_detection_engine.storage.movement_repository import MovementRepository
from anomaly_detection_engine.storage.odds_repository import OddsRepository
from anomaly_detection_engine.storage.raw_payload_repository import RawPayloadRepository
from anomaly_detection_engine.storage.signal_repository import SignalRepository

NOW = datetime.fromisoformat("2026-08-27T10:00:00+00:00")


def build_runtime_for_test() -> Runtime:
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
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


def build_config() -> AppConfig:
    return AppConfig(
        db_path=":memory:",
        odds_source="demo",
        sport_key="soccer_epl",
        odds_api_mode="auto",
        odds_api_capture_dir=None,
        mozzart_capture_dir=None,
        mozzart_mode="manual",
        min_value_gap_percent=Decimal("15.0"),
    )


def test_print_reports_renders_a_real_surebet(capsys):
    # print_reports is presentation on top of already-persisted data --
    # it must work standalone (not called from app.py's core run by
    # default) and still surface a real, actionable surebet.
    runtime = build_runtime_for_test()
    event = Event(
        id="e1",
        sport="football",
        league="L",
        competition_id="competition-1",
        home_team=Team("h", "A"),
        away_team=Team("a", "B"),
        start_time=NOW,
    )

    for outcome, odds in [("1", "2.50"), ("X", "4.00"), ("2", "4.00")]:
        runtime.odds_repository.save(
            OddsSnapshot(
                event_id="e1",
                bookmaker=Bookmaker("bet1", "Bet1"),
                market=DEFAULT_MARKET,
                outcome=outcome,
                odds=Decimal(odds),
                observed_at=NOW,
            )
        )

    print_reports(runtime, [event], build_config())

    captured = capsys.readouterr()
    assert "A vs B" in captured.out
    assert "Surebet: YES" in captured.out
    assert "OPPORTUNITIES" in captured.out
    assert "ODDS MOVEMENT" in captured.out


def test_print_reports_handles_no_events(capsys):
    runtime = build_runtime_for_test()

    print_reports(runtime, [], build_config())

    captured = capsys.readouterr()
    assert "No opportunities above threshold." in captured.out
    assert "No significant odds movements." in captured.out
