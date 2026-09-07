from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.analysis.freshness import FreshnessPolicy
from anomaly_detection_engine.analysis.opportunity_detection import (
    detect_surebet_candidates,
    detect_value_gap_candidates,
)
from anomaly_detection_engine.models.event import Event
from anomaly_detection_engine.models.market import MarketIdentity
from anomaly_detection_engine.models.signal import SUREBET, VALUE_GAP
from anomaly_detection_engine.storage.odds_repository import OddsRepository

__all__ = [
    "SUREBET",
    "VALUE_GAP",
    "OpportunityRow",
    "build_opportunity_report",
    "render_opportunity_report",
]


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
    freshness_policy: FreshnessPolicy,
    analysis_time: datetime,
    min_surebet_profit_percent: Decimal = Decimal("1.0"),
    min_value_gap_percent: Decimal = Decimal("15.0"),
    min_value_gap_bookmakers: int = 3,
) -> list[OpportunityRow]:
    """Surfaces real betting opportunities and filters out noise.

    Thin presentation layer over analysis.opportunity_detection: that
    module finds every real arbitrage/outlier unconditionally (raw
    facts), this applies the "is it worth a line in the report"
    threshold on top and flattens each into a display row.

    analysis_time is required, not defaulted to real wall-clock time: it
    is the "now" freshness is measured against (see
    analysis.opportunity_detection.detect_surebet_candidates for why it
    must be the caller's actual notion of "now", not derived from the
    snapshots' own timestamps).

    freshness_policy is required, not defaulted: this report compares
    odds *across bookmakers at a point in time* (best odds, arbitrage,
    consensus deviation), which is only meaningful if those odds were
    actually simultaneously valid. Without this gate, a fast-moving
    source (a live API, a fresh manual capture) sharing an event with a
    source that has old/fixed timestamps can produce a SUREBET or
    VALUE_GAP built from odds that were never really available at the
    same time -- confirmed during development: a manual capture merged
    into a demo fixture with month-old snapshots produced exactly this.
    There's no single sensible default across deployments (it depends on
    real polling frequency), so callers must decide.

    SUREBET: kept only if the theoretical profit clears
    min_surebet_profit_percent (default 1.0%). A mathematically real
    margin of 0.1-0.2% is still noise in practice: odds can move before
    all legs are placed, stakes have to be rounded, and bookmakers
    actively limit accounts they suspect of arbitrage betting -- all of
    which can eat a thin margin before it's ever realized.

    VALUE_GAP: the default threshold matches detect_outliers' own (15%):
    with only 3-4 bookmakers, ordinary bookmaker-margin spread can
    easily clear a low bar like 3-5%, which would just fill the report
    with routine price shopping rather than real gaps.

    Rows are sorted by edge, largest first, so the most actionable items
    are at the top regardless of signal type.
    """
    rows: list[OpportunityRow] = []

    surebet_sweep = detect_surebet_candidates(
        events,
        odds_repository,
        market,
        freshness_policy=freshness_policy,
        analysis_time=analysis_time,
    )
    for surebet in surebet_sweep.candidates:
        if surebet.profit_percent < min_surebet_profit_percent:
            continue

        for leg in surebet.legs:
            rows.append(
                OpportunityRow(
                    signal=SUREBET,
                    event=surebet.event.display_name,
                    outcome=leg.outcome,
                    bookmaker=leg.bookmaker,
                    odds=leg.odds,
                    edge_percent=surebet.profit_percent,
                )
            )

    value_gap_sweep = detect_value_gap_candidates(
        events,
        odds_repository,
        market,
        freshness_policy=freshness_policy,
        analysis_time=analysis_time,
        threshold_percent=min_value_gap_percent,
        min_bookmakers=min_value_gap_bookmakers,
    )
    for gap in value_gap_sweep.candidates:
        rows.append(
            OpportunityRow(
                signal=VALUE_GAP,
                event=gap.event.display_name,
                outcome=gap.outcome,
                bookmaker=gap.bookmaker,
                odds=gap.odds,
                edge_percent=gap.deviation_percent,
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
