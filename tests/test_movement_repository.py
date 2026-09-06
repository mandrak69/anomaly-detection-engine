import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from anomaly_detection_engine.analysis.movement_detection import MovementCandidate
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.storage.database import initialize_database
from anomaly_detection_engine.storage.movement_repository import MovementRepository

MARKET = MarketIdentity(market_type=MarketType.THREE_WAY, period=MarketPeriod.FULL_TIME)
T0 = datetime.fromisoformat("2026-08-27T10:00:00+00:00")
EVENT = Event(
    id="e1",
    sport="football",
    league="L",
    home_team=Team("h", "A"),
    away_team=Team("a", "B"),
    start_time=T0,
)


def make_repository() -> MovementRepository:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    return MovementRepository(connection)


def make_candidate(previous_observed_at=T0, current_observed_at=None) -> MovementCandidate:
    current_observed_at = current_observed_at or T0 + timedelta(minutes=3)
    return MovementCandidate(
        event=EVENT,
        market=MARKET,
        outcome="1",
        bookmaker_id="mozzart",
        bookmaker_name="Mozzart",
        previous_odds=Decimal("2.20"),
        current_odds=Decimal("1.10"),
        previous_observed_at=previous_observed_at,
        current_observed_at=current_observed_at,
        change_percent=Decimal("-50.0"),
        time_delta=current_observed_at - previous_observed_at,
    )


def test_saves_a_movement():
    repo = make_repository()
    repo.save(make_candidate(), detected_at=T0 + timedelta(minutes=3))

    records = repo.find_by_event("e1")
    assert len(records) == 1
    record = records[0]
    assert record.bookmaker_name == "Mozzart"
    assert record.previous_odds == Decimal("2.20")
    assert record.current_odds == Decimal("1.10")
    assert record.change_percent == Decimal("-50.0")


def test_saving_the_same_transition_twice_is_idempotent():
    repo = make_repository()
    candidate = make_candidate()

    repo.save(candidate, detected_at=T0 + timedelta(minutes=3))
    repo.save(candidate, detected_at=T0 + timedelta(minutes=3))

    assert len(repo.find_by_event("e1")) == 1


def test_a_different_transition_for_the_same_bookmaker_is_a_separate_row():
    repo = make_repository()

    repo.save(make_candidate(), detected_at=T0 + timedelta(minutes=3))
    later_candidate = make_candidate(
        previous_observed_at=T0 + timedelta(minutes=3),
        current_observed_at=T0 + timedelta(minutes=6),
    )
    repo.save(later_candidate, detected_at=T0 + timedelta(minutes=6))

    assert len(repo.find_by_event("e1")) == 2
