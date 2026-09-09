from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.models.market import MarketIdentity
from anomaly_detection_engine.models.odds import OddsSnapshot


@dataclass(frozen=True)
class BestOddsResult:
    outcome: str
    odds: Decimal
    bookmaker_name: str
    # Provenance carried through from the winning OddsSnapshot, so a
    # signal built from this (see pipeline.from_surebet/from_value_gap)
    # can be traced back to exactly which quote/run/provider it came
    # from, not just "some bookmaker named X" -- see
    # OddsSnapshot.collector_run_id/quote_time. All default None only
    # for direct test construction of a BestOddsResult without a real
    # backing snapshot; find_best_odds always populates them.
    bookmaker_id: str | None = None
    quote_time: datetime | None = None
    collector_run_id: str | None = None


def find_best_odds(
    snapshots: Iterable[OddsSnapshot],
    *,
    event_id: str,
    market: MarketIdentity,
) -> dict[str, BestOddsResult]:
    best: dict[str, BestOddsResult] = {}

    for snapshot in snapshots:
        if snapshot.event_id != event_id or snapshot.market != market:
            continue

        current = best.get(snapshot.outcome)
        if current is None or snapshot.odds > current.odds:
            best[snapshot.outcome] = BestOddsResult(
                outcome=snapshot.outcome,
                odds=snapshot.odds,
                bookmaker_name=snapshot.bookmaker.name,
                bookmaker_id=snapshot.bookmaker.id,
                quote_time=snapshot.quote_time,
                collector_run_id=snapshot.collector_run_id,
            )

    return best
