from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.models.market import EventLifecycle, MarketIdentity


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
    # The source's own stable identifier for the *event/fixture* itself
    # (e.g. api-football's fixture.id), distinct from source_id above
    # (which identifies the bookmaker) -- optional, the same way
    # source_id is: a collector with no such stable id (the JSON demo,
    # Mozzart, the-odds-api at time of writing) leaves this unset, and
    # FixtureCatalog falls back to its existing team/competition/
    # start_time resolution, exactly as it did before this field
    # existed. When present, FixtureCatalog caches and reuses it (see
    # source_event_mappings) so a fixture already resolved once skips
    # fuzzy team matching on every later sighting.
    source_event_id: str | None = None
    # This event's real-world progress, when the source actually reports
    # it (e.g. api-football's fixture.status.short) -- None (the
    # default) means the source doesn't say, not "scheduled"; see
    # models.market.EventLifecycle for why no default is guessed here.
    lifecycle: EventLifecycle | None = None