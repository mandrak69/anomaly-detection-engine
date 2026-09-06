import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from anomaly_detection_engine.analysis.freshness import FreshnessPolicy
from anomaly_detection_engine.analysis.opportunity_detection import (
    SignalIdentity,
    detect_surebet_candidates,
    detect_value_gap_candidates,
)
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.market import (
    MarketIdentity,
    MarketPeriod,
    MarketPhase,
    MarketType,
)
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.storage.database import configure_connection, initialize_database
from anomaly_detection_engine.storage.odds_repository import OddsRepository

MARKET = MarketIdentity(
    market_type=MarketType.THREE_WAY, period=MarketPeriod.FULL_TIME, phase=MarketPhase.PRE_MATCH
)
NOW = datetime.fromisoformat("2026-08-27T10:00:00+00:00")
FRESH = FreshnessPolicy(
    max_snapshot_age=timedelta(hours=1), max_observation_spread=timedelta(hours=1)
)


def make_repository():
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
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
        competition_id="competition-1",
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

    sweep = detect_surebet_candidates(
        [event], repository, MARKET, freshness_policy=FRESH, analysis_time=NOW
    )

    assert len(sweep.candidates) == 1
    candidate = sweep.candidates[0]
    assert candidate.event.id == "e1"
    assert candidate.profit_percent == Decimal("10")
    assert len(candidate.legs) == 3
    assert {leg.outcome for leg in candidate.legs} == {"1", "X", "2"}
    # SUREBET has no per-outcome identity -- outcome=None (see SignalIdentity).
    assert sweep.evaluated_keys == frozenset({SignalIdentity("e1", MARKET, None)})


def test_detect_surebet_candidates_has_no_minimum_profit_threshold():
    # Unlike build_opportunity_report, detection itself does not filter
    # by "worth reporting" -- any real margin<1 surebet is a candidate,
    # including a mathematically-real-but-tiny one.
    repository = make_repository()
    event = make_event("e1", "A", "B")

    save(repository, "e1", "Bet1", "1", "3.001")
    save(repository, "e1", "Bet1", "X", "3.001")
    save(repository, "e1", "Bet1", "2", "3.001")

    sweep = detect_surebet_candidates(
        [event], repository, MARKET, freshness_policy=FRESH, analysis_time=NOW
    )

    assert len(sweep.candidates) == 1
    assert sweep.candidates[0].profit_percent > Decimal("0")
    assert sweep.candidates[0].profit_percent < Decimal("1")


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
    sweep = detect_surebet_candidates(
        [event], repository, MARKET, freshness_policy=strict_policy, analysis_time=NOW
    )

    assert sweep.candidates == []
    # Failed freshness means this event was *not* evaluated this sweep --
    # a caller reconciling persisted signals must not treat this the same
    # as "evaluated, genuinely no surebet".
    assert sweep.evaluated_keys == frozenset()


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

    sweep = detect_surebet_candidates(
        [event], repository, MARKET, freshness_policy=strict_policy, analysis_time=NOW
    )

    assert sweep.candidates == []
    assert sweep.evaluated_keys == frozenset()


def test_evaluated_keys_excludes_events_with_no_snapshots_yet():
    # A brand-new event with no odds saved for this market at all -- not
    # even attempted, so it must not count as "evaluated" either.
    repository = make_repository()
    event = make_event("e1", "A", "B")

    sweep = detect_surebet_candidates(
        [event], repository, MARKET, freshness_policy=FRESH, analysis_time=NOW
    )

    assert sweep.candidates == []
    assert sweep.evaluated_keys == frozenset()


def test_evaluated_keys_includes_a_fresh_event_with_no_surebet():
    # Good, fresh data that simply doesn't contain an arbitrage -- this
    # *is* a genuine "evaluated, absent" case, unlike the two tests above.
    repository = make_repository()
    event = make_event("e1", "A", "B")

    save(repository, "e1", "Bet1", "1", "1.50")
    save(repository, "e1", "Bet1", "X", "3.50")
    save(repository, "e1", "Bet1", "2", "5.00")

    sweep = detect_surebet_candidates(
        [event], repository, MARKET, freshness_policy=FRESH, analysis_time=NOW
    )

    assert sweep.candidates == []
    assert sweep.evaluated_keys == frozenset({SignalIdentity("e1", MARKET, None)})


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

    sweep = detect_value_gap_candidates(
        [event],
        repository,
        MARKET,
        freshness_policy=FRESH,
        analysis_time=NOW,
        threshold_percent=Decimal("3.0"),
    )

    assert len(sweep.candidates) == 1
    assert sweep.candidates[0].bookmaker == "BigPrice"
    assert sweep.candidates[0].outcome == "1"
    assert sweep.candidates[0].deviation_percent > Decimal("0")
    assert sweep.evaluated_keys == frozenset(
        {
            SignalIdentity("e2", MARKET, "1"),
            SignalIdentity("e2", MARKET, "X"),
            SignalIdentity("e2", MARKET, "2"),
        }
    )


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
    sweep = detect_value_gap_candidates(
        [event],
        repository,
        MARKET,
        freshness_policy=strict_policy,
        analysis_time=NOW,
        threshold_percent=Decimal("3.0"),
    )

    assert sweep.candidates == []
    assert sweep.evaluated_keys == frozenset()


def test_value_gap_evaluated_keys_are_scoped_per_outcome_not_per_event():
    # The exact scenario this granularity exists for: outcome "1" has
    # enough bookmakers to evaluate, but "X" only has one -- an event
    # passing freshness overall must not imply every one of its outcomes
    # was actually evaluated.
    repository = make_repository()
    event = make_event("e1", "A", "B")

    for bookmaker, odds in [("Bet1", "2.00"), ("Bet2", "2.05"), ("Bet3", "2.10")]:
        save(repository, "e1", bookmaker, "1", odds)
    save(repository, "e1", "Bet1", "X", "3.50")  # only one bookmaker quotes X

    sweep = detect_value_gap_candidates(
        [event],
        repository,
        MARKET,
        freshness_policy=FRESH,
        analysis_time=NOW,
        min_bookmakers=3,
    )

    assert sweep.evaluated_keys == frozenset({SignalIdentity("e1", MARKET, "1")})
