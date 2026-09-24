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
    # The source's own stable identifier for the home/away team and the
    # competition/league themselves (e.g. api-football's teams.home.id,
    # league.id) -- distinct from source_event_id above (the whole
    # fixture) the same way source_id (the bookmaker) is. Several real
    # sources report these right next to the display name and the
    # collector was simply discarding them in favor of fuzzy-matching the
    # name from scratch on every sighting; when present, FixtureCatalog
    # caches and reuses them (see source_team_id_mappings /
    # source_competition_id_mappings) so a team or competition already
    # resolved once by id skips fuzzy matching entirely on every later
    # sighting, the same way source_event_id already does for the whole
    # fixture. A source with no such stable id (the-odds-api's teams, the
    # JSON demo) leaves these unset, and resolution falls back to the
    # existing name-based path exactly as it did before these fields
    # existed.
    source_home_team_id: str | None = None
    source_away_team_id: str | None = None
    source_competition_id: str | None = None
    # The competition's real-world country/region, when the source
    # reports one alongside the league name -- distinct from folding it
    # into `league` itself (what api_football_collector.py and
    # meridianbet_file_collector.py already do as a stop-gap to stop
    # same-named leagues from different countries colliding). Optional:
    # a source with no such field (Mozzart, the-odds-api) leaves this
    # unset rather than guessing one.
    country: str | None = None