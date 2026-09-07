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


def canonical_decimal(value: Decimal) -> Decimal:
    """Normalizes a Decimal to the exact form MarketIdentity stores and
    compares by, so that two numerically-equal but differently-formatted
    lines (a totals line of "2.50" from one source, "2.5" from another)
    produce the identical identity everywhere -- not just under Python's
    own `==` (already true: `Decimal.__eq__` compares value, not
    formatting), but in every SQL identity/dedupe key too, which stores
    and compares `str(line)` as plain text ("2.50" != "2.5" there).

    Plain `Decimal.normalize()` almost does this (it strips trailing
    zeros: `Decimal("2.50").normalize() == Decimal("2.5")`), but for a
    round number it can flip into exponential notation instead
    (`Decimal("100").normalize()` is `Decimal("1E+2")`) -- useless as a
    betting line's text form. Re-expressing through `format(..., "f")`
    forces fixed-point notation back, so the result is always a plain
    decimal string like a line actually looks.
    """
    return Decimal(format(value.normalize(), "f"))


@dataclass(frozen=True)
class MarketIdentity:
    market_type: MarketType
    period: MarketPeriod
    phase: MarketPhase
    line: Decimal | None = None
    rules: str | None = None
    specifier: str | None = None

    def __post_init__(self) -> None:
        # Canonicalize at construction so every MarketIdentity, from
        # every call site, is already in its comparable form -- callers
        # never need to remember to canonicalize a line or normalize an
        # empty string themselves. rules/specifier are normalized to
        # None (not "") for the same reason: SQL's `COALESCE(x, '')`
        # already treats NULL and '' as identical for dedupe/identity
        # purposes, but the Python dataclass's own `==` does not (`None
        # != ""`) unless both sides agree on one canonical "absent"
        # value here.
        if self.line is not None:
            object.__setattr__(self, "line", canonical_decimal(self.line))
        if self.rules == "":
            object.__setattr__(self, "rules", None)
        if self.specifier == "":
            object.__setattr__(self, "specifier", None)


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
