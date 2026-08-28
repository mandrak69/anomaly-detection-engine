import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.reporting.movement_report import (
    build_movement_report,
    render_movement_report,
)
from anomaly_detection_engine.storage.database import initialize_database
from anomaly_detection_engine.storage.odds_repository import OddsRepository

MARKET = MarketIdentity(market_type=MarketType.THREE_WAY, period=MarketPeriod.FULL_TIME)
T0 = datetime.fromisoformat("2026-08-27T10:00:00+00:00")


def make_repository():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    return OddsRepository(connection)


def save(repository, event_id, bookmaker_name, outcome, odds, observed_at):
    repository.save(
        OddsSnapshot(
            event_id=event_id,
            bookmaker=Bookmaker(bookmaker_name.lower(), bookmaker_name),
            market=MARKET,
            outcome=outcome,
            odds=Decimal(odds),
            observed_at=observed_at,
        )
    )


def make_event(event_id: str, home: str, away: str) -> Event:
    return Event(
        id=event_id,
        sport="football",
        league="demo-league",
        home_team=Team(f"{event_id}-home", home),
        away_team=Team(f"{event_id}-away", away),
        start_time=T0,
    )


def test_detects_odds_halving_between_last_two_readings():
    repository = make_repository()
    event = make_event("e1", "A", "B")

    save(repository, "e1", "Mozzart", "1", "2.20", T0)
    save(repository, "e1", "Mozzart", "1", "1.10", T0 + timedelta(minutes=3))

    rows = build_movement_report([event], repository, MARKET)

    assert len(rows) == 1
    row = rows[0]
    assert row.bookmaker == "Mozzart"
    assert row.outcome == "1"
    assert row.previous_odds == Decimal("2.20")
    assert row.current_odds == Decimal("1.10")
    assert row.change_percent == Decimal("-50.0")
    assert row.time_delta == timedelta(minutes=3)


def test_small_change_is_not_reported():
    repository = make_repository()
    event = make_event("e2", "C", "D")

    save(repository, "e2", "Mozzart", "1", "2.20", T0)
    save(repository, "e2", "Mozzart", "1", "2.25", T0 + timedelta(minutes=3))

    rows = build_movement_report([event], repository, MARKET)

    assert rows == []


def test_only_one_reading_is_not_reported():
    repository = make_repository()
    event = make_event("e3", "E", "F")

    save(repository, "e3", "Mozzart", "1", "2.20", T0)

    rows = build_movement_report([event], repository, MARKET)

    assert rows == []


def test_move_outside_max_window_is_not_reported():
    repository = make_repository()
    event = make_event("e4", "G", "H")

    save(repository, "e4", "Mozzart", "1", "2.20", T0)
    save(repository, "e4", "Mozzart", "1", "1.10", T0 + timedelta(hours=48))

    rows = build_movement_report(
        [event],
        repository,
        MARKET,
        max_window=timedelta(hours=24),
    )

    assert rows == []


def test_reports_multiple_bookmakers_and_sorts_by_magnitude():
    repository = make_repository()
    event = make_event("e5", "I", "J")

    save(repository, "e5", "Mozzart", "1", "2.20", T0)
    save(repository, "e5", "Mozzart", "1", "1.98", T0 + timedelta(minutes=2))  # ~-10%

    save(repository, "e5", "MaxBet", "X", "3.00", T0)
    save(repository, "e5", "MaxBet", "X", "1.50", T0 + timedelta(minutes=2))  # -50%

    rows = build_movement_report([event], repository, MARKET)

    assert [r.bookmaker for r in rows] == ["MaxBet", "Mozzart"]
    assert abs(rows[0].change_percent) > abs(rows[1].change_percent)


def test_render_empty_report():
    assert render_movement_report([]) == "No significant odds movements."


def test_render_includes_key_fields():
    repository = make_repository()
    event = make_event("e1", "A", "B")

    save(repository, "e1", "Mozzart", "1", "2.20", T0)
    save(repository, "e1", "Mozzart", "1", "1.10", T0 + timedelta(minutes=3))

    rows = build_movement_report([event], repository, MARKET)
    text = render_movement_report(rows)

    assert "A vs B" in text
    assert "Mozzart" in text
    assert "-50.00%" in text
