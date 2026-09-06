from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from anomaly_detection_engine.analysis.movement_detection import detect_movements
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
    """Flattens analysis.movement_detection.detect_movements into display
    rows, sorted by the size of the move (either direction), largest
    first -- a drop (odds getting cheaper, implying the market now thinks
    that outcome more likely) is just as reportable as a rise.

    Unlike opportunity_report, this does not take a FreshnessPolicy.
    Freshness there guards against comparing odds *across bookmakers*
    that were never simultaneously valid; this always compares one
    bookmaker against its own earlier reading, and max_window already
    bounds how far apart those two readings can be -- a pair further
    apart than max_window is excluded by detect_rapid_movement itself,
    so there is no equivalent gap here to close.
    """
    rows = [
        MovementRow(
            event=candidate.event.display_name,
            outcome=candidate.outcome,
            bookmaker=candidate.bookmaker_name,
            previous_odds=candidate.previous_odds,
            current_odds=candidate.current_odds,
            change_percent=candidate.change_percent,
            time_delta=candidate.time_delta,
        )
        for candidate in detect_movements(
            events,
            odds_repository,
            market,
            threshold_percent=threshold_percent,
            max_window=max_window,
        )
    ]

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
