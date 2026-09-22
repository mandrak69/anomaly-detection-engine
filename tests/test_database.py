import sqlite3

import pytest

from anomaly_detection_engine.storage.database import (
    configure_connection,
    create_connection,
    initialize_database,
)


def test_configure_connection_enables_row_factory():
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)

    assert connection.row_factory is sqlite3.Row


def test_configure_connection_enables_foreign_key_enforcement():
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)

    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_foreign_key_violation_is_rejected():
    # events.home_team_id/away_team_id REFERENCES teams(id) -- without
    # PRAGMA foreign_keys = ON (SQLite does not enable this by default on
    # a new connection, even though the schema declares it), this insert
    # would silently succeed and leave an orphan row.
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO events (id, sport, league, home_team_id, away_team_id, start_time)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "event-1", "football", "L",
                "no-such-team-1", "no-such-team-2", "2026-01-01T00:00:00+00:00",
            ),
        )


def test_foreign_key_violation_is_rejected_without_foreign_keys_pragma():
    # Sanity check for the test above: proves the violation is only
    # caught because configure_connection turns PRAGMA foreign_keys on --
    # without it, SQLite happily inserts the orphan row.
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)

    connection.execute(
        """
        INSERT INTO events (id, sport, league, home_team_id, away_team_id, start_time)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "event-1", "football", "L",
            "no-such-team-1", "no-such-team-2", "2026-01-01T00:00:00+00:00",
        ),
    )
    connection.commit()

    row = connection.execute("SELECT * FROM events WHERE id = ?", ("event-1",)).fetchone()
    assert row is not None  # orphan row was allowed through


def test_configure_connection_enables_wal_journal_mode(tmp_path):
    # journal_mode is a database-*file* setting, not a per-connection
    # one -- meaningless on :memory: (SQLite reports "memory" there
    # regardless, confirmed separately), so this needs a real file.
    db_path = str(tmp_path / "test.db")
    connection = sqlite3.connect(db_path)
    configure_connection(connection)

    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_wal_mode_persists_in_the_file_for_a_later_connection(tmp_path):
    db_path = str(tmp_path / "test.db")
    first = sqlite3.connect(db_path)
    configure_connection(first)
    first.close()

    # A fresh connection that never calls configure_connection itself
    # still sees WAL -- it's recorded in the database file, not
    # something each connection has to separately turn on.
    second = sqlite3.connect(db_path)
    assert second.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_reader_is_not_blocked_by_an_open_writer_transaction_under_wal(tmp_path):
    # The concurrency guarantee this project actually needs: this
    # project explicitly supports multiple processes sharing one
    # database file (watch_capture.py-spawned collectors, poller.py
    # alongside inspect_data.py/run_retention_cleanup.py run by hand),
    # and FixtureCatalog.match()/BookmakerCatalog.resolve() hold a
    # BEGIN IMMEDIATE write transaction open for their whole
    # resolve-or-create cycle. Under the default rollback-journal mode,
    # a writer's lock can escalate to exclusive once it flushes pages to
    # disk, blocking every reader until it finishes; WAL's entire point
    # is that a reader is never blocked by a writer (or vice versa) --
    # only writer-vs-writer contention remains, which BEGIN IMMEDIATE
    # already serializes explicitly.
    db_path = str(tmp_path / "test.db")
    writer = create_connection(db_path)
    initialize_database(writer)

    reader = create_connection(db_path)

    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO teams (id, canonical_name, sport) VALUES (?, ?, ?)",
        ("team-1", "A Team", "football"),
    )
    try:
        # Must not raise "database is locked" (sqlite3.OperationalError)
        # despite the writer's transaction still being open/uncommitted.
        count = reader.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
        assert count == 0  # the writer's insert isn't committed yet either
    finally:
        writer.rollback()
