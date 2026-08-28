import sqlite3
from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.reporting.opportunity_report import (
    SUREBET,
    VALUE_GAP,
    build_opportunity_report,
    render_opportunity_report,
)
from anomaly_detection_engine.storage.database import initialize_database
from anomaly_detection_engine.storage.odds_repository import OddsRepository

MARKET = MarketIdentity(market_type=MarketType.THREE_WAY, period=MarketPeriod.FULL_TIME)
NOW = datetime.fromisoformat("2026-08-27T10:00:00+00:00")


def make_repository():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    return OddsRepository(connection)


def save(repository, event_id, bookmaker_name, outcome, odds):
    repository.save(
        OddsSnapshot(
            event_id=event_id,
            bookmaker=Bookmaker(bookmaker_name.lower(), bookmaker_name),
            market=MARKET,
            outcome=outcome,
            odds=Decimal(odds),
            observed_at=NOW,
        )
    )


def make_event(event_id: str, home: str, away: str) -> Event:
    return Event(
        id=event_id,
        sport="football",
        league="demo-league",
        home_team=Team(f"{event_id}-home", home),
        away_team=Team(f"{event_id}-away", away),
        start_time=NOW,
    )


def test_report_surfaces_a_real_surebet():
    repository = make_repository()
    event = make_event("e1", "A", "B")

    for bookmaker in ("Bet1", "Bet2", "Bet3"):
        save(repository, "e1", bookmaker, "1", "2.50")
        save(repository, "e1", bookmaker, "X", "4.00")
        save(repository, "e1", bookmaker, "2", "4.00")

    rows = build_opportunity_report([event], repository, MARKET)

    surebet_rows = [r for r in rows if r.signal == SUREBET]
    assert len(surebet_rows) == 3
    assert {r.outcome for r in surebet_rows} == {"1", "X", "2"}
    assert all(r.edge_percent == Decimal("10") for r in surebet_rows)
    # All three bookmakers quote identical odds here (to avoid an
    # incidental VALUE_GAP flag) so find_best_odds' tie-break keeps
    # whichever was inserted first for every outcome.
    assert {r.bookmaker for r in surebet_rows} == {"Bet1"}


def test_report_surfaces_a_value_gap_but_not_a_surebet():
    repository = make_repository()
    event = make_event("e2", "C", "D")

    for bookmaker, odds in [("Bet1", "2.00"), ("Bet2", "2.05"), ("Bet3", "2.10")]:
        save(repository, "e2", bookmaker, "1", odds)
    save(repository, "e2", "BigPrice", "1", "3.00")

    for bookmaker, odds in [("Bet1", "2.75"), ("Bet2", "2.78"), ("Bet3", "2.80")]:
        save(repository, "e2", bookmaker, "X", odds)
    for bookmaker, odds in [("Bet1", "2.65"), ("Bet2", "2.68"), ("Bet3", "2.70")]:
        save(repository, "e2", bookmaker, "2", odds)

    rows = build_opportunity_report([event], repository, MARKET)

    assert not any(r.signal == SUREBET for r in rows)

    value_rows = [r for r in rows if r.signal == VALUE_GAP]
    assert len(value_rows) == 1
    assert value_rows[0].bookmaker == "BigPrice"
    assert value_rows[0].outcome == "1"
    assert value_rows[0].edge_percent > Decimal("3.0")


def test_report_excludes_noise_below_thresholds():
    repository = make_repository()

    noisy_event = make_event("e3", "E", "F")
    for bookmaker, odds in [("Bet1", "2.00"), ("Bet2", "2.02"), ("Bet3", "2.03")]:
        save(repository, "e3", bookmaker, "1", odds)
    for bookmaker, odds in [("Bet1", "3.30"), ("Bet2", "3.32"), ("Bet3", "3.33")]:
        save(repository, "e3", bookmaker, "X", odds)
    for bookmaker, odds in [("Bet1", "3.28"), ("Bet2", "3.29"), ("Bet3", "3.30")]:
        save(repository, "e3", bookmaker, "2", odds)

    tiny_surebet_event = make_event("e4", "G", "H")
    save(repository, "e4", "TinyEdge", "1", "3.001")
    save(repository, "e4", "TinyEdge", "X", "3.001")
    save(repository, "e4", "TinyEdge", "2", "3.001")

    rows = build_opportunity_report(
        [noisy_event, tiny_surebet_event],
        repository,
        MARKET,
        min_surebet_profit_percent=Decimal("0.1"),
    )

    assert rows == []


def test_rows_sorted_by_edge_descending():
    # Two separate events, each with exactly one value-gap outcome and two
    # tight (non-signal) outcomes, sized so neither accidentally forms a
    # cross-bookmaker surebet (margin stays > 1 in both) -- verified by
    # test_report_surfaces_a_value_gap_but_not_a_surebet's pattern.
    repository = make_repository()
    event_a = make_event("eA", "A1", "A2")
    event_b = make_event("eB", "B1", "B2")

    # ~13.9% gap
    for bookmaker, odds in [("Bet1", "2.00"), ("Bet2", "2.02"), ("BigA", "2.30")]:
        save(repository, "eA", bookmaker, "1", odds)
    for bookmaker, odds in [("Bet1", "2.78"), ("Bet2", "2.79"), ("BigA", "2.80")]:
        save(repository, "eA", bookmaker, "X", odds)
    for bookmaker, odds in [("Bet1", "2.64"), ("Bet2", "2.65"), ("BigA", "2.66")]:
        save(repository, "eA", bookmaker, "2", odds)

    # ~40% gap -- larger than event A's
    for bookmaker, odds in [("Bet1", "2.00"), ("Bet2", "2.02"), ("BigB", "2.83")]:
        save(repository, "eB", bookmaker, "1", odds)
    for bookmaker, odds in [("Bet1", "2.78"), ("Bet2", "2.79"), ("BigB", "2.80")]:
        save(repository, "eB", bookmaker, "X", odds)
    for bookmaker, odds in [("Bet1", "2.64"), ("Bet2", "2.65"), ("BigB", "2.66")]:
        save(repository, "eB", bookmaker, "2", odds)

    # Event A's ~13.9% gap is below the module's default 15% threshold
    # (deliberately conservative to avoid flagging ordinary bookmaker
    # spread) -- lower it explicitly here since this test is about sort
    # order, not the default threshold choice.
    rows = build_opportunity_report(
        [event_a, event_b],
        repository,
        MARKET,
        min_value_gap_percent=Decimal("10.0"),
    )

    assert [r.signal for r in rows] == [VALUE_GAP, VALUE_GAP]
    assert [r.bookmaker for r in rows] == ["BigB", "BigA"]
    assert rows[0].edge_percent > rows[1].edge_percent


def test_render_empty_report():
    assert render_opportunity_report([]) == "No opportunities above threshold."


def test_render_includes_key_fields():
    repository = make_repository()
    event = make_event("e1", "A", "B")
    for bookmaker in ("Bet1", "Bet2", "Bet3"):
        save(repository, "e1", bookmaker, "1", "2.50")
        save(repository, "e1", bookmaker, "X", "4.00")
        save(repository, "e1", bookmaker, "2", "4.00")

    rows = build_opportunity_report([event], repository, MARKET)
    text = render_opportunity_report(rows)

    assert "SUREBET" in text
    assert "A vs B" in text
    assert "Bet1" in text or "Bet2" in text or "Bet3" in text
