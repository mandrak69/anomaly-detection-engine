import sqlite3
from pathlib import Path

from anomaly_detection_engine.storage.migrations import migrate


def configure_connection(connection: sqlite3.Connection) -> None:
    """Applies every per-connection setting a connection to this
    database needs, regardless of how it was opened -- row access by
    column name, and foreign-key enforcement. SQLite does not enable FK
    enforcement by default on a new connection (it is off unless a
    connection explicitly turns it on, even though the schema itself
    declares REFERENCES), so every connection this project opens must
    call this, not just the ones that happen to go through
    create_connection() below.
    """
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")


def create_connection(db_path: str | Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=timeout)
    configure_connection(connection)
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    migrate(connection)
