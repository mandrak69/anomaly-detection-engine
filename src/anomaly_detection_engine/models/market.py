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


class MarketPhase(StrEnum):
    """Whether a quote was offered before kickoff or while the match is
    live -- distinct markets even when type/period/line/rules/specifier
    are otherwise identical, since a pre-match 1X2 price and a live 1X2
    price for the same event are never simultaneously valid the way
    freshness/best-odds comparisons assume. No default anywhere this is
    used: every collector must declare which phase it actually produces
    (see collectors), the same "no silent default" rule already applied
    to analysis_time/evaluated_keys.
    """

    PRE_MATCH = "pre_match"
    LIVE = "live"


@dataclass(frozen=True)
class MarketIdentity:
    market_type: MarketType
    period: MarketPeriod
    phase: MarketPhase
    line: Decimal | None = None
    rules: str | None = None
    specifier: str | None = None


# Current MVP scope (see README) is limited to full-time 1X2 markets, and
# the sample/source payloads do not carry explicit market metadata yet.
# Lives here (not in a collector module) since it is a domain default
# that collectors depend on, not the other way around. Pre-match
# specifically -- a collector reading a live endpoint (Mozzart) must not
# reuse this and instead builds its own MarketIdentity with
# phase=MarketPhase.LIVE (see LIVE_MARKET below).
DEFAULT_MARKET = MarketIdentity(
    market_type=MarketType.THREE_WAY,
    period=MarketPeriod.FULL_TIME,
    phase=MarketPhase.PRE_MATCH,
)

# Same MVP scope as DEFAULT_MARKET, but for a live-odds source
# (MozzartFileCollector reads mozzartbet.com's /live/matches). A live 1X2
# price and a pre-match 1X2 price for the same event are genuinely
# different markets -- see MarketPhase -- so this is a distinct constant,
# not DEFAULT_MARKET with a field overridden ad hoc at each call site.
LIVE_MARKET = MarketIdentity(
    market_type=MarketType.THREE_WAY,
    period=MarketPeriod.FULL_TIME,
    phase=MarketPhase.LIVE,
)
