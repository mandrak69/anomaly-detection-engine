import sqlite3

import pytest

from anomaly_detection_engine.storage.database import configure_connection, initialize_database


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
