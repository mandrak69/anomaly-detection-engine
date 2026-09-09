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


def test_theoretical_profit_percent_is_guaranteed_roi_not_one_minus_margin():
    # margin = 0.95 -> guaranteed ROI is (1/0.95 - 1) * 100 =~ 5.263%,
    # not (1 - 0.95) * 100 = 5.0% -- the latter is not what a bettor
    # actually makes on money actually risked (proportional staking
    # stake_i = T * (1/odds_i) / margin makes every outcome's payout
    # T/margin regardless of which wins, so profit is T * (1/margin - 1)).
    best = {
        "1": BestOddsResult("1", Decimal("2.00"), "A"),
        "X": BestOddsResult("X", Decimal("4.00"), "B"),
        "2": BestOddsResult("2", Decimal("5.00"), "C"),
    }

    result = calculate_arbitrage(best)

    assert result.margin == Decimal("0.95")
    assert result.is_surebet is True
    assert round(result.theoretical_profit_percent, 3) == Decimal("5.263")


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
