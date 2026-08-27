from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.models.market import MarketIdentity


@dataclass(frozen=True)
class Bookmaker:
    id: str
    name: str


@dataclass(frozen=True)
class OddsSnapshot:
    event_id: str
    bookmaker: Bookmaker
    market: MarketIdentity
    outcome: str
    odds: Decimal
    observed_at: datetime
    source_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if self.odds <= Decimal("1.0"):
            raise ValueError("Decimal odds must be greater than 1.0")
