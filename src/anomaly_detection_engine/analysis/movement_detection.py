from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from anomaly_detection_engine.analysis.movement_detector import detect_rapid_movement
from anomaly_detection_engine.models.event import Event
from anomaly_detection_engine.models.market import MarketIdentity
from anomaly_detection_engine.storage.odds_repository import OddsRepository


@dataclass(frozen=True)
class MovementCandidate:
    """One detected transition: a bookmaker's price for one outcome
    between its last two readings. Point-in-time, not an ongoing
    condition -- each one is its own permanent record, nothing to
    reconcile against later polls the way a stateful signal
    (SurebetCandidate/ValueGapCandidate) is.
    """

    event: Event
    market: MarketIdentity
    outcome: str
    bookmaker_id: str
    bookmaker_name: str
    previous_odds: Decimal
    current_odds: Decimal
    previous_observed_at: datetime
    current_observed_at: datetime
    change_percent: Decimal
    time_delta: timedelta


def detect_movements(
    events: list[Event],
    odds_repository: OddsRepository,
    market: MarketIdentity,
    *,
    threshold_percent: Decimal = Decimal("10.0"),
    max_window: timedelta = timedelta(hours=24),
) -> list[MovementCandidate]:
    """Finds every outcome whose odds moved sharply between their last
    two readings.

    Reuses analysis.movement_detector.detect_rapid_movement per
    (event, bookmaker, outcome) pair -- this just discovers which
    combinations currently have at least two readings and applies the
    threshold across all of them, instead of comparing two snapshots by
    hand.

    max_window defaults far wider than detect_rapid_movement's own
    default (5 minutes): "rapid" there means fast *and* big, but this is
    about any big move between two successive readings regardless of how
    far apart those polls happened to land -- a 50% drop discovered
    between readings 6 hours apart is still worth surfacing, even if it
    wasn't "rapid" in the narrow sense.
    """
    candidates: list[MovementCandidate] = []

    for event in events:
        latest = odds_repository.find_latest_for_market(
            event_id=event.id,
            market=market,
        )

        seen: set[tuple[str, str]] = set()
        for snapshot in latest:
            key = (snapshot.bookmaker.id, snapshot.outcome)
            if key in seen:
                continue
            seen.add(key)

            history = odds_repository.find_last_two(
                event_id=event.id,
                bookmaker_id=snapshot.bookmaker.id,
                market=market,
                outcome=snapshot.outcome,
            )
            if len(history) < 2:
                continue

            previous, current = history
            result = detect_rapid_movement(
                previous,
                current,
                threshold_percent=threshold_percent,
                max_window=max_window,
            )
            if not result.detected:
                continue

            candidates.append(
                MovementCandidate(
                    event=event,
                    market=market,
                    outcome=snapshot.outcome,
                    bookmaker_id=snapshot.bookmaker.id,
                    bookmaker_name=snapshot.bookmaker.name,
                    previous_odds=result.previous_odds,
                    current_odds=result.current_odds,
                    previous_observed_at=previous.observed_at,
                    current_observed_at=current.observed_at,
                    change_percent=result.change_percent,
                    time_delta=result.time_delta,
                )
            )

    return candidates
