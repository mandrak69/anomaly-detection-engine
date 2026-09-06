from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class MarketType(StrEnum):
    MONEYLINE = "moneyline"
    THREE_WAY = "three_way"
    TOTALS = "totals"
    HANDICAP = "handicap"


class MarketPeriod(StrEnum):
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


# Current MVP scope (see README) is limited to pre-match full-time 1X2
# markets, and the sample/source payloads do not carry explicit market
# metadata yet. Lives here (not in a collector module) since it is a
# domain default that collectors depend on, not the other way around.
DEFAULT_MARKET = MarketIdentity(
    market_type=MarketType.THREE_WAY,
    period=MarketPeriod.FULL_TIME,
)