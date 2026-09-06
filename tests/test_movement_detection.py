import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from anomaly_detection_engine.analysis.movement_detection import detect_movements
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.storage.database import configure_connection, initialize_database
from anomaly_detection_engine.storage.odds_repository import OddsRepository

MARKET = MarketIdentity(market_type=MarketType.THREE_WAY, period=MarketPeriod.FULL_TIME)
T0 = datetime.fromisoformat("2026-08-27T10:00:00+00:00")


def make_repository():
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
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


def test_candidate_carries_everything_needed_for_persistence():
    repository = make_repository()
    event = make_event("e1", "A", "B")

    save(repository, "e1", "Mozzart", "1", "2.20", T0)
    save(repository, "e1", "Mozzart", "1", "1.10", T0 + timedelta(minutes=3))

    candidates = detect_movements([event], repository, MARKET)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.event.id == "e1"
    assert candidate.market == MARKET
    assert candidate.bookmaker_id == "mozzart"
    assert candidate.bookmaker_name == "Mozzart"
    assert candidate.previous_odds == Decimal("2.20")
    assert candidate.current_odds == Decimal("1.10")
    assert candidate.previous_observed_at == T0
    assert candidate.current_observed_at == T0 + timedelta(minutes=3)
    assert candidate.change_percent == Decimal("-50.0")


def test_no_candidates_when_move_is_too_small():
    repository = make_repository()
    event = make_event("e2", "C", "D")

    save(repository, "e2", "Mozzart", "1", "2.20", T0)
    save(repository, "e2", "Mozzart", "1", "2.25", T0 + timedelta(minutes=3))

    assert detect_movements([event], repository, MARKET) == []
