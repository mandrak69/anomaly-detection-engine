from dataclasses import dataclass
from datetime import timedelta

from anomaly_detection_engine.models.odds import OddsSnapshot


@dataclass(frozen=True)
class MovementResult:
    detected: bool
    change_percent: float
    time_delta: timedelta
    previous_odds: float
    current_odds: float


def detect_rapid_movement(
    previous: OddsSnapshot,
    current: OddsSnapshot,
    *,
    threshold_percent: float = 10.0,
    max_window: timedelta = timedelta(minutes=5),
) -> MovementResult:
    if (
        previous.event_id != current.event_id
        or previous.bookmaker.id != current.bookmaker.id
        or previous.market != current.market
        or previous.outcome != current.outcome
    ):
        raise ValueError("Snapshots must refer to the same event/bookmaker/market/outcome")

    time_delta = current.observed_at - previous.observed_at

    if time_delta.total_seconds() < 0:
        raise ValueError("Current snapshot cannot be older than previous snapshot")

    change_percent = (
        (current.odds - previous.odds) / previous.odds
    ) * 100.0

    detected = (
        abs(change_percent) >= threshold_percent
        and time_delta <= max_window
    )

    return MovementResult(
        detected=detected,
        change_percent=change_percent,
        time_delta=time_delta,
        previous_odds=previous.odds,
        current_odds=current.odds,
    )