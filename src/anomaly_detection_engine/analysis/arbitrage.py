from dataclasses import dataclass
from decimal import Decimal

from .best_odds import BestOddsResult


@dataclass(frozen=True)
class ArbitrageResult:
    margin: Decimal
    is_surebet: bool
    theoretical_profit_percent: Decimal
    # What fraction of total stake to put on each outcome so every
    # possible result pays out identically (see calculate_arbitrage's
    # own comment for the derivation) -- always computed, not just when
    # is_surebet, since the proportional-staking math itself doesn't
    # depend on margin < 1; a caller only actually needs/surfaces it in
    # the surebet case.
    stake_percent: dict[str, Decimal]


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
    # Guaranteed ROI on total stake, not (1 - margin) * 100: staking
    # stake_i = T * (1/odds_i) / margin per leg makes every outcome's
    # payout equal T/margin regardless of which one wins (that equality
    # is what "surebet" means), so guaranteed profit is T/margin - T =
    # T * (1/margin - 1) -- as a percentage of T, (1/margin - 1) * 100.
    # (1 - margin) * 100 is a different, smaller number for any
    # margin < 1 (e.g. margin=0.95: 5.0% vs the true ~5.263%) and does
    # not mean "money actually made on money actually risked".
    profit = (Decimal("1") / margin - Decimal("1")) * Decimal("100") if is_surebet else Decimal("0")
    # stake_i / T = (1/odds_i) / margin, as a percentage of total stake
    # -- the same proportional allocation the profit derivation above
    # assumes. A future report can apply this to whatever total stake it
    # likes without this layer ever needing to know a concrete currency
    # amount.
    stake_percent = {
        outcome: (Decimal("1") / best_odds[outcome].odds) / margin * Decimal("100")
        for outcome in required_outcomes
    }

    return ArbitrageResult(
        margin=margin,
        is_surebet=is_surebet,
        theoretical_profit_percent=profit,
        stake_percent=stake_percent,
    )
