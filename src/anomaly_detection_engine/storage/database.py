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
            ON odds_snapshots(
                event_id, market_type, market_period, market_line,
                market_rules, market_specifier
            );

        -- Full MarketIdentity (type + period + line + rules + specifier),
        -- not just type/period/line: two snapshots that only differ in
        -- rules/specifier are genuinely different markets (see
        -- models.market.MarketIdentity) and must not be deduped together.
        CREATE UNIQUE INDEX IF NOT EXISTS uq_odds_snapshot_dedupe
            ON odds_snapshots(
                event_id,
                bookmaker_id,
                market_type,
                market_period,
                COALESCE(market_line, ''),
                COALESCE(market_rules, ''),
                COALESCE(market_specifier, ''),
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

        -- Stateful signals (SUREBET, VALUE_GAP): a condition that can
        -- persist across multiple poll cycles. Identity deliberately
        -- excludes which bookmaker/odds are currently involved -- those
        -- are "current state" that gets updated in place, not part of
        -- what makes two detections "the same" opportunity. `details` is
        -- a JSON blob for the type-specific current state (surebet legs,
        -- or the single outlier bookmaker/odds for a value gap).
        CREATE TABLE IF NOT EXISTS signals (
            id TEXT PRIMARY KEY,
            signal_type TEXT NOT NULL,
            event_id TEXT NOT NULL,
            market_type TEXT NOT NULL,
            market_period TEXT NOT NULL,
            market_line TEXT,
            market_rules TEXT,
            market_specifier TEXT,
            outcome TEXT,
            status TEXT NOT NULL,
            edge_percent TEXT NOT NULL,
            details TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            resolved_at TEXT
        );

        -- Full MarketIdentity, same rule as odds_snapshots above -- a
        -- signal's identity must not conflate two different markets that
        -- only differ in rules/specifier.
        CREATE UNIQUE INDEX IF NOT EXISTS uq_signals_identity
            ON signals(
                signal_type,
                event_id,
                market_type,
                market_period,
                COALESCE(market_line, ''),
                COALESCE(market_rules, ''),
                COALESCE(market_specifier, ''),
                COALESCE(outcome, '')
            );

        CREATE INDEX IF NOT EXISTS idx_signals_status
            ON signals(status);

        -- Movements are point-in-time events (a transition that already
        -- happened), not an ongoing condition -- append-only, no status,
        -- no reconciliation. Unlike `signals`, a duplicate here would
        -- mean the same (event, bookmaker, outcome) transition was
        -- reported twice for the exact same pair of readings, so it is
        -- deduped on the full transition, not merged/updated like a
        -- stateful signal would be.
        CREATE TABLE IF NOT EXISTS movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            market_type TEXT NOT NULL,
            market_period TEXT NOT NULL,
            market_line TEXT,
            market_rules TEXT,
            market_specifier TEXT,
            outcome TEXT NOT NULL,
            bookmaker_id TEXT NOT NULL,
            bookmaker_name TEXT NOT NULL,
            previous_odds TEXT NOT NULL,
            current_odds TEXT NOT NULL,
            change_percent TEXT NOT NULL,
            previous_observed_at TEXT NOT NULL,
            current_observed_at TEXT NOT NULL,
            detected_at TEXT NOT NULL
        );

        -- Full MarketIdentity, same rule as odds_snapshots above.
        CREATE UNIQUE INDEX IF NOT EXISTS uq_movements_transition
            ON movements(
                event_id,
                bookmaker_id,
                market_type,
                market_period,
                COALESCE(market_line, ''),
                COALESCE(market_rules, ''),
                COALESCE(market_specifier, ''),
                outcome,
                previous_observed_at,
                current_observed_at
            );

        CREATE INDEX IF NOT EXISTS idx_movements_event
            ON movements(event_id, detected_at);
        """
    )

    connection.commit()