import json
import sqlite3

import pytest

from anomaly_detection_engine.storage.database import create_connection, initialize_database
from database_backup import create_backup, manifest_path, verify_database


def _source_database(tmp_path):
    path = tmp_path / "source.db"
    connection = create_connection(path)
    initialize_database(connection)
    connection.execute(
        "INSERT INTO teams (id, canonical_name, sport) VALUES ('team-1', 'Arsenal', 'football')"
    )
    connection.commit()
    connection.close()
    return path


def test_backup_is_consistent_and_has_a_checksum_manifest(tmp_path):
    source = _source_database(tmp_path)
    backup = tmp_path / "backup.db"

    result = create_backup(source, backup)

    assert backup.is_file()
    assert result["backup"]["ok"] is True
    assert result["backup"]["sha256"] == verify_database(backup)["sha256"]
    stored_manifest = json.loads(manifest_path(backup).read_text(encoding="utf-8"))
    assert stored_manifest == result
    connection = sqlite3.connect(backup)
    assert connection.execute("SELECT canonical_name FROM teams").fetchone()[0] == "Arsenal"
    connection.close()


def test_verify_detects_checksum_mismatch(tmp_path):
    source = _source_database(tmp_path)

    result = verify_database(source, expected_sha256="not-the-real-checksum")

    assert result["ok"] is False
    assert result["checksum_matches"] is False


def test_backup_refuses_to_overwrite_existing_target(tmp_path):
    source = _source_database(tmp_path)
    backup = tmp_path / "backup.db"
    backup.touch()

    with pytest.raises(ValueError, match="already exists"):
        create_backup(source, backup)
