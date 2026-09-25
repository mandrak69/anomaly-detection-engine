import sqlite3
from datetime import UTC, datetime

import pytest

from anomaly_detection_engine.storage.database import create_connection, initialize_database
from identity_mapping import main


def make_db(tmp_path):
    db_path = tmp_path / "test.db"
    connection = create_connection(str(db_path))
    initialize_database(connection)
    connection.execute(
        "INSERT INTO teams (id, canonical_name, sport) VALUES ('team-1', 'Arsenal', 'football')"
    )
    connection.execute(
        """
        INSERT INTO competitions (id, canonical_name, sport)
        VALUES ('competition-1', 'EPL', 'football')
        """
    )
    connection.commit()
    connection.close()
    return db_path


def add_event(db_path):
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO teams (id, canonical_name, sport) VALUES ('team-2', 'Chelsea', 'football')"
    )
    connection.execute(
        """
        INSERT INTO events (
            id, sport, league, competition_id, home_team_id, away_team_id, start_time
        ) VALUES (?, 'football', 'EPL', 'competition-1', 'team-1', 'team-2', ?)
        """,
        ("event-1", datetime(2026, 9, 25, 18, tzinfo=UTC).isoformat()),
    )
    connection.commit()
    connection.close()


def test_team_command_writes_a_verified_mapping(tmp_path, capsys):
    db_path = make_db(tmp_path)

    main(
        [
            "--db-path", str(db_path), "team",
            "--provider", "mozzart", "--sport", "football",
            "--source-name", "Westham Untd", "--team-id", "team-1",
            "--source-team-id", "99999",
        ]
    )

    assert "Verified" in capsys.readouterr().out

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    mapping = connection.execute(
        "SELECT trust_state, resolution_method, team_id FROM source_team_mappings "
        "WHERE source='mozzart' AND source_team_name='Westham Untd'"
    ).fetchone()
    assert mapping["trust_state"] == "VERIFIED"
    assert mapping["resolution_method"] == "manual"
    assert mapping["team_id"] == "team-1"

    id_mapping = connection.execute(
        "SELECT trust_state, team_id FROM source_team_id_mappings "
        "WHERE source='mozzart' AND source_team_id='99999'"
    ).fetchone()
    assert id_mapping["trust_state"] == "VERIFIED"
    assert id_mapping["team_id"] == "team-1"


def test_team_command_clears_an_existing_suspect_id_mapping(tmp_path):
    db_path = make_db(tmp_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        INSERT INTO source_team_id_mappings
            (source, sport, source_team_id, team_id, trust_state, resolver_version)
        VALUES ('mozzart', 'football', '99999', 'team-1', 'SUSPECT', 1)
        """
    )
    connection.commit()
    connection.close()

    main(
        [
            "--db-path", str(db_path), "team",
            "--provider", "mozzart", "--sport", "football",
            "--source-name", "Westham Untd", "--team-id", "team-1",
            "--source-team-id", "99999",
        ]
    )

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT trust_state FROM source_team_id_mappings "
        "WHERE source='mozzart' AND source_team_id='99999'"
    ).fetchone()
    assert row["trust_state"] == "VERIFIED"


def test_team_command_rejects_an_unknown_team_id(tmp_path):
    db_path = make_db(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--db-path", str(db_path), "team",
                "--provider", "mozzart", "--sport", "football",
                "--source-name", "Ghost FC", "--team-id", "team-does-not-exist",
            ]
        )

    assert "error:" in str(exc_info.value)


def test_competition_command_writes_a_verified_mapping(tmp_path):
    db_path = make_db(tmp_path)

    main(
        [
            "--db-path", str(db_path), "competition",
            "--provider", "mozzart", "--sport", "football",
            "--source-name", "Engleska Premier Liga", "--competition-id", "competition-1",
            "--country", "England", "--source-competition-id", "67890",
        ]
    )

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    mapping = connection.execute(
        "SELECT trust_state, competition_id FROM source_competition_mappings "
        "WHERE source='mozzart' AND source_competition_name='Engleska Premier Liga'"
    ).fetchone()
    assert mapping["trust_state"] == "VERIFIED"
    assert mapping["competition_id"] == "competition-1"


def test_suspects_command_lists_a_suspect_team_id_mapping(tmp_path, capsys):
    db_path = make_db(tmp_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        INSERT INTO source_team_id_mappings
            (source, sport, source_team_id, team_id, trust_state, resolver_version,
             last_seen_raw_name)
        VALUES ('mozzart', 'football', '99999', 'team-1', 'SUSPECT', 1, 'Drifted Name FC')
        """
    )
    connection.commit()
    connection.close()

    main(["--db-path", str(db_path), "suspects"])

    output = capsys.readouterr().out
    assert "SUSPECT source_team_id_mappings" in output
    assert "Drifted Name FC" in output


def test_suspects_command_reports_nothing_when_the_database_is_clean(tmp_path, capsys):
    db_path = make_db(tmp_path)

    main(["--db-path", str(db_path), "suspects"])

    assert capsys.readouterr().out == ""


def test_event_command_replaces_suspect_mapping_with_verified_snapshot(tmp_path, capsys):
    db_path = make_db(tmp_path)
    add_event(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        INSERT INTO source_event_mappings (
            source, source_event_id, event_id, trust_state,
            resolution_method, resolver_version, sport
        ) VALUES ('api-football', 'fixture-10', 'event-1', 'SUSPECT',
                  'name_drift', 1, 'football')
        """
    )
    connection.commit()
    connection.close()

    main(
        [
            "--db-path", str(db_path), "event",
            "--provider", "api-football", "--sport", "football",
            "--source-event-id", "fixture-10", "--event-id", "event-1",
            "--home-name", "Arsenal FC", "--away-name", "Chelsea FC",
            "--competition-name", "England - Premier League", "--country", "England",
            "--home-source-team-id", "42", "--away-source-team-id", "49",
            "--source-competition-id", "39",
        ]
    )

    assert "Verified event" in capsys.readouterr().out
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    mapping = connection.execute(
        "SELECT * FROM source_event_mappings "
        "WHERE source='api-football' AND source_event_id='fixture-10'"
    ).fetchone()
    event = connection.execute("SELECT * FROM events WHERE id='event-1'").fetchone()
    assert mapping["trust_state"] == "VERIFIED"
    assert mapping["resolution_method"] == "manual"
    assert mapping["last_verified_home_name"] == "Arsenal FC"
    assert mapping["last_verified_away_name"] == "Chelsea FC"
    assert mapping["last_verified_competition_name"] == "England - Premier League"
    assert mapping["home_source_team_id"] == "42"
    assert event["reference_provider"] == "api-football"
    assert event["reference_provider_id"] == "fixture-10"


def test_event_command_rejects_unknown_canonical_event(tmp_path):
    db_path = make_db(tmp_path)

    with pytest.raises(SystemExit, match="Unknown canonical event"):
        main(
            [
                "--db-path", str(db_path), "event",
                "--provider", "mozzart", "--sport", "football",
                "--source-event-id", "10", "--event-id", "missing",
                "--home-name", "Alpha", "--away-name", "Beta",
                "--competition-name", "League",
            ]
        )
