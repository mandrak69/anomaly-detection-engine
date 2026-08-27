from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class MarketType(str, Enum):
    MONEYLINE = "moneyline"
    THREE_WAY = "three_way"
    TOTALS = "totals"
    HANDICAP = "handicap"


class MarketPeriod(str, Enum):
    FULL_TIME = "full_time"
    FIRST_HALF = "first_half"
    SECOND_HALF = "second_half"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MarketIdentity:
    market_type: MarketType
    period: MarketPeriod
    line: Decimal | None = None
    rules: str | None = None
    specifier: str | None = None