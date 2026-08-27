from dataclasses import dataclass
from datetime import datetime


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
    odds: float
    observed_at: datetime
    source_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if self.odds <= 1.0:
            raise ValueError("Decimal odds must be greater than 1.0")
