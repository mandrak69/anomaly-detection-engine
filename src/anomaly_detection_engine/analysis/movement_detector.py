from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from anomaly_detection_engine.models.odds import OddsSnapshot


@dataclass(frozen=True)
class MovementResult:
    detected: bool
    change_percent: Decimal
    time_delta: timedelta
    previous_odds: Decimal
    current_odds: Decimal
    # True when current.quote_time < previous.quote_time -- the source's
    # own clock moved backward between these two readings (e.g. a
    # corrected/republished last_update), not a genuine price movement.
    # detected is always False when this is True.
    clock_regression: bool = False


def detect_rapid_movement(
    previous: OddsSnapshot,
    current: OddsSnapshot,
    *,
    threshold_percent: Decimal = Decimal("10.0"),
    max_window: timedelta = timedelta(minutes=5),
) -> MovementResult:
    """time_delta (and therefore whether a move counts as "rapid") is
    measured on quote_time (source_timestamp if the source provided one,
    else observed_at -- see OddsSnapshot.quote_time), not observed_at
    directly: this function answers "how fast did the *market* move",
    and the market's own clock is quote_time, not whenever our poll
    happened to land. observed_at still gates the caller-contract check
    below, since that reflects the order snapshots were actually polled
    in, independent of what either bookmaker claims about its own price.
    """
    if (
        previous.event_id != current.event_id
        or previous.bookmaker.id != current.bookmaker.id
        or previous.market != current.market
        or previous.outcome != current.outcome
    ):
        raise ValueError("Snapshots must refer to the same event/bookmaker/market/outcome")

    if current.observed_at < previous.observed_at:
        raise ValueError("Current snapshot cannot be older than previous snapshot")

    time_delta = current.quote_time - previous.quote_time
    change_percent = ((current.odds - previous.odds) / previous.odds) * Decimal("100.0")

    if time_delta.total_seconds() < 0:
        # The source's own quote_time went backward relative to itself
        # even though we polled them in the correct order (observed_at
        # already checked above) -- this is a data-quality anomaly in
        # the source's timestamps, not a real price transition. Flagged
        # distinctly rather than raising: unlike the observed_at check
        # above, this reflects a real, if messy, characteristic of the
        # data itself, not a caller passing arguments in the wrong order.
        return MovementResult(
            detected=False,
            change_percent=change_percent,
            time_delta=time_delta,
            previous_odds=previous.odds,
            current_odds=current.odds,
            clock_regression=True,
        )

    detected = abs(change_percent) >= threshold_percent and time_delta <= max_window

    return MovementResult(
        detected=detected,
        change_percent=change_percent,
        time_delta=time_delta,
        previous_odds=previous.odds,
        current_odds=current.odds,
    )
