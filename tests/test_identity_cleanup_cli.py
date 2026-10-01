import sqlite3

import pytest

from anomaly_detection_engine.storage.database import create_connection, initialize_database
from cleanup_identity import main


def make_db(tmp_path):
    path = tmp_path / "identity-gc-cli.db"
    connection = create_connection(path)
    initialize_database(connection)
    connection.execute(
        """
        INSERT INTO teams (id, canonical_name, sport, identity_status)
        VALUES ('orphan', 'Unused Team', 'football', 'PROVISIONAL')
        """
    )
    connection.execute(
        """
        INSERT INTO source_team_mappings (
            source, sport, source_team_name, competition_id, team_id,
            resolution_method, confidence, created_at, trust_state, resolver_version
        ) VALUES (
            'book', 'football', 'Unused Team', '', 'orphan',
            'fuzzy', 80.0, '2026-09-30T10:00:00+00:00', 'UNVERIFIED', 3
        )
        """
    )
    connection.commit()
    connection.close()
    return path


def test_cli_is_dry_run_by_default(tmp_path, capsys):
    path = make_db(tmp_path)

    main(["--db-path", str(path), "--entity", "team", "--id", "orphan"])

    output = capsys.readouterr().out
    assert "Identity cleanup: DRY RUN" in output
    assert "action:         GC_SAFE" in output

    check = sqlite3.connect(path)
    try:
        assert check.execute("SELECT COUNT(*) FROM teams WHERE id='orphan'").fetchone()[0] == 1
    finally:
        check.close()


def test_cli_apply_deletes_gc_safe_identity(tmp_path, capsys):
    path = make_db(tmp_path)

    main(
        [
            "--db-path",
            str(path),
            "--entity",
            "team",
            "--id",
            "orphan",
            "--apply",
        ]
    )

    output = capsys.readouterr().out
    assert "Identity cleanup: APPLY" in output
    assert "Deleted: teams=1" in output

    check = sqlite3.connect(path)
    try:
        assert check.execute("SELECT COUNT(*) FROM teams WHERE id='orphan'").fetchone()[0] == 0
        assert check.execute(
            "SELECT COUNT(*) FROM source_team_mappings WHERE team_id='orphan'"
        ).fetchone()[0] == 0
    finally:
        check.close()


def test_cli_id_requires_entity(tmp_path):
    path = make_db(tmp_path)

    with pytest.raises(SystemExit):
        main(["--db-path", str(path), "--id", "orphan"])
