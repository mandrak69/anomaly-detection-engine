from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.analysis.outlier_detector import detect_outliers
from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot

MARKET = MarketIdentity(market_type=MarketType.THREE_WAY, period=MarketPeriod.FULL_TIME)
NOW = datetime.fromisoformat("2026-08-27T08:00:00+00:00")


def snapshot(bookmaker_name: str, outcome: str, odds: str) -> OddsSnapshot:
    return OddsSnapshot(
        event_id="e1",
        bookmaker=Bookmaker(bookmaker_name.lower(), bookmaker_name),
        market=MARKET,
        outcome=outcome,
        odds=Decimal(odds),
        observed_at=NOW,
    )


def test_detects_odds_far_above_consensus():
    snapshots = [
        snapshot("A", "1", "2.00"),
        snapshot("B", "1", "2.05"),
        snapshot("C", "1", "2.10"),
        snapshot("D", "1", "3.50"),
    ]

    results = detect_outliers(snapshots, event_id="e1", market=MARKET)

    assert len(results) == 1
    assert results[0].bookmaker_name == "D"
    assert results[0].outcome == "1"
    assert results[0].deviation_percent > 0


def test_detects_odds_far_below_consensus():
    snapshots = [
        snapshot("A", "1", "2.00"),
        snapshot("B", "1", "2.05"),
        snapshot("C", "1", "2.10"),
        snapshot("D", "1", "1.20"),
    ]

    results = detect_outliers(snapshots, event_id="e1", market=MARKET)

    assert len(results) == 1
    assert results[0].bookmaker_name == "D"
    assert results[0].deviation_percent < 0


def test_no_outliers_when_odds_are_close():
    snapshots = [
        snapshot("A", "1", "2.00"),
        snapshot("B", "1", "2.05"),
        snapshot("C", "1", "2.10"),
    ]

    results = detect_outliers(snapshots, event_id="e1", market=MARKET)

    assert results == []


def test_skips_outcomes_with_too_few_bookmakers():
    snapshots = [
        snapshot("A", "1", "2.00"),
        snapshot("B", "1", "10.00"),
    ]

    results = detect_outliers(snapshots, event_id="e1", market=MARKET, min_bookmakers=3)

    assert results == []


def test_ignores_snapshots_for_other_events_and_markets():
    other_market = MarketIdentity(market_type=MarketType.TOTALS, period=MarketPeriod.FULL_TIME)

    snapshots = [
        snapshot("A", "1", "2.00"),
        snapshot("B", "1", "2.05"),
        snapshot("C", "1", "2.10"),
        OddsSnapshot(
            event_id="other-event",
            bookmaker=Bookmaker("e", "E"),
            market=MARKET,
            outcome="1",
            odds=Decimal("50.00"),
            observed_at=NOW,
        ),
        OddsSnapshot(
            event_id="e1",
            bookmaker=Bookmaker("f", "F"),
            market=other_market,
            outcome="1",
            odds=Decimal("50.00"),
            observed_at=NOW,
        ),
    ]

    results = detect_outliers(snapshots, event_id="e1", market=MARKET)

    assert results == []
