import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from anomaly_detection_engine.analysis.opportunity_detection import (
    SUREBET,
    VALUE_GAP,
    SurebetCandidate,
    SurebetLeg,
    ValueGapCandidate,
)
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.storage.database import configure_connection, initialize_database
from anomaly_detection_engine.storage.signal_repository import (
    ACTIVE,
    RESOLVED,
    SignalRepository,
    from_surebet,
    from_value_gap,
)

MARKET = MarketIdentity(market_type=MarketType.THREE_WAY, period=MarketPeriod.FULL_TIME)
OTHER_MARKET = MarketIdentity(market_type=MarketType.TOTALS, period=MarketPeriod.FULL_TIME)
T0 = datetime.fromisoformat("2026-08-27T10:00:00+00:00")
EVENT = Event(
    id="e1",
    sport="football",
    league="L",
    home_team=Team("h", "A"),
    away_team=Team("a", "B"),
    start_time=T0,
)
EVALUATED = frozenset({"e1"})


def make_repository() -> SignalRepository:
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    return SignalRepository(connection)


def surebet_candidate(profit="10.0", bookmaker1="Bet1", market=MARKET) -> SurebetCandidate:
    return SurebetCandidate(
        event=EVENT,
        market=market,
        profit_percent=Decimal(profit),
        legs=(
            SurebetLeg("1", bookmaker1, Decimal("2.50")),
            SurebetLeg("X", "Bet2", Decimal("4.00")),
            SurebetLeg("2", "Bet3", Decimal("4.00")),
        ),
    )


def value_gap_candidate(outcome="1", deviation="20.0", market=MARKET) -> ValueGapCandidate:
    return ValueGapCandidate(
        event=EVENT,
        market=market,
        outcome=outcome,
        bookmaker="BigPrice",
        odds=Decimal("3.00"),
        deviation_percent=Decimal(deviation),
    )


def test_first_sighting_creates_an_active_signal():
    repo = make_repository()
    repo.reconcile(
        SUREBET, MARKET, [from_surebet(surebet_candidate())],
        observed_at=T0, evaluated_event_ids=EVALUATED,
    )

    active = repo.find_active(SUREBET)
    assert len(active) == 1
    record = active[0]
    assert record.status == ACTIVE
    assert record.event_id == "e1"
    assert record.edge_percent == Decimal("10.0")
    assert record.first_seen_at == T0
    assert record.last_seen_at == T0
    assert record.resolved_at is None
    assert record.details["legs"][0]["bookmaker"] == "Bet1"


def test_same_signal_seen_again_updates_last_seen_not_a_new_row():
    repo = make_repository()
    t1 = T0 + timedelta(minutes=5)

    repo.reconcile(
        SUREBET, MARKET, [from_surebet(surebet_candidate())],
        observed_at=T0, evaluated_event_ids=EVALUATED,
    )
    repo.reconcile(
        SUREBET, MARKET, [from_surebet(surebet_candidate(profit="12.0"))],
        observed_at=t1, evaluated_event_ids=EVALUATED,
    )

    active = repo.find_active(SUREBET)
    assert len(active) == 1
    record = active[0]
    assert record.first_seen_at == T0
    assert record.last_seen_at == t1
    assert record.edge_percent == Decimal("12.0")


def test_signal_missing_from_a_later_sweep_is_resolved():
    repo = make_repository()
    t1 = T0 + timedelta(minutes=5)

    repo.reconcile(
        SUREBET, MARKET, [from_surebet(surebet_candidate())],
        observed_at=T0, evaluated_event_ids=EVALUATED,
    )
    repo.reconcile(SUREBET, MARKET, [], observed_at=t1, evaluated_event_ids=EVALUATED)

    assert repo.find_active(SUREBET) == []

    connection_rows = repo._connection.execute("SELECT * FROM signals").fetchall()
    assert len(connection_rows) == 1
    assert connection_rows[0]["status"] == RESOLVED
    assert connection_rows[0]["resolved_at"] == t1.isoformat()


def test_signal_is_not_resolved_when_its_event_was_not_evaluated_this_sweep():
    # The core distinction reconcile()'s evaluated_event_ids scope exists
    # for: a signal missing from `candidates` because the underlying
    # event's data was stale/missing this sweep (not evaluated) must stay
    # ACTIVE, unlike the "genuinely absent" case above.
    repo = make_repository()
    t1 = T0 + timedelta(minutes=5)

    repo.reconcile(
        SUREBET, MARKET, [from_surebet(surebet_candidate())],
        observed_at=T0, evaluated_event_ids=EVALUATED,
    )
    # This sweep found nothing, but also couldn't evaluate "e1" at all
    # (e.g. stale/missing data) -- evaluated_event_ids is empty.
    repo.reconcile(SUREBET, MARKET, [], observed_at=t1, evaluated_event_ids=frozenset())

    active = repo.find_active(SUREBET)
    assert len(active) == 1
    assert active[0].status == ACTIVE
    assert active[0].resolved_at is None


def test_reconcile_does_not_resolve_a_signal_for_a_different_market():
    # A sweep over THREE_WAY must never resolve an ACTIVE signal that
    # belongs to a market it never analyzed (e.g. TOTALS) -- even though
    # both would otherwise match on (signal_type, event_id).
    repo = make_repository()
    t1 = T0 + timedelta(minutes=5)

    repo.reconcile(
        SUREBET, MARKET, [from_surebet(surebet_candidate())],
        observed_at=T0, evaluated_event_ids=EVALUATED,
    )
    repo.reconcile(
        SUREBET, OTHER_MARKET,
        [from_surebet(surebet_candidate(market=OTHER_MARKET))],
        observed_at=T0, evaluated_event_ids=EVALUATED,
    )

    # A THREE_WAY-only sweep that found nothing must not touch the
    # unrelated, still-active TOTALS signal.
    repo.reconcile(SUREBET, MARKET, [], observed_at=t1, evaluated_event_ids=EVALUATED)

    three_way_active = [
        r for r in repo.find_active(SUREBET) if r.market.market_type.value == "three_way"
    ]
    totals_active = [
        r for r in repo.find_active(SUREBET) if r.market.market_type.value == "totals"
    ]
    assert three_way_active == []
    assert len(totals_active) == 1


def test_reconcile_rejects_a_candidate_for_a_different_market():
    repo = make_repository()

    with pytest.raises(ValueError):
        repo.reconcile(
            SUREBET, MARKET,
            [from_surebet(surebet_candidate(market=OTHER_MARKET))],
            observed_at=T0, evaluated_event_ids=EVALUATED,
        )


def test_reconcile_rolls_back_entirely_if_a_later_candidate_is_invalid():
    # reconcile() is one transaction: if the first candidate in a list is
    # valid and would be inserted, but a later one in the *same* call
    # raises (mismatched market/signal_type), nothing from this call may
    # be persisted -- otherwise a partially applied reconcile could leave
    # some signals reflecting this sweep and others still reflecting the
    # previous one.
    repo = make_repository()

    with pytest.raises(ValueError):
        repo.reconcile(
            SUREBET,
            MARKET,
            [
                from_surebet(surebet_candidate(bookmaker1="Bet1")),
                from_surebet(surebet_candidate(market=OTHER_MARKET)),
            ],
            observed_at=T0,
            evaluated_event_ids=EVALUATED,
        )

    all_rows = repo._connection.execute("SELECT COUNT(*) AS n FROM signals").fetchone()
    assert all_rows["n"] == 0


def test_resolved_signal_reappearing_is_reactivated_not_duplicated():
    repo = make_repository()
    t1 = T0 + timedelta(minutes=5)
    t2 = T0 + timedelta(minutes=10)

    repo.reconcile(
        SUREBET, MARKET, [from_surebet(surebet_candidate())],
        observed_at=T0, evaluated_event_ids=EVALUATED,
    )
    repo.reconcile(SUREBET, MARKET, [], observed_at=t1, evaluated_event_ids=EVALUATED)
    repo.reconcile(
        SUREBET, MARKET, [from_surebet(surebet_candidate())],
        observed_at=t2, evaluated_event_ids=EVALUATED,
    )

    active = repo.find_active(SUREBET)
    assert len(active) == 1
    record = active[0]
    assert record.status == ACTIVE
    assert record.resolved_at is None
    assert record.first_seen_at == T0  # identity persisted through the resolve/reactivate
    assert record.last_seen_at == t2

    all_rows = repo._connection.execute("SELECT COUNT(*) AS n FROM signals").fetchone()
    assert all_rows["n"] == 1  # reactivated the same row, did not insert a second one


def test_resolving_one_signal_type_does_not_touch_another():
    repo = make_repository()
    t1 = T0 + timedelta(minutes=5)

    repo.reconcile(
        SUREBET, MARKET, [from_surebet(surebet_candidate())],
        observed_at=T0, evaluated_event_ids=EVALUATED,
    )
    repo.reconcile(
        VALUE_GAP, MARKET, [from_value_gap(value_gap_candidate())],
        observed_at=T0, evaluated_event_ids=EVALUATED,
    )

    # A sweep that found no surebets must not resolve the unrelated,
    # still-active value gap.
    repo.reconcile(SUREBET, MARKET, [], observed_at=t1, evaluated_event_ids=EVALUATED)

    assert repo.find_active(SUREBET) == []
    assert len(repo.find_active(VALUE_GAP)) == 1


def test_different_outcomes_are_different_value_gap_signals():
    repo = make_repository()

    repo.reconcile(
        VALUE_GAP,
        MARKET,
        [
            from_value_gap(value_gap_candidate(outcome="1")),
            from_value_gap(value_gap_candidate(outcome="X")),
        ],
        observed_at=T0,
        evaluated_event_ids=EVALUATED,
    )

    active = repo.find_active(VALUE_GAP)
    assert {record.outcome for record in active} == {"1", "X"}


def test_reconcile_rejects_mismatched_signal_type():
    repo = make_repository()

    with pytest.raises(ValueError):
        repo.reconcile(
            VALUE_GAP, MARKET, [from_surebet(surebet_candidate())],
            observed_at=T0, evaluated_event_ids=EVALUATED,
        )


def test_from_value_gap_maps_fields_into_a_signal_candidate():
    candidate = from_value_gap(value_gap_candidate(outcome="2", deviation="33.3"))

    assert candidate.signal_type == VALUE_GAP
    assert candidate.event_id == "e1"
    assert candidate.outcome == "2"
    assert candidate.edge_percent == Decimal("33.3")
    assert candidate.details == {"bookmaker": "BigPrice", "odds": "3.00"}
