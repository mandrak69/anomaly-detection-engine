from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.analysis.best_odds import find_best_odds
from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot

MARKET = MarketIdentity(
    market_type=MarketType.THREE_WAY,
    period=MarketPeriod.FULL_TIME,
)


def test_finds_best_odds_per_outcome():
    now = datetime.fromisoformat("2026-08-27T08:00:00+00:00")
    a = Bookmaker("a", "A")
    b = Bookmaker("b", "B")

    snapshots = [
        OddsSnapshot("e1", a, MARKET, "1", Decimal("2.00"), now),
        OddsSnapshot("e1", b, MARKET, "1", Decimal("2.20"), now),
        OddsSnapshot("e1", a, MARKET, "X", Decimal("3.50"), now),
        OddsSnapshot("e1", b, MARKET, "X", Decimal("3.40"), now),
        OddsSnapshot("e1", a, MARKET, "2", Decimal("3.10"), now),
        OddsSnapshot("e1", b, MARKET, "2", Decimal("3.60"), now),
    ]

    result = find_best_odds(snapshots, event_id="e1", market=MARKET)

    assert result["1"].odds == Decimal("2.20")
    assert result["1"].bookmaker_name == "B"
    assert result["X"].odds == Decimal("3.50")
    assert result["2"].odds == Decimal("3.60")
