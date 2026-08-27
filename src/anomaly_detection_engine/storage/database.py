import sqlite3
from pathlib import Path


def create_connection(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS odds_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            bookmaker_id TEXT NOT NULL,
            bookmaker_name TEXT NOT NULL,
            market TEXT NOT NULL,
            outcome TEXT NOT NULL,
            odds REAL NOT NULL,
            observed_at TEXT NOT NULL,
            source_timestamp TEXT
        );
        );

        CREATE INDEX IF NOT EXISTS idx_odds_event
            ON odds_snapshots(event_id);

        CREATE INDEX IF NOT EXISTS idx_odds_event_time
            ON odds_snapshots(event_id, observed_at);
        """
    )

    connection.commit()