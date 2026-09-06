import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from anomaly_detection_engine.storage.database import configure_connection, initialize_database
from anomaly_detection_engine.storage.fixture_catalog import FixtureCatalog

T0 = datetime.fromisoformat("2026-09-01T20:00:00+00:00")


def make_connection():
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    return connection


def test_first_sighting_creates_a_new_team_and_event():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="mozzart")

    result = catalog.match(
        sport="football",
        league="Super liga Srbije",
        home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda",
        start_time=T0,
    )

    assert result.event is not None
    assert result.event.home_team.canonical_name == "Partizan"
    assert result.event.away_team.canonical_name == "Crvena Zvezda"
    assert result.confidence == 100.0


def test_same_source_same_spelling_reuses_cached_mapping_and_event():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="mozzart")

    first = catalog.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0,
    )
    second = catalog.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0,
    )

    assert first.event.id == second.event.id
    teams = connection.execute("SELECT COUNT(*) AS n FROM teams").fetchone()["n"]
    assert teams == 2  # Partizan + Crvena Zvezda, not duplicated


def test_two_different_sources_same_exact_spelling_share_one_team_and_event():
    connection = make_connection()
    mozzart = FixtureCatalog(connection, provider_id="mozzart")
    other = FixtureCatalog(connection, provider_id="the-odds-api")

    r1 = mozzart.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0,
    )
    r2 = other.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0 + timedelta(minutes=5),
    )

    assert r1.event.id == r2.event.id
    assert r1.event.home_team.id == r2.event.home_team.id


def test_alias_merges_a_never_before_seen_spelling_into_its_canonical_target():
    connection = make_connection()
    catalog = FixtureCatalog(
        connection,
        provider_id="json-demo",
        aliases={"Man Utd": "Manchester United"},
    )

    result = catalog.match(
        sport="football", league="EPL", home_team_raw="Man Utd",
        away_team_raw="Liverpool", start_time=T0,
    )

    assert result.event.home_team.canonical_name == "Manchester United"

    row = connection.execute(
        "SELECT canonical_name FROM teams WHERE id = ?",
        (result.event.home_team.id,),
    ).fetchone()
    assert row["canonical_name"] == "Manchester United"


def test_fuzzy_match_merges_similar_spelling_into_existing_team():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="src-a", fuzzy_threshold=80.0)

    catalog.match(
        sport="football", league="L", home_team_raw="Manchester United",
        away_team_raw="Liverpool", start_time=T0,
    )
    result = catalog.match(
        sport="football", league="L", home_team_raw="Manchester Utd",
        away_team_raw="Liverpool", start_time=T0 + timedelta(minutes=10),
    )

    assert result.event.home_team.canonical_name == "Manchester United"
    teams = connection.execute(
        "SELECT COUNT(*) AS n FROM teams WHERE canonical_name LIKE 'Manchester%'"
    ).fetchone()["n"]
    assert teams == 1


def test_dissimilar_spelling_below_threshold_creates_a_separate_team():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="src-a", fuzzy_threshold=90.0)

    catalog.match(
        sport="football", league="L", home_team_raw="Manchester United",
        away_team_raw="Liverpool", start_time=T0,
    )
    result = catalog.match(
        sport="football", league="L", home_team_raw="Utd of Manchester",
        away_team_raw="Liverpool", start_time=T0 + timedelta(minutes=10),
    )

    assert result.event.home_team.canonical_name == "Utd of Manchester"
    teams = connection.execute(
        "SELECT COUNT(*) AS n FROM teams WHERE sport = 'football'"
        " AND canonical_name IN ('Manchester United', 'Utd of Manchester')"
    ).fetchone()["n"]
    assert teams == 2


def test_ambiguous_fuzzy_match_creates_a_new_team_instead_of_guessing():
    connection = make_connection()
    # Seeded directly rather than via two catalog.match() calls: fuzzy
    # matching "Manchester United U21" against a catalog that only knows
    # "Manchester United" so far would itself resolve into that team
    # (a separate, orthogonal fuzzy-matching quirk) before both ever
    # coexist -- the scenario under test needs both to already exist.
    connection.execute(
        "INSERT INTO teams (id, canonical_name, sport) VALUES (?, ?, ?)",
        ("team-mu", "Manchester United", "football"),
    )
    connection.execute(
        "INSERT INTO teams (id, canonical_name, sport) VALUES (?, ?, ?)",
        ("team-mu21", "Manchester United U21", "football"),
    )
    connection.commit()

    catalog = FixtureCatalog(connection, provider_id="src-a", fuzzy_threshold=80.0)

    # "Man United" scores an almost exact tie between the two existing
    # teams above -- must not be silently merged into either.
    result = catalog.match(
        sport="football", league="L", home_team_raw="Man United",
        away_team_raw="Everton", start_time=T0,
    )

    assert result.event.home_team.canonical_name == "Man United"
    teams = connection.execute(
        "SELECT COUNT(*) AS n FROM teams WHERE canonical_name LIKE 'Man%United%'"
    ).fetchone()["n"]
    assert teams == 3


def test_same_kickoff_reported_under_different_offsets_resolves_to_one_event():
    # Two sources reporting the exact same real kickoff instant under
    # different (equally valid) UTC offsets must still resolve to one
    # canonical event -- start_time is normalized to UTC at storage the
    # same way OddsSnapshot's timestamps are.
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="src-a")

    utc_time = T0  # 2026-09-01T20:00:00+00:00
    plus_two = utc_time.astimezone(timezone(timedelta(hours=2)))
    assert plus_two.isoformat() != utc_time.isoformat()  # sanity: different text

    r1 = catalog.match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=utc_time,
    )
    r2 = catalog.match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=plus_two,
    )

    assert r1.event.id == r2.event.id


def test_same_teams_different_league_are_different_events():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="src-a")

    league_result = catalog.match(
        sport="football", league="Premier League", home_team_raw="A",
        away_team_raw="B", start_time=T0,
    )
    cup_result = catalog.match(
        sport="football", league="FA Cup", home_team_raw="A",
        away_team_raw="B", start_time=T0,
    )

    assert league_result.event.id != cup_result.event.id
    assert league_result.event.home_team.id == cup_result.event.home_team.id
    assert league_result.event.competition_id != cup_result.event.competition_id


def test_league_alias_merges_two_sources_spellings_into_one_canonical_event():
    # The exact cross-source scenario this exists for: the-odds-api says
    # "Premier League", another source says "England Premier League" --
    # same real competition, same match, must resolve to one canonical
    # event rather than two that never get compared against each other.
    connection = make_connection()
    the_odds_api = FixtureCatalog(connection, provider_id="the-odds-api")
    other_source = FixtureCatalog(
        connection,
        provider_id="other-source",
        league_aliases={"England Premier League": "Premier League"},
    )

    r1 = the_odds_api.match(
        sport="football", league="Premier League", home_team_raw="A",
        away_team_raw="B", start_time=T0,
    )
    r2 = other_source.match(
        sport="football", league="England Premier League", home_team_raw="A",
        away_team_raw="B", start_time=T0,
    )

    assert r1.event.id == r2.event.id
    assert r1.event.league == "Premier League"
    assert r2.event.league == "Premier League"
    assert r1.event.competition_id == r2.event.competition_id

    competitions = connection.execute(
        "SELECT COUNT(*) AS n FROM competitions WHERE sport = 'football'"
    ).fetchone()["n"]
    assert competitions == 1


def test_event_identity_survives_a_competition_rename_via_competition_id():
    # _find_event is keyed on competition_id, not the league display
    # string -- so if a canonical competition's name is ever renamed
    # (no rename feature exists yet, but the id already supports it),
    # a later poll for the same real match still resolves to the same
    # event instead of silently spawning a duplicate under the new name.
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="src-a")

    first = catalog.match(
        sport="football", league="Premier League", home_team_raw="A",
        away_team_raw="B", start_time=T0,
    )

    connection.execute(
        "UPDATE competitions SET canonical_name = 'EPL' WHERE id = ?",
        (first.event.competition_id,),
    )
    connection.commit()

    second = catalog.match(
        sport="football", league="Premier League", home_team_raw="A",
        away_team_raw="B", start_time=T0 + timedelta(minutes=5),
    )

    assert second.event.id == first.event.id
    assert second.event.competition_id == first.event.competition_id


def test_fuzzy_league_match_merges_a_similar_spelling():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="src-a", fuzzy_threshold=80.0)

    catalog.match(
        sport="football", league="Premier League", home_team_raw="A",
        away_team_raw="B", start_time=T0,
    )
    result = catalog.match(
        sport="football", league="Premier Leage", home_team_raw="C",
        away_team_raw="D", start_time=T0,
    )

    assert result.event.league == "Premier League"
    competitions = connection.execute(
        "SELECT COUNT(*) AS n FROM competitions WHERE sport = 'football'"
    ).fetchone()["n"]
    assert competitions == 1


def test_same_matchup_within_tolerance_reuses_the_event():
    connection = make_connection()
    catalog = FixtureCatalog(
        connection, provider_id="src-a", start_time_tolerance=timedelta(minutes=30)
    )

    r1 = catalog.match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=T0,
    )
    r2 = catalog.match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=T0 + timedelta(minutes=20),
    )

    assert r1.event.id == r2.event.id


def test_same_matchup_outside_tolerance_creates_a_new_event():
    connection = make_connection()
    catalog = FixtureCatalog(
        connection, provider_id="src-a", start_time_tolerance=timedelta(minutes=30)
    )

    r1 = catalog.match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=T0,
    )
    r2 = catalog.match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=T0 + timedelta(days=7),
    )

    assert r1.event.id != r2.event.id


def test_rejects_missing_or_identical_team_names():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="src-a")

    empty = catalog.match(
        sport="football", league="L", home_team_raw="  ", away_team_raw="B",
        start_time=T0,
    )
    same = catalog.match(
        sport="football", league="L", home_team_raw="A", away_team_raw="A",
        start_time=T0,
    )

    assert empty.event is None
    assert empty.reason == "missing-team-name"
    assert same.event is None
    assert same.reason == "home-equals-away"


def test_list_events_filters_by_sport():
    connection = make_connection()
    football = FixtureCatalog(connection, provider_id="src-a")
    basketball = FixtureCatalog(connection, provider_id="src-a")

    football.match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=T0,
    )
    basketball.match(
        sport="basketball", league="L", home_team_raw="C", away_team_raw="D",
        start_time=T0,
    )

    assert len(FixtureCatalog(connection, provider_id="x").list_events()) == 2
    football_only = FixtureCatalog(connection, provider_id="x").list_events(sport="football")
    assert len(football_only) == 1
    assert football_only[0].sport == "football"


def test_mappings_persist_across_catalog_instances_sharing_a_connection():
    connection = make_connection()

    first_run = FixtureCatalog(connection, provider_id="mozzart")
    r1 = first_run.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0,
    )

    # Simulates a later poll cycle constructing a fresh catalog instance
    # against the same (persisted) database.
    second_run = FixtureCatalog(connection, provider_id="mozzart")
    r2 = second_run.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0 + timedelta(minutes=4),
    )

    assert r1.event.id == r2.event.id
    assert r1.event.home_team.id == r2.event.home_team.id


def test_concurrent_resolution_serializes_instead_of_racing(tmp_path):
    # This project explicitly supports multiple concurrent
    # watch_capture.py-spawned processes sharing one database file --
    # ":memory:" can't be shared across connections, so this needs a
    # real file with two separate connections to stand in for two such
    # processes. Proves match()'s BEGIN IMMEDIATE actually serializes
    # them: while one holds the writer lock mid-resolution, the other's
    # match() call blocks and fails fast (a short busy_timeout, rather
    # than the default 5s) instead of racing it through the same
    # "does this team exist yet" check.
    db_path = tmp_path / "fixtures.db"
    connection_a = sqlite3.connect(str(db_path), timeout=0.1)
    configure_connection(connection_a)
    initialize_database(connection_a)
    connection_b = sqlite3.connect(str(db_path), timeout=0.1)
    configure_connection(connection_b)

    catalog_b = FixtureCatalog(connection_b, provider_id="src-b")

    connection_a.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            catalog_b.match(
                sport="football", league="L", home_team_raw="Partizan",
                away_team_raw="Crvena Zvezda", start_time=T0,
            )
    finally:
        connection_a.commit()

    # Once catalog_a's transaction releases the lock, a retry (the same
    # thing a real process would do on its next poll cycle) succeeds
    # normally.
    result = catalog_b.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0,
    )
    assert result.event is not None
    assert result.event.home_team.canonical_name == "Partizan"
