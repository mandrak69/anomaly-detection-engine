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