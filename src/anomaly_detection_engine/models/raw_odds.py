from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.models.market import MarketIdentity


@dataclass(frozen=True)
class RawEventOdds:
    source: str
    sport: str
    league: str
    home_team: str
    away_team: str
    start_time: datetime
    observed_at: datetime
    market: MarketIdentity
    odds: dict[str, Decimal]
    source_timestamp: datetime | None = None
    # A stable bookmaker identifier from the source itself (e.g.
    # the-odds-api's per-bookmaker "key", such as "bet365"), when the
    # source provides one -- distinct from `source`, which is a display
    # name ("Bet365") that can legitimately change ("Bet365 UK") without
    # the underlying bookmaker changing. Optional: a collector with no
    # such stable identifier (the JSON demo, Mozzart) leaves this unset,
    # and Bookmaker.id falls back to a normalized form of `source` (see
    # OddsIngestionService._ingest_one) -- the same behavior as before
    # this field existed.
    source_id: str | None = None