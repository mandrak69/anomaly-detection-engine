from dataclasses import dataclass
from decimal import Decimal

from .best_odds import BestOddsResult


@dataclass(frozen=True)
class ArbitrageResult:
    margin: Decimal
    is_surebet: bool
    theoretical_profit_percent: Decimal


def calculate_arbitrage(
    best_odds: dict[str, BestOddsResult],
    required_outcomes: tuple[str, ...] = ("1", "X", "2"),
) -> ArbitrageResult:
    missing = [outcome for outcome in required_outcomes if outcome not in best_odds]
    if missing:
        raise ValueError(f"Missing required outcomes: {', '.join(missing)}")

    margin = sum(
        (Decimal("1") / best_odds[outcome].odds for outcome in required_outcomes),
        start=Decimal("0"),
    )
    is_surebet = margin < Decimal("1")
    profit = (Decimal("1") - margin) * Decimal("100") if is_surebet else Decimal("0")

    return ArbitrageResult(
        margin=margin,
        is_surebet=is_surebet,
        theoretical_profit_percent=profit,
    )
