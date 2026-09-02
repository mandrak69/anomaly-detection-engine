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
            market_type TEXT NOT NULL,
            market_period TEXT NOT NULL,
            market_line TEXT,
            market_rules TEXT,
            market_specifier TEXT,
            outcome TEXT NOT NULL,
            odds TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            source_timestamp TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_odds_event
            ON odds_snapshots(event_id);

        CREATE INDEX IF NOT EXISTS idx_odds_event_time
            ON odds_snapshots(event_id, observed_at);

        CREATE INDEX IF NOT EXISTS idx_odds_event_market
            ON odds_snapshots(event_id, market_type, market_period, market_line);

        CREATE UNIQUE INDEX IF NOT EXISTS uq_odds_snapshot_dedupe
            ON odds_snapshots(
                event_id,
                bookmaker_id,
                market_type,
                market_period,
                COALESCE(market_line, ''),
                outcome,
                observed_at
            );

        CREATE TABLE IF NOT EXISTS collector_runs (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            status TEXT NOT NULL,
            records_received INTEGER NOT NULL,
            records_accepted INTEGER NOT NULL,
            records_rejected INTEGER NOT NULL,
            collector_version TEXT,
            error_type TEXT,
            error_message TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_collector_runs_source
            ON collector_runs(source, started_at);

        CREATE TABLE IF NOT EXISTS raw_payloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collector_run_id TEXT NOT NULL,
            source TEXT NOT NULL,
            payload TEXT NOT NULL,
            accepted INTEGER NOT NULL,
            rejection_reason TEXT,
            received_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_raw_payloads_run
            ON raw_payloads(collector_run_id);

        CREATE TABLE IF NOT EXISTS teams (
            id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            sport TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_teams_name_sport
            ON teams(canonical_name, sport);

        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            sport TEXT NOT NULL,
            league TEXT NOT NULL,
            home_team_id TEXT NOT NULL REFERENCES teams(id),
            away_team_id TEXT NOT NULL REFERENCES teams(id),
            start_time TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_teams_time
            ON events(home_team_id, away_team_id, start_time);

        CREATE TABLE IF NOT EXISTS source_team_mappings (
            source TEXT NOT NULL,
            sport TEXT NOT NULL,
            source_team_name TEXT NOT NULL,
            team_id TEXT NOT NULL REFERENCES teams(id),
            PRIMARY KEY (source, sport, source_team_name)
        );
        """
    )

    connection.commit()