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


def test_source_event_id_is_cached_and_reused_on_a_later_sighting():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="api-football")

    first = catalog.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0, source_event_id="12345",
    )
    mappings = connection.execute(
        "SELECT COUNT(*) AS n FROM source_event_mappings"
    ).fetchone()["n"]
    assert mappings == 1

    # Even with different (wrong-on-purpose) team names/start_time, the
    # cached source_event_id mapping wins outright -- this is exactly
    # the fast path's point: a fixture already resolved once is trusted
    # without re-running fuzzy team/competition resolution at all.
    second = catalog.match(
        sport="football", league="L", home_team_raw="Some Other Name",
        away_team_raw="Yet Another Name", start_time=T0 + timedelta(days=30),
        source_event_id="12345",
    )

    assert second.event.id == first.event.id
    assert second.confidence == 100.0
    # No new team rows created for "Some Other Name"/"Yet Another Name".
    teams = connection.execute("SELECT COUNT(*) AS n FROM teams").fetchone()["n"]
    assert teams == 2


def test_source_event_id_fast_path_syncs_a_rescheduled_kickoff():
    # Signal expiry keys directly off events.start_time (see
    # SignalRepository.expire_active_signals) -- a provider genuinely
    # postponing a fixture must update the canonical event's start_time,
    # not leave it pointing at a kickoff that no longer happens, even
    # though the fast path otherwise trusts source_event_id outright and
    # skips team/competition re-resolution.
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="api-football")

    first = catalog.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0, source_event_id="12345",
    )
    postponed_start = T0 + timedelta(days=3)

    second = catalog.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=postponed_start, source_event_id="12345",
    )

    assert second.event.id == first.event.id
    assert second.event.start_time == postponed_start

    row = connection.execute(
        "SELECT start_time FROM events WHERE id = ?", (first.event.id,)
    ).fetchone()
    assert datetime.fromisoformat(row["start_time"]) == postponed_start


def test_source_event_id_fast_path_does_not_rewrite_an_unchanged_start_time():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="api-football")

    catalog.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0, source_event_id="12345",
    )
    result = catalog.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0, source_event_id="12345",
    )

    assert result.event.start_time == T0


def test_slow_path_syncs_a_kickoff_shift_within_tolerance():
    # No source_event_id here -- the general team/competition/tolerance-
    # window matching path (_find_event) must sync start_time too, not
    # just the source_event_id fast path.
    connection = make_connection()
    catalog = FixtureCatalog(
        connection, provider_id="mozzart", start_time_tolerance=timedelta(minutes=30)
    )

    first = catalog.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0,
    )
    shifted_start = T0 + timedelta(minutes=15)

    second = catalog.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=shifted_start,
    )

    assert second.event.id == first.event.id
    assert second.event.start_time == shifted_start

    row = connection.execute(
        "SELECT start_time FROM events WHERE id = ?", (first.event.id,)
    ).fetchone()
    assert datetime.fromisoformat(row["start_time"]) == shifted_start


def test_different_providers_do_not_share_source_event_id_mappings():
    connection = make_connection()
    api_football = FixtureCatalog(connection, provider_id="api-football")
    the_odds_api = FixtureCatalog(connection, provider_id="the-odds-api")

    api_football.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0, source_event_id="12345",
    )

    # Same raw string "12345" from a different provider must not
    # accidentally hit api-football's cached mapping -- it's just an
    # unrelated string until this provider resolves it for itself.
    result = the_odds_api.match(
        sport="football", league="L", home_team_raw="Novi Tim",
        away_team_raw="Drugi Tim", start_time=T0, source_event_id="12345",
    )

    assert result.event.home_team.canonical_name == "Novi Tim"


def test_no_source_event_id_falls_back_to_normal_resolution():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="mozzart")

    result = catalog.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0, source_event_id=None,
    )

    assert result.event is not None
    mappings = connection.execute(
        "SELECT COUNT(*) AS n FROM source_event_mappings"
    ).fetchone()["n"]
    assert mappings == 0


def test_team_source_id_hit_skips_fuzzy_matching_entirely():
    # The point of the id fast path: a raw name that would never
    # fuzzy-match on its own (nothing here resembles "Partizan" at all)
    # still resolves correctly once its provider id was already mapped --
    # proof the fuzzy step is skipped outright, not just cached under the
    # old name.
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="api-football")

    first = catalog.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0,
        home_team_source_id="435", away_team_source_id="451",
    )

    second = catalog.match(
        sport="football", league="L", home_team_raw="Completely Different Spelling",
        away_team_raw="Also Totally Different", start_time=T0 + timedelta(days=1),
        home_team_source_id="435", away_team_source_id="451",
    )

    assert second.event.home_team.id == first.event.home_team.id
    assert second.event.away_team.id == first.event.away_team.id
    assert second.confidence == 100.0
    # No new team rows for the wildly different spellings -- the id hit
    # short-circuited before TeamNormalizer ever ran.
    teams = connection.execute("SELECT COUNT(*) AS n FROM teams").fetchone()["n"]
    assert teams == 2


def test_team_source_id_is_saved_after_a_name_based_resolution():
    # A source_team_id supplied alongside a name that resolves via the
    # normal (name-keyed) path must still get its id mapping written, so
    # the *next* sighting of that id can take the fast path even before
    # the raw name has ever drifted.
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="api-football")

    catalog.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0,
        home_team_source_id="435", away_team_source_id="451",
    )

    mappings = connection.execute(
        "SELECT source_team_id, last_seen_raw_name FROM source_team_id_mappings "
        "ORDER BY source_team_id"
    ).fetchall()
    assert {(row["source_team_id"], row["last_seen_raw_name"]) for row in mappings} == {
        ("435", "Partizan"),
        ("451", "Crvena Zvezda"),
    }


def test_team_source_id_hit_with_drifted_name_still_trusts_the_id(caplog):
    # A provider recycling a numeric id across a season boundary onto a
    # genuinely different name is still trusted -- see
    # fixture_catalog.team_id.name_drift -- but must be logged so a human
    # notices, not silently merged with no trace.
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="api-football")

    first = catalog.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0,
        home_team_source_id="435", away_team_source_id="451",
    )

    with caplog.at_level("WARNING"):
        second = catalog.match(
            sport="football", league="L", home_team_raw="A Totally Unrelated Name",
            away_team_raw="Crvena Zvezda", start_time=T0 + timedelta(days=200),
            home_team_source_id="435", away_team_source_id="451",
        )

    assert second.event.home_team.id == first.event.home_team.id
    assert any(
        record.message == "fixture_catalog.team_id.name_drift" for record in caplog.records
    )


def test_no_team_source_id_falls_back_to_normal_resolution():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="mozzart")

    result = catalog.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0,
    )

    assert result.event is not None
    mappings = connection.execute(
        "SELECT COUNT(*) AS n FROM source_team_id_mappings"
    ).fetchone()["n"]
    assert mappings == 0


def test_competition_source_id_hit_skips_fuzzy_matching_entirely():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="api-football")

    first = catalog.match(
        sport="football", league="Liga Profesional Argentina", home_team_raw="River Plate",
        away_team_raw="Boca Juniors", start_time=T0, competition_source_id="128",
    )

    second = catalog.match(
        sport="football", league="A Completely Different League Name",
        home_team_raw="Some Other Team", away_team_raw="Yet Another Team",
        start_time=T0 + timedelta(days=1), competition_source_id="128",
    )

    assert second.event.competition_id == first.event.competition_id
    competitions = connection.execute(
        "SELECT COUNT(*) AS n FROM competitions"
    ).fetchone()["n"]
    assert competitions == 1


def test_competition_source_id_is_saved_after_a_name_based_resolution():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="api-football")

    catalog.match(
        sport="football", league="Liga Profesional Argentina", home_team_raw="River Plate",
        away_team_raw="Boca Juniors", start_time=T0, competition_source_id="128",
    )

    row = connection.execute(
        "SELECT last_seen_raw_name FROM source_competition_id_mappings "
        "WHERE source_competition_id = ?",
        ("128",),
    ).fetchone()
    assert row["last_seen_raw_name"] == "Liga Profesional Argentina"


def test_competition_source_id_hit_with_drifted_name_still_trusts_the_id(caplog):
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="api-football")

    catalog.match(
        sport="football", league="Liga Profesional Argentina", home_team_raw="River Plate",
        away_team_raw="Boca Juniors", start_time=T0, competition_source_id="128",
    )

    with caplog.at_level("WARNING"):
        catalog.match(
            sport="football", league="A Totally Unrelated League Name",
            home_team_raw="River Plate", away_team_raw="Boca Juniors",
            start_time=T0 + timedelta(days=200), competition_source_id="128",
        )

    assert any(
        record.message == "fixture_catalog.competition_id.name_drift"
        for record in caplog.records
    )


def test_case_and_whitespace_variant_spelling_reuses_the_existing_team():
    # A different provider (or the same one, on a bad day) reporting the
    # same real team under different case/whitespace must not create a
    # second, duplicate canonical team.
    connection = make_connection()
    first_source = FixtureCatalog(connection, provider_id="mozzart")
    second_source = FixtureCatalog(connection, provider_id="the-odds-api")

    r1 = first_source.match(
        sport="football", league="L", home_team_raw="Real Madrid",
        away_team_raw="Barcelona", start_time=T0,
    )
    r2 = second_source.match(
        sport="football", league="L", home_team_raw="real   madrid",
        away_team_raw="barcelona", start_time=T0 + timedelta(minutes=5),
    )

    assert r1.event.home_team.id == r2.event.home_team.id
    assert r2.event.home_team.canonical_name == "Real Madrid"
    teams = connection.execute(
        "SELECT COUNT(*) AS n FROM teams WHERE sport = 'football'"
    ).fetchone()["n"]
    assert teams == 2  # Real Madrid + Barcelona, not duplicated


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


def test_alias_target_differing_only_by_case_reuses_the_existing_team_not_a_duplicate():
    # The alias's own target string ("MANCHESTER UNITED") is spelled
    # differently -- only by case -- from the real existing canonical
    # team ("Manchester United", created by an earlier sighting from a
    # different provider). Without resolving the alias target through
    # the same comparison key used everywhere else, this would create a
    # second, duplicate "MANCHESTER UNITED" team instead of reusing the
    # real one.
    connection = make_connection()
    first_source = FixtureCatalog(connection, provider_id="the-odds-api")
    first_source.match(
        sport="football", league="EPL", home_team_raw="Manchester United",
        away_team_raw="Everton", start_time=T0,
    )

    second_source = FixtureCatalog(
        connection,
        provider_id="json-demo",
        aliases={"Man Utd": "MANCHESTER UNITED"},
    )
    result = second_source.match(
        sport="football", league="EPL", home_team_raw="Man Utd",
        away_team_raw="Liverpool", start_time=T0 + timedelta(minutes=5),
    )

    assert result.event.home_team.canonical_name == "Manchester United"
    teams = connection.execute(
        "SELECT COUNT(*) AS n FROM teams WHERE canonical_name LIKE '%anchester%nited%'"
    ).fetchone()["n"]
    assert teams == 1


def test_token_alias_expands_a_brand_new_teams_canonical_name():
    # The expansion must apply even on a team's very first sighting (no
    # existing candidate to fuzzy-match against yet) -- otherwise a
    # *second* provider spelling the same acronym out in full ("United
    # Arab Emirates M23") would compare against this row's literal,
    # unexpanded name ("UAE M23") and fail to match it, just moving the
    # same gap to whichever provider is seen second instead of first.
    connection = make_connection()
    catalog = FixtureCatalog(
        connection, provider_id="src-a", token_aliases={"UAE": "United Arab Emirates"},
    )

    result = catalog.match(
        sport="football", league="L", home_team_raw="UAE M23",
        away_team_raw="Iran M23", start_time=T0,
    )

    assert result.event.home_team.canonical_name == "United Arab Emirates M23"


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


def mapping_row(connection, *, provider_id: str, sport: str, raw_name: str):
    return connection.execute(
        """
        SELECT resolution_method, confidence, created_at FROM source_team_mappings
        WHERE source = ? AND sport = ? AND source_team_name = ?
        """,
        (provider_id, sport, raw_name),
    ).fetchone()


def test_first_sighting_records_unknown_resolution_method_at_full_confidence():
    # A brand-new team has no existing candidate to match against at all
    # -- TeamNormalizer reports this as "unknown", but the *mapping*
    # confidence is still 100: raw_name definitionally maps to the team
    # just created for it, no merge/guess involved.
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="mozzart")

    catalog.match(
        sport="football", league="L", home_team_raw="Partizan",
        away_team_raw="Crvena Zvezda", start_time=T0,
    )

    row = mapping_row(connection, provider_id="mozzart", sport="football", raw_name="Partizan")
    assert row["resolution_method"] == "unknown"
    assert row["confidence"] == 100.0
    assert row["created_at"] is not None


def test_alias_match_records_alias_resolution_method():
    connection = make_connection()
    catalog = FixtureCatalog(
        connection, provider_id="json-demo", aliases={"Man Utd": "Manchester United"},
    )

    catalog.match(
        sport="football", league="EPL", home_team_raw="Man Utd",
        away_team_raw="Liverpool", start_time=T0,
    )

    row = mapping_row(connection, provider_id="json-demo", sport="football", raw_name="Man Utd")
    assert row["resolution_method"] == "alias"
    assert row["confidence"] == 100.0


def test_fuzzy_match_records_fuzzy_resolution_method_and_its_real_score():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="src-a", fuzzy_threshold=80.0)

    catalog.match(
        sport="football", league="L", home_team_raw="Manchester United",
        away_team_raw="Liverpool", start_time=T0,
    )
    catalog.match(
        sport="football", league="L", home_team_raw="Manchester Utd",
        away_team_raw="Liverpool", start_time=T0 + timedelta(minutes=10),
    )

    row = mapping_row(
        connection, provider_id="src-a", sport="football", raw_name="Manchester Utd"
    )
    assert row["resolution_method"] == "fuzzy"
    # Not asserted against a hardcoded score -- just that it's a real,
    # sub-100 confidence (a fuzzy match, not a perfect one), the exact
    # thing a reviewer would want visible without re-running the matcher.
    assert 80.0 <= row["confidence"] < 100.0


def test_exact_match_records_exact_resolution_method():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="src-a")

    catalog.match(
        sport="football", league="L", home_team_raw="Liverpool",
        away_team_raw="Everton", start_time=T0,
    )
    catalog.match(
        sport="football", league="L", home_team_raw="Liverpool",
        away_team_raw="Arsenal", start_time=T0 + timedelta(days=1),
        source_event_id=None,
    )
    # A different provider seeing the exact same already-canonical name
    # -- not the cached-mapping fast path (a fresh provider_id), so this
    # genuinely exercises TeamNormalizer's "exact" branch.
    other_catalog = FixtureCatalog(connection, provider_id="src-b")
    other_catalog.match(
        sport="football", league="L", home_team_raw="Liverpool",
        away_team_raw="Chelsea", start_time=T0 + timedelta(days=2),
    )

    row = mapping_row(connection, provider_id="src-b", sport="football", raw_name="Liverpool")
    assert row["resolution_method"] == "exact"
    assert row["confidence"] == 100.0


def test_ambiguous_match_records_ambiguous_resolution_method_and_its_real_score():
    connection = make_connection()
    # Two candidates scoring an exact tie (86.79 each, under
    # token_sort_ratio) against the raw name below -- genuinely
    # ambiguous, not just "both plausible".
    connection.execute(
        "INSERT INTO teams (id, canonical_name, sport) VALUES (?, ?, ?)",
        ("team-a", "Real City United Sporting Club", "football"),
    )
    connection.execute(
        "INSERT INTO teams (id, canonical_name, sport) VALUES (?, ?, ?)",
        ("team-b", "Real City Rovers Sporting Club", "football"),
    )
    connection.commit()
    catalog = FixtureCatalog(connection, provider_id="src-a", fuzzy_threshold=80.0)

    catalog.match(
        sport="football", league="L", home_team_raw="Real City Sporting Club",
        away_team_raw="Everton", start_time=T0,
    )

    row = mapping_row(
        connection, provider_id="src-a", sport="football", raw_name="Real City Sporting Club"
    )
    assert row["resolution_method"] == "ambiguous"
    assert row["confidence"] > 80.0


def test_pre_migration_12_mapping_rows_have_null_resolution_metadata():
    # A mapping written before this project tracked resolution_method/
    # confidence/created_at (see migration 12) has genuinely unknown
    # provenance -- NULL, not a fabricated backfilled value.
    connection = make_connection()
    connection.execute(
        "INSERT INTO teams (id, canonical_name, sport) VALUES ('team-old', 'Old Team', 'football')"
    )
    connection.execute(
        """
        INSERT INTO source_team_mappings (source, sport, source_team_name, team_id)
        VALUES ('old-source', 'football', 'Old Team', 'team-old')
        """
    )
    connection.commit()

    row = mapping_row(connection, provider_id="old-source", sport="football", raw_name="Old Team")
    assert row["resolution_method"] is None
    assert row["confidence"] is None
    assert row["created_at"] is None


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


def competition_mapping_row(connection, *, provider_id: str, sport: str, raw_league: str):
    return connection.execute(
        """
        SELECT resolution_method, confidence, created_at FROM source_competition_mappings
        WHERE source = ? AND sport = ? AND source_competition_name = ?
        """,
        (provider_id, sport, raw_league),
    ).fetchone()


def test_fuzzy_league_match_records_fuzzy_resolution_method_on_the_competition_mapping():
    connection = make_connection()
    catalog = FixtureCatalog(connection, provider_id="src-a", fuzzy_threshold=80.0)

    catalog.match(
        sport="football", league="Premier League", home_team_raw="A",
        away_team_raw="B", start_time=T0,
    )
    catalog.match(
        sport="football", league="Premier Leage", home_team_raw="C",
        away_team_raw="D", start_time=T0,
    )

    row = competition_mapping_row(
        connection, provider_id="src-a", sport="football", raw_league="Premier Leage"
    )
    assert row["resolution_method"] == "fuzzy"
    assert 80.0 <= row["confidence"] < 100.0
    assert row["created_at"] is not None


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
