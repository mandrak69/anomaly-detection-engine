from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from anomaly_detection_engine.models.market import MarketIdentity
from anomaly_detection_engine.models.odds import OddsSnapshot


@dataclass(frozen=True)
class BookmakerLagResult:
    outcome: str
    bookmaker_name: str
    observed_at: datetime
    lag: timedelta


def detect_bookmaker_lag(
    snapshots: Iterable[OddsSnapshot],
    *,
    event_id: str,
    market: MarketIdentity,
    staleness_threshold: timedelta = timedelta(minutes=2),
) -> list[BookmakerLagResult]:
    """Flags bookmakers whose latest quote is stale relative to their peers.

    This is a relative comparison against the most recently observed
    snapshot for the same outcome, not an absolute freshness check (see
    analysis.freshness for that) -- it answers "who hasn't reacted yet
    while everyone else has", which is what MARKET_ANOMALY-style
    bookmaker-lag signals are about.
    """
    by_outcome: dict[str, list[OddsSnapshot]] = {}
    for snapshot in snapshots:
        if snapshot.event_id != event_id or snapshot.market != market:
            continue
        by_outcome.setdefault(snapshot.outcome, []).append(snapshot)

    results: list[BookmakerLagResult] = []

    for outcome, outcome_snapshots in by_outcome.items():
        if len(outcome_snapshots) < 2:
            continue

        newest_observed_at = max(s.observed_at for s in outcome_snapshots)

        for snapshot in outcome_snapshots:
            lag = newest_observed_at - snapshot.observed_at
            if lag >= staleness_threshold:
                results.append(
                    BookmakerLagResult(
                        outcome=outcome,
                        bookmaker_name=snapshot.bookmaker.name,
                        observed_at=snapshot.observed_at,
                        lag=lag,
                    )
                )

    return results
