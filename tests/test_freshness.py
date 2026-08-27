from datetime import datetime, timedelta
from decimal import Decimal

from anomaly_detection_engine.analysis.freshness import FreshnessPolicy, validate_freshness
from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot

MARKET = MarketIdentity(market_type=MarketType.THREE_WAY, period=MarketPeriod.FULL_TIME)
NOW = datetime.fromisoformat("2026-08-27T10:00:00+00:00")
POLICY = FreshnessPolicy(
    max_snapshot_age=timedelta(minutes=5),
    max_observation_spread=timedelta(minutes=2),
)


def snapshot(bookmaker_name: str, observed_at: datetime) -> OddsSnapshot:
    return OddsSnapshot(
        event_id="e1",
        bookmaker=Bookmaker(bookmaker_name.lower(), bookmaker_name),
        market=MARKET,
        outcome="1",
        odds=Decimal("2.00"),
        observed_at=observed_at,
    )


def test_valid_when_fresh_and_coherent():
    snapshots = [
        snapshot("A", NOW - timedelta(seconds=30)),
        snapshot("B", NOW - timedelta(seconds=45)),
    ]

    result = validate_freshness(snapshots, analysis_time=NOW, policy=POLICY)

    assert result.valid is True
    assert result.reason is None
    assert result.stale_sources == ()


def test_invalid_when_one_snapshot_is_stale():
    snapshots = [
        snapshot("A", NOW - timedelta(seconds=30)),
        snapshot("B", NOW - timedelta(minutes=10)),
    ]

    result = validate_freshness(snapshots, analysis_time=NOW, policy=POLICY)

    assert result.valid is False
    assert result.reason == "stale-snapshots"
    assert result.stale_sources == ("b",)


def test_invalid_when_observation_spread_too_large():
    snapshots = [
        snapshot("A", NOW - timedelta(seconds=10)),
        snapshot("B", NOW - timedelta(minutes=4)),
    ]

    result = validate_freshness(snapshots, analysis_time=NOW, policy=POLICY)

    assert result.valid is False
    assert result.reason == "observation-spread-too-large"


def test_invalid_when_snapshot_is_from_the_future():
    snapshots = [snapshot("A", NOW + timedelta(minutes=1))]

    result = validate_freshness(snapshots, analysis_time=NOW, policy=POLICY)

    assert result.valid is False
    assert result.reason == "snapshot-from-future"


def test_invalid_when_no_snapshots():
    result = validate_freshness([], analysis_time=NOW, policy=POLICY)

    assert result.valid is False
    assert result.reason == "no-snapshots"
