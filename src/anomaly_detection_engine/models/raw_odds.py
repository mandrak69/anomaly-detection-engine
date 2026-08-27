from dataclasses import dataclass
from datetime import datetime


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
    odds: dict[str, float]
    source_timestamp: datetime | None = None