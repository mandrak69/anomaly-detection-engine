from datetime import datetime, timedelta
from decimal import Decimal

from anomaly_detection_engine.analysis.bookmaker_lag import detect_bookmaker_lag
from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot

MARKET = MarketIdentity(market_type=MarketType.THREE_WAY, period=MarketPeriod.FULL_TIME)
NOW = datetime.fromisoformat("2026-08-27T10:00:00+00:00")


def snapshot(bookmaker_name: str, outcome: str, observed_at: datetime) -> OddsSnapshot:
    return OddsSnapshot(
        event_id="e1",
        bookmaker=Bookmaker(bookmaker_name.lower(), bookmaker_name),
        market=MARKET,
        outcome=outcome,
        odds=Decimal("2.00"),
        observed_at=observed_at,
    )


def test_detects_bookmaker_lagging_behind_peers():
    snapshots = [
        snapshot("A", "1", NOW),
        snapshot("B", "1", NOW - timedelta(seconds=10)),
        snapshot("C", "1", NOW - timedelta(minutes=5)),
    ]

    results = detect_bookmaker_lag(snapshots, event_id="e1", market=MARKET)

    assert len(results) == 1
    assert results[0].bookmaker_name == "C"
    assert results[0].outcome == "1"
    assert results[0].lag == timedelta(minutes=5)


def test_no_lag_when_all_close_in_time():
    snapshots = [
        snapshot("A", "1", NOW),
        snapshot("B", "1", NOW - timedelta(seconds=20)),
        snapshot("C", "1", NOW - timedelta(seconds=40)),
    ]

    results = detect_bookmaker_lag(snapshots, event_id="e1", market=MARKET)

    assert results == []


def test_returns_empty_with_single_snapshot():
    snapshots = [snapshot("A", "1", NOW)]

    results = detect_bookmaker_lag(snapshots, event_id="e1", market=MARKET)

    assert results == []


def test_ignores_snapshots_for_other_events_and_markets():
    other_market = MarketIdentity(market_type=MarketType.TOTALS, period=MarketPeriod.FULL_TIME)

    snapshots = [
        snapshot("A", "1", NOW),
        snapshot("B", "1", NOW - timedelta(seconds=10)),
        OddsSnapshot(
            event_id="other-event",
            bookmaker=Bookmaker("c", "C"),
            market=MARKET,
            outcome="1",
            odds=Decimal("2.00"),
            observed_at=NOW - timedelta(minutes=10),
        ),
        OddsSnapshot(
            event_id="e1",
            bookmaker=Bookmaker("d", "D"),
            market=other_market,
            outcome="1",
            odds=Decimal("2.00"),
            observed_at=NOW - timedelta(minutes=10),
        ),
    ]

    results = detect_bookmaker_lag(snapshots, event_id="e1", market=MARKET)

    assert results == []


def test_custom_staleness_threshold():
    snapshots = [
        snapshot("A", "1", NOW),
        snapshot("B", "1", NOW - timedelta(seconds=30)),
    ]

    results = detect_bookmaker_lag(
        snapshots,
        event_id="e1",
        market=MARKET,
        staleness_threshold=timedelta(seconds=15),
    )

    assert len(results) == 1
    assert results[0].bookmaker_name == "B"
