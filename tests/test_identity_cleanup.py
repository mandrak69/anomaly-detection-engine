import sqlite3

import pytest

from anomaly_detection_engine.identity_cleanup import (
    apply_identity_cleanup,
    find_identity_cleanup_candidates,
)
from anomaly_detection_engine.storage.database import create_connection, initialize_database

NOW = "2026-09-30T10:00:00+00:00"


def make_connection(tmp_path) -> sqlite3.Connection:
    connection = create_connection(tmp_path / "identity-gc.db")
    initialize_database(connection)
    return connection


def add_team(
    connection: sqlite3.Connection,
    team_id: str,
    *,
    name: str | None = None,
    identity_status: str = "PROVISIONAL",
    reference_provider: str | None = None,
    reference_provider_id: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO teams (
            id, canonical_name, sport, identity_status,
            reference_provider, reference_provider_id
        ) VALUES (?, ?, 'football', ?, ?, ?)
        """,
        (team_id, name or team_id, identity_status, reference_provider, reference_provider_id),
    )


def add_competition(
    connection: sqlite3.Connection,
    competition_id: str,
    *,
    name: str | None = None,
    identity_status: str = "PROVISIONAL",
) -> None:
    connection.execute(
        """
        INSERT INTO competitions (id, canonical_name, sport, identity_status)
        VALUES (?, ?, 'football', ?)
        """,
        (competition_id, name or competition_id, identity_status),
    )


def add_team_name_mapping(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    trust_state: str,
    competition_id: str = "",
    source_name: str = "Raw Team",
) -> None:
    connection.execute(
        """
        INSERT INTO source_team_mappings (
            source, sport, source_team_name, competition_id, team_id,
            resolution_method, confidence, created_at, trust_state, resolver_version
        ) VALUES ('book', 'football', ?, ?, ?, 'fuzzy', 80.0, ?, ?, 3)
        """,
        (source_name, competition_id, team_id, NOW, trust_state),
    )


def add_team_id_mapping(
    connection: sqlite3.Connection, *, team_id: str, trust_state: str
) -> None:
    connection.execute(
        """
        INSERT INTO source_team_id_mappings (
            source, sport, source_team_id, team_id,
            trust_state, resolver_version, created_at
        ) VALUES ('book', 'football', ?, ?, ?, 3, ?)
        """,
        (f"source-{team_id}", team_id, trust_state, NOW),
    )


def add_competition_name_mapping(
    connection: sqlite3.Connection,
    *,
    competition_id: str,
    trust_state: str,
) -> None:
    connection.execute(
        """
        INSERT INTO source_competition_mappings (
            source, sport, source_competition_name, country_key, competition_id,
            resolution_method, confidence, created_at, trust_state, resolver_version
        ) VALUES ('book', 'football', ?, '', ?, 'fuzzy', 80.0, ?, ?, 3)
        """,
        (f"Raw {competition_id}", competition_id, NOW, trust_state),
    )


def add_team_competition(
    connection: sqlite3.Connection, *, team_id: str, competition_id: str
) -> None:
    connection.execute(
        """
        INSERT INTO team_competitions (team_id, competition_id, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?)
        """,
        (team_id, competition_id, NOW, NOW),
    )


def test_orphan_with_only_unverified_identity_is_gc_safe(tmp_path):
    connection = make_connection(tmp_path)
    add_team(connection, "orphan", name="Unused B")
    add_team_name_mapping(connection, team_id="orphan", trust_state="UNVERIFIED")
    add_team_id_mapping(connection, team_id="orphan", trust_state="UNVERIFIED")
    connection.commit()

    [candidate] = find_identity_cleanup_candidates(
        connection, entity_type="team", entity_id="orphan"
    )

    assert candidate.safe_to_delete is True
    assert candidate.unverified_mappings == 2
    assert candidate.protection_reasons == ()


@pytest.mark.parametrize("trust_state", ["VERIFIED", "SUSPECT", "REJECTED"])
def test_durable_mapping_protects_an_orphan_team(tmp_path, trust_state):
    connection = make_connection(tmp_path)
    add_team(connection, "orphan")
    add_team_name_mapping(connection, team_id="orphan", trust_state=trust_state)
    connection.commit()

    [candidate] = find_identity_cleanup_candidates(
        connection, entity_type="team", entity_id="orphan"
    )

    assert candidate.safe_to_delete is False
    assert candidate.protection_reasons == (f"{trust_state.lower()}-mapping",)


def test_unknown_future_trust_state_fails_closed(tmp_path):
    connection = make_connection(tmp_path)
    add_team(connection, "orphan")
    add_team_name_mapping(connection, team_id="orphan", trust_state="RETIRED")
    connection.commit()

    [candidate] = find_identity_cleanup_candidates(
        connection, entity_type="team", entity_id="orphan"
    )

    assert candidate.safe_to_delete is False
    assert candidate.other_mappings == 1
    assert candidate.protection_reasons == ("unknown-trust-state",)


def test_reference_identity_protects_even_an_inconsistent_provisional_row(tmp_path):
    connection = make_connection(tmp_path)
    add_team(
        connection,
        "reference-like",
        reference_provider="api-football",
        reference_provider_id="42",
    )
    connection.commit()

    [candidate] = find_identity_cleanup_candidates(
        connection, entity_type="team", entity_id="reference-like"
    )

    assert candidate.safe_to_delete is False
    assert candidate.protection_reasons == ("reference-identity",)


def test_team_referenced_by_an_event_is_not_an_orphan_candidate(tmp_path):
    connection = make_connection(tmp_path)
    add_team(connection, "home")
    add_team(connection, "away")
    add_competition(connection, "league")
    connection.execute(
        """
        INSERT INTO events (
            id, sport, league, competition_id, home_team_id, away_team_id, start_time
        ) VALUES ('event-1', 'football', 'League', 'league', 'home', 'away', ?)
        """,
        (NOW,),
    )
    connection.commit()

    assert find_identity_cleanup_candidates(
        connection, entity_type="team", entity_id="home"
    ) == []


def test_apply_deletes_safe_team_with_unverified_mappings_and_relations(tmp_path):
    connection = make_connection(tmp_path)
    add_team(connection, "orphan")
    add_team(connection, "member")
    add_competition(connection, "league")
    add_team_name_mapping(
        connection,
        team_id="orphan",
        trust_state="UNVERIFIED",
        competition_id="league",
    )
    add_team_id_mapping(connection, team_id="orphan", trust_state="UNVERIFIED")
    add_team_competition(connection, team_id="orphan", competition_id="league")
    connection.commit()

    summary = apply_identity_cleanup(
        connection, entity_type="team", entity_id="orphan"
    )

    assert summary.teams_deleted == 1
    assert summary.mappings_deleted == 2
    assert summary.relations_deleted == 1
    assert connection.execute("SELECT 1 FROM teams WHERE id='orphan'").fetchone() is None
    assert connection.execute(
        "SELECT COUNT(*) FROM source_team_mappings WHERE team_id='orphan'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM source_team_id_mappings WHERE team_id='orphan'"
    ).fetchone()[0] == 0


def test_rejected_mapping_and_its_team_survive_cleanup(tmp_path):
    connection = make_connection(tmp_path)
    add_team(connection, "protected")
    add_team_name_mapping(connection, team_id="protected", trust_state="REJECTED")
    connection.execute(
        """
        UPDATE source_team_mappings
        SET rejection_reason='wrong club', rejected_at=?
        WHERE team_id='protected'
        """,
        (NOW,),
    )
    connection.commit()

    summary = apply_identity_cleanup(
        connection, entity_type="team", entity_id="protected"
    )

    assert summary.total_rows_deleted == 0
    assert connection.execute("SELECT 1 FROM teams WHERE id='protected'").fetchone()
    mapping = connection.execute(
        "SELECT trust_state, rejection_reason FROM source_team_mappings "
        "WHERE team_id='protected'"
    ).fetchone()
    assert mapping["trust_state"] == "REJECTED"
    assert mapping["rejection_reason"] == "wrong club"


def test_competition_cleanup_includes_contextual_team_mappings(tmp_path):
    connection = make_connection(tmp_path)
    add_team(connection, "team")
    add_competition(connection, "unused-league")
    add_competition_name_mapping(
        connection, competition_id="unused-league", trust_state="UNVERIFIED"
    )
    add_team_name_mapping(
        connection,
        team_id="team",
        trust_state="UNVERIFIED",
        competition_id="unused-league",
        source_name="Context Team",
    )
    add_team_competition(connection, team_id="team", competition_id="unused-league")
    connection.commit()

    [candidate] = find_identity_cleanup_candidates(
        connection, entity_type="competition", entity_id="unused-league"
    )
    assert candidate.safe_to_delete is True
    assert candidate.unverified_mappings == 2

    summary = apply_identity_cleanup(
        connection, entity_type="competition", entity_id="unused-league"
    )

    assert summary.competitions_deleted == 1
    assert summary.mappings_deleted == 2
    assert summary.relations_deleted == 1
    assert connection.execute(
        "SELECT 1 FROM competitions WHERE id='unused-league'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT COUNT(*) FROM source_team_mappings WHERE competition_id='unused-league'"
    ).fetchone()[0] == 0


def test_verified_contextual_team_mapping_protects_competition(tmp_path):
    connection = make_connection(tmp_path)
    add_team(connection, "team")
    add_competition(connection, "unused-league")
    add_team_name_mapping(
        connection,
        team_id="team",
        trust_state="VERIFIED",
        competition_id="unused-league",
        source_name="Trusted Context Team",
    )
    connection.commit()

    [candidate] = find_identity_cleanup_candidates(
        connection, entity_type="competition", entity_id="unused-league"
    )

    assert candidate.safe_to_delete is False
    assert candidate.verified_mappings == 1
    assert candidate.protection_reasons == ("verified-mapping",)


def test_cleanup_rolls_back_child_deletes_if_parent_delete_fails(tmp_path):
    connection = make_connection(tmp_path)
    add_team(connection, "orphan")
    add_team_name_mapping(connection, team_id="orphan", trust_state="UNVERIFIED")
    connection.commit()
    connection.execute(
        """
        CREATE TRIGGER fail_orphan_delete
        BEFORE DELETE ON teams
        WHEN OLD.id = 'orphan'
        BEGIN
            SELECT RAISE(ABORT, 'simulated delete failure');
        END;
        """
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="simulated delete failure"):
        apply_identity_cleanup(connection, entity_type="team", entity_id="orphan")

    assert connection.execute("SELECT 1 FROM teams WHERE id='orphan'").fetchone()
    assert connection.execute(
        "SELECT COUNT(*) FROM source_team_mappings WHERE team_id='orphan'"
    ).fetchone()[0] == 1
