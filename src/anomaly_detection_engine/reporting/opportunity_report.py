from dataclasses import dataclass
from decimal import Decimal

from anomaly_detection_engine.analysis.arbitrage import calculate_arbitrage
from anomaly_detection_engine.analysis.best_odds import find_best_odds
from anomaly_detection_engine.analysis.outlier_detector import detect_outliers
from anomaly_detection_engine.models.event import Event
from anomaly_detection_engine.models.market import MarketIdentity
from anomaly_detection_engine.storage.odds_repository import OddsRepository

SUREBET = "SUREBET"
VALUE_GAP = "VALUE_GAP"


@dataclass(frozen=True)
class OpportunityRow:
    """One actionable line: who (bookmaker), where (event/outcome), how much (odds/edge)."""

    signal: str
    event: str
    outcome: str
    bookmaker: str
    odds: Decimal
    edge_percent: Decimal


def build_opportunity_report(
    events: list[Event],
    odds_repository: OddsRepository,
    market: MarketIdentity,
    *,
    min_surebet_profit_percent: Decimal = Decimal("1.0"),
    min_value_gap_percent: Decimal = Decimal("15.0"),
    min_value_gap_bookmakers: int = 3,
) -> list[OpportunityRow]:
    """Surfaces real betting opportunities and filters out noise.

    Two signal types, both already-vetted analysis modules -- this just
    applies a "is it worth a line in the report" threshold on top:

    SUREBET: an actual arbitrage (calculate_arbitrage.is_surebet), but
    only kept if the theoretical profit clears min_surebet_profit_percent
    (default 1.0%). A mathematically real margin of 0.1-0.2% is still
    noise in practice: odds can move before all legs are placed, stakes
    have to be rounded, and bookmakers actively limit accounts they
    suspect of arbitrage betting -- all of which can eat a thin margin
    before it's ever realized.

    VALUE_GAP: one bookmaker pricing an outcome well above the consensus
    of its peers (detect_outliers, restricted to the favorable direction
    only -- an outlier priced *below* consensus is a bad price, not an
    opportunity). The default threshold matches detect_outliers' own
    (15%): with only 3-4 bookmakers, ordinary bookmaker-margin spread
    can easily clear a low bar like 3-5%, which would just fill the
    report with routine price shopping rather than real gaps.

    Rows are sorted by edge, largest first, so the most actionable items
    are at the top regardless of signal type.
    """
    rows: list[OpportunityRow] = []

    for event in events:
        snapshots = odds_repository.find_latest_for_market(
            event_id=event.id,
            market_type=market.market_type.value,
            market_period=market.period.value,
        )
        if not snapshots:
            continue

        best = find_best_odds(snapshots, event_id=event.id, market=market)
        if len(best) == 3:
            arbitrage = calculate_arbitrage(best)
            if arbitrage.is_surebet and arbitrage.theoretical_profit_percent >= min_surebet_profit_percent:
                for outcome, item in best.items():
                    rows.append(
                        OpportunityRow(
                            signal=SUREBET,
                            event=event.display_name,
                            outcome=outcome,
                            bookmaker=item.bookmaker_name,
                            odds=item.odds,
                            edge_percent=arbitrage.theoretical_profit_percent,
                        )
                    )

        for outlier in detect_outliers(
            snapshots,
            event_id=event.id,
            market=market,
            threshold_percent=min_value_gap_percent,
            min_bookmakers=min_value_gap_bookmakers,
        ):
            if outlier.deviation_percent <= 0:
                continue

            rows.append(
                OpportunityRow(
                    signal=VALUE_GAP,
                    event=event.display_name,
                    outcome=outlier.outcome,
                    bookmaker=outlier.bookmaker_name,
                    odds=outlier.odds,
                    edge_percent=outlier.deviation_percent,
                )
            )

    rows.sort(key=lambda row: row.edge_percent, reverse=True)
    return rows


def render_opportunity_report(rows: list[OpportunityRow]) -> str:
    if not rows:
        return "No opportunities above threshold."

    header = f"{'SIGNAL':<10} {'EVENT':<32} {'OUT':<4} {'BOOKMAKER':<16} {'ODDS':>6} {'EDGE%':>7}"
    lines = [header, "-" * len(header)]

    for row in rows:
        lines.append(
            f"{row.signal:<10} {row.event[:32]:<32} {row.outcome:<4} "
            f"{row.bookmaker[:16]:<16} {row.odds:>6.2f} {row.edge_percent:>6.2f}%"
        )

    return "\n".join(lines)
