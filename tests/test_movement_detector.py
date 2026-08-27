from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.analysis.movement_detector import detect_rapid_movement
from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot

MARKET = MarketIdentity(
    market_type=MarketType.THREE_WAY,
    period=MarketPeriod.FULL_TIME,
)


def test_detects_rapid_odds_movement():
    bookmaker = Bookmaker("mozzart", "Mozzart")

    previous = OddsSnapshot(
        event_id="event-001",
        bookmaker=bookmaker,
        market=MARKET,
        outcome="1",
        odds=Decimal("2.20"),
        observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
    )

    current = OddsSnapshot(
        event_id="event-001",
        bookmaker=bookmaker,
        market=MARKET,
        outcome="1",
        odds=Decimal("1.90"),
        observed_at=datetime.fromisoformat("2026-08-27T08:02:00+00:00"),
    )

    result = detect_rapid_movement(previous, current)

    assert result.detected is True
    assert result.change_percent < -10.0


def test_does_not_detect_small_movement():
    bookmaker = Bookmaker("mozzart", "Mozzart")

    previous = OddsSnapshot(
        event_id="event-001",
        bookmaker=bookmaker,
        market=MARKET,
        outcome="1",
        odds=Decimal("2.20"),
        observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
    )

    current = OddsSnapshot(
        event_id="event-001",
        bookmaker=bookmaker,
        market=MARKET,
        outcome="1",
        odds=Decimal("2.10"),
        observed_at=datetime.fromisoformat("2026-08-27T08:02:00+00:00"),
    )

    result = detect_rapid_movement(previous, current)

    assert result.detected is False
