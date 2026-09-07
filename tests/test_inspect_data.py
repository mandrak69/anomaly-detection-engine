from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.models.market import DEFAULT_MARKET
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.storage.database import create_connection, initialize_database
from anomaly_detection_engine.storage.fixture_catalog import FixtureCatalog
from anomaly_detection_engine.storage.odds_repository import OddsRepository
from inspect_data import cmd_cross_provider, cmd_event, cmd_events, cmd_summary


def _connection():
    connection = create_connection(":memory:")
    initialize_database(connection)
    return connection


def test_cmd_summary_on_an_empty_database_does_not_crash(capsys):
    cmd_summary(_connection())

    output = capsys.readouterr().out
    assert "odds_snapshots:" in output
    assert "(no collector runs yet)" in output
    assert "(no signals yet)" in output


def test_cmd_events_on_an_empty_database_says_so(capsys):
    cmd_events(_connection(), limit=10)

    assert "(no events yet)" in capsys.readouterr().out


def test_cmd_event_reports_unknown_event(capsys):
    cmd_event(_connection(), "no-such-event")

    assert "No event 'no-such-event'" in capsys.readouterr().out


def test_cmd_event_shows_snapshots_for_a_real_event(capsys):
    connection = _connection()
    match = FixtureCatalog(connection, provider_id="test").match(
        sport="football",
        league="L",
        home_team_raw="Home FC",
        away_team_raw="Away FC",
        start_time=datetime.fromisoformat("2026-09-08T00:15:00+00:00"),
    )
    event = match.event

    OddsRepository(connection).save(
        OddsSnapshot(
            event_id=event.id,
            bookmaker=Bookmaker("bk-1", "Bet365"),
            market=DEFAULT_MARKET,
            outcome="1",
            odds=Decimal("2.10"),
            observed_at=datetime.fromisoformat("2026-09-07T12:00:00+00:00"),
        )
    )

    cmd_event(connection, event.id)

    output = capsys.readouterr().out
    assert "Home FC vs Away FC" in output
    assert "Bet365" in output
    assert "2.10" in output


def test_cmd_events_lists_a_created_event(capsys):
    connection = _connection()
    FixtureCatalog(connection, provider_id="test").match(
        sport="football",
        league="L",
        home_team_raw="Home FC",
        away_team_raw="Away FC",
        start_time=datetime.fromisoformat("2026-09-08T00:15:00+00:00"),
    )

    cmd_events(connection, limit=10)

    assert "Home FC vs Away FC" in capsys.readouterr().out


def test_cmd_cross_provider_finds_an_event_matched_by_two_providers(capsys):
    connection = _connection()
    start_time = datetime.fromisoformat("2026-09-08T00:15:00+00:00")

    FixtureCatalog(connection, provider_id="the-odds-api").match(
        sport="football", league="L", home_team_raw="Home FC", away_team_raw="Away FC",
        start_time=start_time,
    )
    second = FixtureCatalog(connection, provider_id="api-football").match(
        sport="football", league="L", home_team_raw="Home FC", away_team_raw="Away FC",
        start_time=start_time,
    )

    cmd_cross_provider(connection)

    output = capsys.readouterr().out
    assert second.event.id in output
    assert "the-odds-api" in output
    assert "api-football" in output


def test_cmd_cross_provider_says_so_when_nothing_qualifies(capsys):
    connection = _connection()
    FixtureCatalog(connection, provider_id="only-one-provider").match(
        sport="football", league="L", home_team_raw="Home FC", away_team_raw="Away FC",
        start_time=datetime.fromisoformat("2026-09-08T00:15:00+00:00"),
    )

    cmd_cross_provider(connection)

    assert "No event yet resolved by 2+ distinct providers." in capsys.readouterr().out
