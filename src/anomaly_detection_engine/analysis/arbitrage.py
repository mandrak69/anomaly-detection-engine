from dataclasses import dataclass

from .best_odds import BestOddsResult


@dataclass(frozen=True)
class ArbitrageResult:
    margin: float
    is_surebet: bool
    theoretical_profit_percent: float


def calculate_arbitrage(
    best_odds: dict[str, BestOddsResult],
    required_outcomes: tuple[str, ...] = ("1", "X", "2"),
) -> ArbitrageResult:
    missing = [outcome for outcome in required_outcomes if outcome not in best_odds]
    if missing:
        raise ValueError(f"Missing required outcomes: {', '.join(missing)}")

    margin = sum(1.0 / best_odds[outcome].odds for outcome in required_outcomes)
    is_surebet = margin < 1.0
    profit = (1.0 - margin) * 100.0 if is_surebet else 0.0

    return ArbitrageResult(
        margin=margin,
        is_surebet=is_surebet,
        theoretical_profit_percent=profit,
    )
