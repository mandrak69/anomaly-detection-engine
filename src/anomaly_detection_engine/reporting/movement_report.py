from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from anomaly_detection_engine.analysis.movement_detector import detect_rapid_movement
from anomaly_detection_engine.models.event import Event
from anomaly_detection_engine.models.market import MarketIdentity
from anomaly_detection_engine.storage.odds_repository import OddsRepository


@dataclass(frozen=True)
class MovementRow:
    """One noteworthy move: who (bookmaker), where (event/outcome), how much (from -> to, %)."""

    event: str
    outcome: str
    bookmaker: str
    previous_odds: Decimal
    current_odds: Decimal
    change_percent: Decimal
    time_delta: timedelta


def build_movement_report(
    events: list[Event],
    odds_repository: OddsRepository,
    market: MarketIdentity,
    *,
    threshold_percent: Decimal = Decimal("10.0"),
    max_window: timedelta = timedelta(hours=24),
) -> list[MovementRow]:
    """Flags outcomes whose odds moved sharply between their last two readings.

    Reuses analysis.movement_detector.detect_rapid_movement per
    (event, bookmaker, outcome) pair -- this just discovers which
    combinations currently have at least two readings and applies the
    threshold across all of them, instead of comparing two snapshots by
    hand.

    max_window defaults far wider than detect_rapid_movement's own
    default (5 minutes): "rapid" there means fast *and* big, but this
    report is about any big move between two successive readings
    regardless of how far apart those polls happened to land -- a 50%
    drop discovered between readings 6 hours apart is still worth a
    line, even if it wasn't "rapid" in the narrow sense.

    Rows are sorted by the size of the move (either direction), largest
    first -- a drop (odds getting cheaper, implying the market now
    thinks that outcome more likely) is just as reportable as a rise.
    """
    rows: list[MovementRow] = []

    for event in events:
        latest = odds_repository.find_latest_for_market(
            event_id=event.id,
            market_type=market.market_type.value,
            market_period=market.period.value,
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
                market_type=market.market_type.value,
                market_period=market.period.value,
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

            rows.append(
                MovementRow(
                    event=event.display_name,
                    outcome=snapshot.outcome,
                    bookmaker=snapshot.bookmaker.name,
                    previous_odds=result.previous_odds,
                    current_odds=result.current_odds,
                    change_percent=result.change_percent,
                    time_delta=result.time_delta,
                )
            )

    rows.sort(key=lambda row: abs(row.change_percent), reverse=True)
    return rows


def render_movement_report(rows: list[MovementRow]) -> str:
    if not rows:
        return "No significant odds movements."

    header = (
        f"{'EVENT':<32} {'OUT':<4} {'BOOKMAKER':<16} "
        f"{'FROM':>6} {'TO':>6} {'CHANGE%':>8} {'ELAPSED':>8}"
    )
    lines = [header, "-" * len(header)]

    for row in rows:
        lines.append(
            f"{row.event[:32]:<32} {row.outcome:<4} {row.bookmaker[:16]:<16} "
            f"{row.previous_odds:>6.2f} {row.current_odds:>6.2f} "
            f"{row.change_percent:>7.2f}% {_format_elapsed(row.time_delta):>8}"
        )

    return "\n".join(lines)


def _format_elapsed(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"
