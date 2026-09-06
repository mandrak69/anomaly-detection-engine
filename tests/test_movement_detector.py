from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from anomaly_detection_engine.analysis.movement_detector import detect_rapid_movement
from anomaly_detection_engine.models.market import (
    MarketIdentity,
    MarketPeriod,
    MarketPhase,
    MarketType,
)
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot

MARKET = MarketIdentity(
    market_type=MarketType.THREE_WAY,
    period=MarketPeriod.FULL_TIME,
    phase=MarketPhase.PRE_MATCH,
)


def test_detects_rapid_odds_movement():
    bookmaker = Bookmaker("mozzart", "Mozzart")

    previous = OddsSnapshot(
        event_id="event-001",
        bookmaker=bookmaker,
        market=MARKET,
        outcome="1",
        odds=Decimal("2.20"),
        observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
    )

    current = OddsSnapshot(
        event_id="event-001",
        bookmaker=bookmaker,
        market=MARKET,
        outcome="1",
        odds=Decimal("1.90"),
        observed_at=datetime.fromisoformat("2026-08-27T08:02:00+00:00"),
    )

    result = detect_rapid_movement(previous, current)

    assert result.detected is True
    assert result.change_percent < -10.0


def test_does_not_detect_small_movement():
    bookmaker = Bookmaker("mozzart", "Mozzart")

    previous = OddsSnapshot(
        event_id="event-001",
        bookmaker=bookmaker,
        market=MARKET,
        outcome="1",
        odds=Decimal("2.20"),
        observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
    )

    current = OddsSnapshot(
        event_id="event-001",
        bookmaker=bookmaker,
        market=MARKET,
        outcome="1",
        odds=Decimal("2.10"),
        observed_at=datetime.fromisoformat("2026-08-27T08:02:00+00:00"),
    )

    result = detect_rapid_movement(previous, current)

    assert result.detected is False


def test_time_delta_is_measured_on_quote_time_not_observed_at():
    # "How fast did the market move" is answered by quote_time (the
    # source's own last_update), not observed_at (when we happened to
    # poll) -- a movement genuinely 90 seconds apart by the source's own
    # clock must be measured as 90 seconds even if our two polls landed
    # further apart than that.
    bookmaker = Bookmaker("bet365", "Bet365")
    observed_at_1 = datetime.fromisoformat("2026-08-27T08:00:00+00:00")
    observed_at_2 = datetime.fromisoformat("2026-08-27T08:10:00+00:00")  # 10 min apart by poll

    previous = OddsSnapshot(
        event_id="event-001",
        bookmaker=bookmaker,
        market=MARKET,
        outcome="1",
        odds=Decimal("2.20"),
        observed_at=observed_at_1,
        source_timestamp=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
    )
    current = OddsSnapshot(
        event_id="event-001",
        bookmaker=bookmaker,
        market=MARKET,
        outcome="1",
        odds=Decimal("1.90"),
        observed_at=observed_at_2,
        # ...but only 90 seconds apart by the source's own last_update.
        source_timestamp=datetime.fromisoformat("2026-08-27T08:01:30+00:00"),
    )

    result = detect_rapid_movement(previous, current, max_window=timedelta(minutes=2))

    assert result.time_delta == timedelta(seconds=90)
    assert result.detected is True  # within max_window on quote_time, even though polls weren't


def test_source_clock_regression_is_flagged_not_raised_or_treated_as_movement():
    # The source's own quote_time went backward between two readings
    # (e.g. a corrected/republished last_update) even though we polled
    # them in the correct order -- a data-quality anomaly, not a real
    # price transition and not a caller bug, so this must not raise and
    # must not be reported as a detected movement.
    bookmaker = Bookmaker("bet365", "Bet365")

    previous = OddsSnapshot(
        event_id="event-001",
        bookmaker=bookmaker,
        market=MARKET,
        outcome="1",
        odds=Decimal("2.20"),
        observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
        source_timestamp=datetime.fromisoformat("2026-08-27T08:05:00+00:00"),
    )
    current = OddsSnapshot(
        event_id="event-001",
        bookmaker=bookmaker,
        market=MARKET,
        outcome="1",
        odds=Decimal("1.50"),
        observed_at=datetime.fromisoformat("2026-08-27T08:10:00+00:00"),  # polled later, correctly
        source_timestamp=datetime.fromisoformat("2026-08-27T08:03:00+00:00"),  # but claims earlier
    )

    result = detect_rapid_movement(previous, current)

    assert result.detected is False
    assert result.clock_regression is True


def test_observed_at_out_of_order_still_raises():
    # This is the genuine caller-contract violation (arguments passed in
    # the wrong order), distinct from the source-clock-regression case
    # above -- observed_at reflects the order we actually polled in,
    # which the caller controls, unlike a source's self-reported
    # quote_time.
    bookmaker = Bookmaker("mozzart", "Mozzart")

    previous = OddsSnapshot(
        event_id="event-001",
        bookmaker=bookmaker,
        market=MARKET,
        outcome="1",
        odds=Decimal("2.20"),
        observed_at=datetime.fromisoformat("2026-08-27T08:10:00+00:00"),
    )
    current = OddsSnapshot(
        event_id="event-001",
        bookmaker=bookmaker,
        market=MARKET,
        outcome="1",
        odds=Decimal("1.90"),
        observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
    )

    with pytest.raises(ValueError, match="cannot be older"):
        detect_rapid_movement(previous, current)
