import sqlite3
from pathlib import Path

from anomaly_detection_engine.storage.migrations import migrate


def configure_connection(connection: sqlite3.Connection) -> None:
    """Applies every per-connection setting a connection to this
    database needs, regardless of how it was opened -- row access by
    column name, foreign-key enforcement, and WAL journal mode. SQLite
    does not enable FK enforcement by default on a new connection (it is
    off unless a connection explicitly turns it on, even though the
    schema itself declares REFERENCES), so every connection this
    project opens must call this, not just the ones that happen to go
    through create_connection() below.

    journal_mode is a *database-file* setting, not a per-connection one
    -- WAL, once set by any connection, persists in the file itself and
    every later connection already sees it without needing to set it
    again. Set here anyway, unconditionally, for the same reason
    foreign_keys is: this function is the one place that guarantees a
    connection this project opens is configured correctly regardless of
    which process created the file or in what order connections open,
    and re-asserting an already-set journal_mode is a cheap no-op
    (PRAGMA journal_mode=WAL just confirms and returns 'wal').

    Why WAL: this project explicitly supports multiple concurrent
    connections against one database file (watch_capture.py-spawned
    processes, poller.py alongside inspect_data.py/
    run_retention_cleanup.py run by hand) -- the default rollback-
    journal mode lets a writer's lock escalate to exclusive once it
    actually flushes pages to disk, blocking every reader until that
    writer finishes; WAL's whole purpose is that readers are never
    blocked by a writer (and vice versa), only writer-vs-writer
    contention remains, which this project's own BEGIN IMMEDIATE
    pattern (FixtureCatalog.match(), BookmakerCatalog.resolve()) already
    serializes explicitly. Verified directly: a reader connection reads
    successfully in ~5ms while a separate connection holds an open,
    uncommitted BEGIN IMMEDIATE write transaction under WAL.
    """
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")


def create_connection(db_path: str | Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=timeout)
    configure_connection(connection)
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    migrate(connection)
