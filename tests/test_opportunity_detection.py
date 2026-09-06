import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from anomaly_detection_engine.analysis.freshness import FreshnessPolicy
from anomaly_detection_engine.analysis.opportunity_detection import (
    detect_surebet_candidates,
    detect_value_gap_candidates,
)
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.storage.database import initialize_database
from anomaly_detection_engine.storage.odds_repository import OddsRepository

MARKET = MarketIdentity(market_type=MarketType.THREE_WAY, period=MarketPeriod.FULL_TIME)
NOW = datetime.fromisoformat("2026-08-27T10:00:00+00:00")
FRESH = FreshnessPolicy(
    max_snapshot_age=timedelta(hours=1), max_observation_spread=timedelta(hours=1)
)


def make_repository():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    return OddsRepository(connection)


def save(repository, event_id, bookmaker_name, outcome, odds, observed_at=NOW):
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
        start_time=NOW,
    )


def test_detect_surebet_candidates_groups_all_three_legs_into_one_candidate():
    repository = make_repository()
    event = make_event("e1", "A", "B")

    save(repository, "e1", "Bet1", "1", "2.50")
    save(repository, "e1", "Bet1", "X", "4.00")
    save(repository, "e1", "Bet1", "2", "4.00")

    candidates = detect_surebet_candidates(
        [event], repository, MARKET, freshness_policy=FRESH, analysis_time=NOW
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.event.id == "e1"
    assert candidate.profit_percent == Decimal("10")
    assert len(candidate.legs) == 3
    assert {leg.outcome for leg in candidate.legs} == {"1", "X", "2"}


def test_detect_surebet_candidates_has_no_minimum_profit_threshold():
    # Unlike build_opportunity_report, detection itself does not filter
    # by "worth reporting" -- any real margin<1 surebet is a candidate,
    # including a mathematically-real-but-tiny one.
    repository = make_repository()
    event = make_event("e1", "A", "B")

    save(repository, "e1", "Bet1", "1", "3.001")
    save(repository, "e1", "Bet1", "X", "3.001")
    save(repository, "e1", "Bet1", "2", "3.001")

    candidates = detect_surebet_candidates(
        [event], repository, MARKET, freshness_policy=FRESH, analysis_time=NOW
    )

    assert len(candidates) == 1
    assert candidates[0].profit_percent > Decimal("0")
    assert candidates[0].profit_percent < Decimal("1")


def test_detect_surebet_candidates_respects_freshness():
    repository = make_repository()
    event = make_event("e1", "A", "B")

    old_time = NOW - timedelta(days=30)
    save(repository, "e1", "StaleBook", "1", "2.50", observed_at=old_time)
    save(repository, "e1", "FreshBook1", "X", "4.00", observed_at=NOW)
    save(repository, "e1", "FreshBook2", "2", "4.00", observed_at=NOW)

    strict_policy = FreshnessPolicy(
        max_snapshot_age=timedelta(minutes=5), max_observation_spread=timedelta(minutes=5)
    )
    candidates = detect_surebet_candidates(
        [event], repository, MARKET, freshness_policy=strict_policy, analysis_time=NOW
    )

    assert candidates == []


def test_detect_surebet_candidates_flags_a_uniformly_old_batch_as_stale():
    # The exact bug analysis_time being explicit fixes: every snapshot
    # here is mutually close together (no internal spread), which is what
    # previously made analysis_time=max(observed_at within the batch)
    # trivially "fresh" relative to itself no matter how long ago the
    # whole batch actually happened. With a real, later analysis_time
    # (NOW, three hours after all of them), the same batch is correctly
    # caught as stale.
    repository = make_repository()
    event = make_event("e1", "A", "B")

    old_time = NOW - timedelta(hours=3)
    save(repository, "e1", "Bet1", "1", "2.50", observed_at=old_time)
    save(repository, "e1", "Bet2", "X", "4.00", observed_at=old_time + timedelta(seconds=30))
    save(repository, "e1", "Bet3", "2", "4.00", observed_at=old_time + timedelta(minutes=1))

    strict_policy = FreshnessPolicy(
        max_snapshot_age=timedelta(minutes=5), max_observation_spread=timedelta(minutes=5)
    )

    candidates = detect_surebet_candidates(
        [event], repository, MARKET, freshness_policy=strict_policy, analysis_time=NOW
    )

    assert candidates == []


def test_detect_value_gap_candidates_finds_favorable_outliers_only():
    repository = make_repository()
    event = make_event("e2", "C", "D")

    for bookmaker, odds in [("Bet1", "2.00"), ("Bet2", "2.05"), ("Bet3", "2.10")]:
        save(repository, "e2", bookmaker, "1", odds)
    save(repository, "e2", "BigPrice", "1", "3.00")

    for bookmaker, odds in [("Bet1", "2.75"), ("Bet2", "2.78"), ("Bet3", "2.80")]:
        save(repository, "e2", bookmaker, "X", odds)
    for bookmaker, odds in [("Bet1", "2.65"), ("Bet2", "2.68"), ("Bet3", "2.70")]:
        save(repository, "e2", bookmaker, "2", odds)

    candidates = detect_value_gap_candidates(
        [event],
        repository,
        MARKET,
        freshness_policy=FRESH,
        analysis_time=NOW,
        threshold_percent=Decimal("3.0"),
    )

    assert len(candidates) == 1
    assert candidates[0].bookmaker == "BigPrice"
    assert candidates[0].outcome == "1"
    assert candidates[0].deviation_percent > Decimal("0")


def test_detect_value_gap_candidates_respects_freshness():
    # analysis_time is explicit (NOW), not derived from the batch, so a
    # stale reading is caught even though it shares the batch with a
    # genuinely fresh one -- StaleBook here is 30 days older than NOW,
    # comfortably outside the 5-minute policy below.
    repository = make_repository()
    event = make_event("e2", "C", "D")

    old_time = NOW - timedelta(days=30)
    save(repository, "e2", "Bet1", "1", "2.00", observed_at=old_time)
    save(repository, "e2", "Bet2", "1", "2.05", observed_at=old_time)
    save(repository, "e2", "Bet3", "1", "2.10", observed_at=old_time)
    save(repository, "e2", "BigPrice", "1", "3.00", observed_at=NOW)

    strict_policy = FreshnessPolicy(
        max_snapshot_age=timedelta(minutes=5), max_observation_spread=timedelta(minutes=5)
    )
    candidates = detect_value_gap_candidates(
        [event],
        repository,
        MARKET,
        freshness_policy=strict_policy,
        analysis_time=NOW,
        threshold_percent=Decimal("3.0"),
    )

    assert candidates == []
