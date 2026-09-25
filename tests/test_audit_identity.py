import sqlite3

from anomaly_detection_engine.storage.database import create_connection, initialize_database
from audit_identity import audit_identity, main


def _database(tmp_path):
    path = tmp_path / "audit.db"
    connection = create_connection(path)
    initialize_database(connection)
    return path, connection


def test_clean_database_has_no_findings(tmp_path):
    _, connection = _database(tmp_path)

    assert audit_identity(connection) == []


def test_audit_reports_orphans_duplicates_and_suspect_mappings(tmp_path):
    _, connection = _database(tmp_path)
    connection.executescript(
        """
        INSERT INTO teams (id, canonical_name, sport, identity_status)
        VALUES ('home', 'Home', 'football', 'VERIFIED'),
               ('away', 'Away', 'football', 'VERIFIED'),
               ('orphan', 'Unused B', 'football', 'PROVISIONAL');
        INSERT INTO competitions (id, canonical_name, sport, identity_status)
        VALUES ('league', 'League', 'football', 'VERIFIED'),
               ('unused-league', 'Unused League', 'football', 'PROVISIONAL');
        INSERT INTO events
            (id, sport, league, competition_id, home_team_id, away_team_id, start_time)
        VALUES ('e1', 'football', 'League', 'league', 'home', 'away', '2026-09-25T18:00:00+00:00'),
               ('e2', 'football', 'League', 'league', 'home', 'away', '2026-09-25T18:00:00+00:00');
        INSERT INTO source_team_mappings
            (source, sport, source_team_name, competition_id, team_id,
             resolution_method, confidence, created_at, trust_state, resolver_version)
        VALUES ('book', 'football', 'Unclear', '', 'home',
                'fuzzy', 87.0, '2026-09-25T18:00:00+00:00', 'SUSPECT', 1);
        """
    )
    connection.commit()

    codes = {finding.code for finding in audit_identity(connection)}

    assert "duplicate-canonical-events" in codes
    assert "orphan-provisional-teams" in codes
    assert "orphan-provisional-competitions" in codes
    assert "unresolved-source_team_mappings" in codes


def test_cli_opens_database_read_only(tmp_path, capsys):
    path, connection = _database(tmp_path)
    connection.close()

    main(["--db-path", str(path)])

    assert capsys.readouterr().out.strip() == "Identity audit: no findings."
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    check.close()
