from decimal import Decimal

from anomaly_detection_engine.analysis.arbitrage import calculate_arbitrage
from anomaly_detection_engine.analysis.best_odds import BestOddsResult


def test_detects_surebet():
    best = {
        "1": BestOddsResult("1", Decimal("2.20"), "A"),
        "X": BestOddsResult("X", Decimal("3.75"), "B"),
        "2": BestOddsResult("2", Decimal("3.60"), "C"),
    }

    result = calculate_arbitrage(best)

    assert result.is_surebet is True
    assert result.margin < 1.0
    assert result.theoretical_profit_percent > 0.0


def test_no_surebet_when_margin_above_one():
    best = {
        "1": BestOddsResult("1", Decimal("2.00"), "A"),
        "X": BestOddsResult("X", Decimal("3.20"), "B"),
        "2": BestOddsResult("2", Decimal("3.20"), "C"),
    }

    result = calculate_arbitrage(best)

    assert result.is_surebet is False
    assert result.margin > 1.0
    assert result.theoretical_profit_percent == 0.0
