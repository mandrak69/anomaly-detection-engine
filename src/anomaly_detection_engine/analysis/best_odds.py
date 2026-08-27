from dataclasses import dataclass
from typing import Iterable

from anomaly_detection_engine.models.odds import OddsSnapshot


@dataclass(frozen=True)
class BestOddsResult:
    outcome: str
    odds: float
    bookmaker_name: str


def find_best_odds(
    snapshots: Iterable[OddsSnapshot],
    *,
    event_id: str,
    market: str,
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
            )

    return best
