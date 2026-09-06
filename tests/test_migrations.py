import sqlite3

from anomaly_detection_engine.storage.database import configure_connection, initialize_database
from anomaly_detection_engine.storage.migrations import MIGRATIONS, _migration_1_initial_schema


def make_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    return connection


def test_fresh_database_ends_up_at_latest_version():
    connection = make_connection()

    initialize_database(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_initialize_database_is_idempotent():
    connection = make_connection()

    initialize_database(connection)
    initialize_database(connection)  # must not raise or re-apply anything

    assert connection.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_migrating_a_pre_existing_old_schema_database_preserves_data():
    # Simulates a real persistent data/anomaly_detection.db created before
    # migrations existed: only the original schema (no market_rules/
    # market_specifier on signals/movements, narrower indexes), and no
    # user_version bookkeeping (starts at 0). initialize_database() must
    # bring it up to date in place without losing what's already there --
    # "just delete the file" is not an acceptable answer for a real
    # deployment's data.
    connection = make_connection()
    _migration_1_initial_schema(connection)
    connection.execute(
        """
        INSERT INTO signals (
            id, signal_type, event_id, market_type, market_period,
            status, edge_percent, details, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "sig-1", "SUREBET", "e1", "three_way", "full_time",
            "ACTIVE", "5.0", "{}",
            "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
        ),
    )
    connection.commit()
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0

    initialize_database(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)

    columns = {row[1] for row in connection.execute("PRAGMA table_info(signals)")}
    assert "market_rules" in columns
    assert "market_specifier" in columns

    row = connection.execute("SELECT * FROM signals WHERE id = ?", ("sig-1",)).fetchone()
    assert row is not None
    assert row["status"] == "ACTIVE"
    assert row["market_rules"] is None

    index_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'uq_signals_identity'"
    ).fetchone()[0]
    assert "market_rules" in index_sql
    assert "market_specifier" in index_sql


def test_migrate_does_not_reapply_already_applied_migrations():
    # A connection already at version 1 (migration 1 applied, e.g. by a
    # previous initialize_database() call before migration 2 shipped)
    # must only run migration 2 going forward, not re-run migration 1.
    connection = make_connection()
    _migration_1_initial_schema(connection)
    connection.execute("PRAGMA user_version = 1")
    connection.commit()

    initialize_database(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(movements)")}
    assert "market_rules" in columns
