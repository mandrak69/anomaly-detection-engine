import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from anomaly_detection_engine.analysis.movement_detection import detect_movements
from anomaly_detection_engine.models.collector_run import CollectorRun, CollectorRunStatus
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.market import (
    MarketIdentity,
    MarketPeriod,
    MarketPhase,
    MarketType,
)
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.storage.collector_run_repository import CollectorRunRepository
from anomaly_detection_engine.storage.database import configure_connection, initialize_database
from anomaly_detection_engine.storage.odds_repository import OddsRepository

MARKET = MarketIdentity(
    market_type=MarketType.THREE_WAY, period=MarketPeriod.FULL_TIME, phase=MarketPhase.PRE_MATCH
)
T0 = datetime.fromisoformat("2026-08-27T10:00:00+00:00")


def make_connection():
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    return connection


def make_repository():
    return OddsRepository(make_connection())


def save(repository, event_id, bookmaker_name, outcome, odds, observed_at, collector_run_id=None):
    repository.save(
        OddsSnapshot(
            event_id=event_id,
            bookmaker=Bookmaker(bookmaker_name.lower(), bookmaker_name),
            market=MARKET,
            outcome=outcome,
            odds=Decimal(odds),
            observed_at=observed_at,
            collector_run_id=collector_run_id,
        )
    )


def save_collector_run(connection, run_id: str, provider_id: str) -> None:
    CollectorRunRepository(connection).save(
        CollectorRun(
            id=run_id,
            source=provider_id,
            started_at=T0,
            finished_at=T0,
            status=CollectorRunStatus.SUCCESS,
            records_received=1,
            records_accepted=1,
            records_rejected=0,
            provider_id=provider_id,
        )
    )


def make_event(event_id: str, home: str, away: str) -> Event:
    return Event(
        id=event_id,
        sport="football",
        league="demo-league",
        competition_id="competition-1",
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


def test_no_movement_across_two_different_providers_reporting_the_same_bookmaker():
    # The canonical BookmakerCatalog can merge the-odds-api's "Bet365"
    # and api-football's "Bet365" into one Bookmaker.id -- but a big
    # difference between two providers' own readings of that bookmaker
    # is not proof the bookmaker's real price moved; it can just be two
    # feeds disagreeing or polling on different schedules. Must not be
    # reported as a movement.
    connection = make_connection()
    repository = OddsRepository(connection)
    save_collector_run(connection, "run-a", "the-odds-api")
    save_collector_run(connection, "run-b", "api-football")
    event = make_event("e3", "E", "F")

    save(repository, "e3", "Bet365", "1", "2.20", T0, collector_run_id="run-a")
    save(
        repository, "e3", "Bet365", "1", "1.10",
        T0 + timedelta(minutes=3), collector_run_id="run-b",
    )

    assert detect_movements([event], repository, MARKET) == []


def test_movement_still_detected_across_two_readings_from_the_same_provider():
    connection = make_connection()
    repository = OddsRepository(connection)
    save_collector_run(connection, "run-a1", "the-odds-api")
    save_collector_run(connection, "run-a2", "the-odds-api")
    event = make_event("e4", "G", "H")

    save(repository, "e4", "Bet365", "1", "2.20", T0, collector_run_id="run-a1")
    save(
        repository, "e4", "Bet365", "1", "1.10",
        T0 + timedelta(minutes=3), collector_run_id="run-a2",
    )

    candidates = detect_movements([event], repository, MARKET)

    assert len(candidates) == 1
    assert candidates[0].previous_odds == Decimal("2.20")
    assert candidates[0].current_odds == Decimal("1.10")


def test_movement_skips_a_different_provider_reading_and_uses_the_earlier_same_provider_one():
    # Bet365 via the-odds-api at T0, then Bet365 via api-football at
    # T0+1m (a small, unrelated blip from the other feed), then Bet365
    # via the-odds-api again at T0+3m with a big move -- the middle,
    # different-provider reading must be skipped, comparing against the
    # earlier same-provider (the-odds-api) reading instead.
    connection = make_connection()
    repository = OddsRepository(connection)
    save_collector_run(connection, "run-a1", "the-odds-api")
    save_collector_run(connection, "run-b1", "api-football")
    save_collector_run(connection, "run-a2", "the-odds-api")
    event = make_event("e5", "I", "J")

    save(repository, "e5", "Bet365", "1", "2.20", T0, collector_run_id="run-a1")
    save(
        repository,
        "e5",
        "Bet365",
        "1",
        "2.21",
        T0 + timedelta(minutes=1),
        collector_run_id="run-b1",
    )
    save(
        repository,
        "e5",
        "Bet365",
        "1",
        "1.10",
        T0 + timedelta(minutes=3),
        collector_run_id="run-a2",
    )

    candidates = detect_movements([event], repository, MARKET)

    assert len(candidates) == 1
    assert candidates[0].previous_odds == Decimal("2.20")
    assert candidates[0].current_odds == Decimal("1.10")
