from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class MarketType(StrEnum):
    MONEYLINE = "moneyline"
    THREE_WAY = "three_way"
    TOTALS = "totals"
    HANDICAP = "handicap"


# Every outcome a market of this type must have fully quoted before a
# surebet/arbitrage check makes sense (see analysis.opportunity_detection.
# detect_surebet_candidates and analysis.arbitrage.calculate_arbitrage,
# whose margin formula sums over exactly these outcomes) -- THREE_WAY
# needs all of 1/X/2 priced, TOTALS needs both OVER/UNDER, HANDICAP (the
# 3-way "Handicap Result" flavor this project detects, see
# HANDICAP_MINUS_1_MARKET below -- not 2-way Asian Handicap, deliberately
# not modeled yet) also needs all of 1/X/2. Lives here, not in analysis/,
# since it is a fact about what a market type *is*, not a
# detection-specific policy; VALUE_GAP detection has no equivalent need
# (it evaluates whichever outcomes are present, individually).
REQUIRED_OUTCOMES: dict[MarketType, tuple[str, ...]] = {
    MarketType.THREE_WAY: ("1", "X", "2"),
    MarketType.TOTALS: ("OVER", "UNDER"),
    MarketType.HANDICAP: ("1", "X", "2"),
}


def required_outcomes(market_type: MarketType) -> tuple[str, ...]:
    """Looks up REQUIRED_OUTCOMES, raising ValueError (not KeyError, and
    not silently treating an unmapped type as "any outcomes will do")
    for a market_type with no known outcome set yet -- e.g. MONEYLINE,
    not wired into detection yet even though the enum value already
    exists.
    """
    try:
        return REQUIRED_OUTCOMES[market_type]
    except KeyError:
        raise ValueError(
            f"No known required-outcomes set for market_type={market_type!r} -- "
            f"add one to REQUIRED_OUTCOMES before detecting surebets for it."
        ) from None


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

# The second market type this project detects, added specifically to
# prove MarketIdentity/detection generalize beyond THREE_WAY rather than
# happening to only work for it. Deliberately only the 2.5 goals line for
# now, not every line a source might offer (e.g. api-football.com's
# "Goals Over/Under" bet bundles many lines -- 0.5, 1.5, 2.5, 3.5, ... --
# into one response; this project only extracts 2.5 from it) -- the same
# narrow-MVP-first discipline DEFAULT_MARKET/LIVE_MARKET already follow.
TOTALS_2_5_MARKET = MarketIdentity(
    market_type=MarketType.TOTALS,
    period=MarketPeriod.FULL_TIME,
    phase=MarketPhase.PRE_MATCH,
    line=Decimal("2.5"),
)

# The third market type this project detects, chosen deliberately as the
# *3-way* "Handicap Result" flavor (Home/Draw/Away still all possible,
# just with a fixed number of goals subtracted from the home side before
# settlement) rather than 2-way Asian Handicap: Asian Handicap's own
# real response shape (api-football.com's "Asian Handicap" bet) labels
# each side's line independently ("Home -0.5" and "Away -0.5" both
# appear, rather than a complementary "Home -0.5"/"Away +0.5" pair), and
# getting that pairing semantically right needs more care than this
# round's scope -- "Handicap Result" avoids the ambiguity entirely by
# reusing THREE_WAY's exact 1/X/2 outcome shape (see REQUIRED_OUTCOMES
# above), just at a specific handicap line. -1 (home team's goals
# reduced by 1 for settlement) is a commonly-offered whole-goal line,
# same "pick one concrete line" discipline TOTALS_2_5_MARKET already
# follows -- api-football.com's "Handicap Result" bet bundles many lines
# ("Home -1"/"Draw -1"/"Away -1", "Home -2"/..., "Home +1"/...) into one
# response the same way "Goals Over/Under" does for totals.
HANDICAP_MINUS_1_MARKET = MarketIdentity(
    market_type=MarketType.HANDICAP,
    period=MarketPeriod.FULL_TIME,
    phase=MarketPhase.PRE_MATCH,
    line=Decimal("-1"),
)
