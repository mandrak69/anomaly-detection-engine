import json
import sys

import pytest

from anomaly_detection_engine.storage.database import create_connection, initialize_database
from replay_against_copy import copy_database, database_snapshot, run_replay


def _database(path):
    connection = create_connection(path)
    initialize_database(connection)
    connection.execute(
        "INSERT INTO teams (id, canonical_name, sport) VALUES ('t1', 'Alpha', 'football')"
    )
    connection.commit()
    connection.close()


def test_copy_database_does_not_modify_the_source(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "copy.db"
    _database(source)

    copy_database(source, target)
    copied = create_connection(target)
    copied.execute(
        "INSERT INTO teams (id, canonical_name, sport) VALUES ('t2', 'Beta', 'football')"
    )
    copied.commit()
    copied.close()

    original = create_connection(source)
    assert database_snapshot(original)["table_counts"]["teams"] == 1
    original.close()


def test_replay_reports_before_after_diff_and_uses_db_path(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "copy.db"
    report_path = tmp_path / "report.json"
    _database(source)

    code = (
        "import os, sqlite3; "
        "c=sqlite3.connect(os.environ['DB_PATH']); "
        "c.execute(\"INSERT INTO teams (id, canonical_name, sport) "
        "VALUES ('t2', 'Beta', 'football')\"); c.commit()"
    )
    report, exit_code = run_replay(
        source, target, [sys.executable, "-c", code], report_path=report_path
    )

    assert exit_code == 0
    assert report["before"]["table_counts"]["teams"] == 1
    assert report["after"]["table_counts"]["teams"] == 2
    assert report["diff"]["table_counts"]["teams"] == 1
    assert json.loads(report_path.read_text(encoding="utf-8")) == report


def test_copy_database_refuses_to_overwrite_an_existing_target(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "copy.db"
    _database(source)
    target.touch()

    with pytest.raises(ValueError, match="already exists"):
        copy_database(source, target)
