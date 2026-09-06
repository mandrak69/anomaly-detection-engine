import sqlite3
from datetime import datetime, timedelta, timezone

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
    catalog = FixtureCatalog(connection, source="mozzart")

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
    catalog = FixtureCatalog(connection, source="mozzart")

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
    mozzart = FixtureCatalog(connection, source="mozzart")
    other = FixtureCatalog(connection, source="the-odds-api")

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
        source="json-demo",
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
    catalog = FixtureCatalog(connection, source="src-a", fuzzy_threshold=80.0)

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
    catalog = FixtureCatalog(connection, source="src-a", fuzzy_threshold=90.0)

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

    catalog = FixtureCatalog(connection, source="src-a", fuzzy_threshold=80.0)

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
    catalog = FixtureCatalog(connection, source="src-a")

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
    catalog = FixtureCatalog(connection, source="src-a")

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


def test_same_matchup_within_tolerance_reuses_the_event():
    connection = make_connection()
    catalog = FixtureCatalog(
        connection, source="src-a", start_time_tolerance=timedelta(minutes=30)
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
        connection, source="src-a", start_time_tolerance=timedelta(minutes=30)
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
    catalog = FixtureCatalog(connection, source="src-a")

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
    football = FixtureCatalog(connection, source="src-a")
    basketball = FixtureCatalog(connection, source="src-a")

    football.match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=T0,
    )
    basketball.match(
        sport="basketball", league="L", home_team_raw="C", away_team_raw="D",
        start_time=T0,
    )

    assert len(FixtureCatalog(connection, source="x").list_events()) == 2
    football_only = FixtureCatalog(connection, source="x").list_events(sport="football")
    assert len(football_only) == 1
    assert football_only[0].sport == "football"


def test_mappings_persist_across_catalog_instances_sharing_a_connection():
    connection = make_connection()

    first_run = FixtureCatalog(connection, source="mozzart")
    r1 = first_run.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0,
    )

    # Simulates a later poll cycle constructing a fresh catalog instance
    # against the same (persisted) database.
    second_run = FixtureCatalog(connection, source="mozzart")
    r2 = second_run.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0 + timedelta(minutes=4),
    )

    assert r1.event.id == r2.event.id
    assert r1.event.home_team.id == r2.event.home_team.id
