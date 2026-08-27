from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Iterable

from anomaly_detection_engine.models.market import MarketIdentity
from anomaly_detection_engine.models.odds import OddsSnapshot


@dataclass(frozen=True)
class OutlierResult:
    outcome: str
    bookmaker_name: str
    odds: Decimal
    reference_median: Decimal
    deviation_percent: Decimal


def detect_outliers(
    snapshots: Iterable[OddsSnapshot],
    *,
    event_id: str,
    market: MarketIdentity,
    threshold_percent: Decimal = Decimal("15.0"),
    min_bookmakers: int = 3,
) -> list[OutlierResult]:
    """Flags odds that deviate sharply from the per-outcome consensus.

    A per-outcome median is only meaningful with enough independent
    observations; outcomes with fewer than `min_bookmakers` snapshots are
    skipped rather than compared against a single other bookmaker.
    """
    by_outcome: dict[str, list[OddsSnapshot]] = {}
    for snapshot in snapshots:
        if snapshot.event_id != event_id or snapshot.market != market:
            continue
        by_outcome.setdefault(snapshot.outcome, []).append(snapshot)

    results: list[OutlierResult] = []

    for outcome, outcome_snapshots in by_outcome.items():
        if len(outcome_snapshots) < min_bookmakers:
            continue

        reference_median = median(snapshot.odds for snapshot in outcome_snapshots)

        for snapshot in outcome_snapshots:
            deviation_percent = (
                (snapshot.odds - reference_median) / reference_median
            ) * Decimal("100.0")

            if abs(deviation_percent) >= threshold_percent:
                results.append(
                    OutlierResult(
                        outcome=outcome,
                        bookmaker_name=snapshot.bookmaker.name,
                        odds=snapshot.odds,
                        reference_median=reference_median,
                        deviation_percent=deviation_percent,
                    )
                )

    return results
