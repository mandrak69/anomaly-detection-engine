import sqlite3

import pytest

from anomaly_detection_engine.storage.bookmaker_catalog import (
    BookmakerCatalog,
    normalize_bookmaker_name,
)
from anomaly_detection_engine.storage.database import configure_connection, initialize_database


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    return connection


def test_normalize_bookmaker_name_collapses_case_whitespace_and_punctuation():
    assert normalize_bookmaker_name("Bet365") == "bet365"
    assert normalize_bookmaker_name("BET365") == "bet365"
    assert normalize_bookmaker_name("Bet 365") == "bet365"
    assert normalize_bookmaker_name("  Bet365  ") == "bet365"


def test_two_providers_reporting_the_same_bookmaker_resolve_to_one_canonical_id():
    connection = _connection()
    the_odds_api = BookmakerCatalog(connection, provider_id="the-odds-api")
    api_football = BookmakerCatalog(connection, provider_id="api-football")

    a = the_odds_api.resolve(source_bookmaker_id="bet365", source_name="Bet365")
    b = api_football.resolve(source_bookmaker_id="8", source_name="Bet365")

    assert a.id == b.id


def test_same_provider_source_id_is_stable_across_repeated_resolutions():
    connection = _connection()
    catalog = BookmakerCatalog(connection, provider_id="the-odds-api")

    first = catalog.resolve(source_bookmaker_id="bet365", source_name="Bet365")
    second = catalog.resolve(source_bookmaker_id="bet365", source_name="Bet365")

    assert first.id == second.id


def test_unknown_bookmaker_gets_a_new_canonical_identity():
    connection = _connection()
    catalog = BookmakerCatalog(connection, provider_id="the-odds-api")

    bookmaker = catalog.resolve(source_bookmaker_id="unibet", source_name="Unibet")

    assert bookmaker.id.startswith("bookmaker-")
    assert bookmaker.name == "Unibet"


def test_normalized_name_matching_an_existing_canonical_bookmaker_links_to_it():
    # No (provider_id, source_bookmaker_id) mapping on file yet for this
    # provider, but a canonical bookmaker with the same normalized name
    # already exists (created via a different source_bookmaker_id, e.g.
    # a provider that changed its own internal ids) -- must link to the
    # existing canonical bookmaker rather than creating a duplicate.
    connection = _connection()
    catalog = BookmakerCatalog(connection, provider_id="the-odds-api")

    original = catalog.resolve(source_bookmaker_id="bet365", source_name="Bet365")
    relinked = catalog.resolve(source_bookmaker_id="bet365-new-id", source_name="Bet365")

    assert relinked.id == original.id


def test_display_name_change_does_not_create_a_new_canonical_bookmaker():
    connection = _connection()
    catalog = BookmakerCatalog(connection, provider_id="the-odds-api")

    original = catalog.resolve(source_bookmaker_id="bet365", source_name="Bet365")
    renamed = catalog.resolve(source_bookmaker_id="bet365", source_name="Bet365 UK")

    assert renamed.id == original.id


def test_similarly_named_bookmakers_are_never_collapsed_by_similarity_alone():
    # "Pinnacle" and "Pinnacle Sports" might be genuinely different real
    # bookmakers/feeds -- normalize_bookmaker_name only strips
    # case/whitespace/punctuation, it never fuzzy-matches, so these must
    # resolve to two distinct canonical bookmakers.
    connection = _connection()
    catalog = BookmakerCatalog(connection, provider_id="the-odds-api")

    pinnacle = catalog.resolve(source_bookmaker_id="pinnacle", source_name="Pinnacle")
    pinnacle_sports = catalog.resolve(
        source_bookmaker_id="pinnacle_sports", source_name="Pinnacle Sports"
    )

    assert pinnacle.id != pinnacle_sports.id


def test_no_stable_source_id_falls_back_to_normalized_name_as_the_mapping_key():
    # Sources with no stable per-bookmaker id (JSON demo, Mozzart) still
    # get "same name, same provider -> same canonical bookmaker"
    # stability across repeated resolutions -- the same behavior the old
    # raw.source.lower() scheme gave, now centralized here.
    connection = _connection()
    catalog = BookmakerCatalog(connection, provider_id="mozzart")

    first = catalog.resolve(source_bookmaker_id=None, source_name="Mozzart")
    second = catalog.resolve(source_bookmaker_id=None, source_name="Mozzart")

    assert first.id == second.id


def test_bookmakers_normalized_name_is_unique_at_the_schema_level():
    # Structural guarantee behind "0 or 1 matches, never ambiguous":
    # nothing (not even a bug elsewhere) can persist two canonical
    # bookmaker rows with the same normalized_name.
    connection = _connection()
    connection.execute(
        "INSERT INTO bookmakers (id, canonical_name, normalized_name) VALUES (?, ?, ?)",
        ("bookmaker-aaaaaaaaaa", "Bet365", "bet365"),
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO bookmakers (id, canonical_name, normalized_name) VALUES (?, ?, ?)",
            ("bookmaker-bbbbbbbbbb", "BET365", "bet365"),
        )
