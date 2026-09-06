import sqlite3
from typing import Callable

Migration = Callable[[sqlite3.Connection], None]

# Applied in order, tracked via PRAGMA user_version (an integer already
# built into every SQLite file for exactly this purpose). MIGRATIONS[i]
# is "version i+1" -- migrate() below runs only the migrations a given
# database hasn't seen yet, so an existing persistent data/*.db file
# (created by an earlier version of this schema) is brought forward
# in place, rather than requiring "just delete the file" as the answer
# to every schema change. That workaround is fine for a scratch/test
# database, but not something to rely on for a real deployment.
#
# Once a migration has shipped, its SQL must never be edited -- a
# database that already recorded it as applied (via user_version) will
# never run it again, so a later fix has to be its own new migration,
# the same discipline any migration-based system requires.


def _migration_1_initial_schema(connection: sqlite3.Connection) -> None:
    """The schema as it existed when this project first started writing
    to a persistent file (see git history) -- odds_snapshots already had
    full MarketIdentity columns, but signals/movements did not yet, and
    none of the identity indexes covered market_rules/market_specifier.
    Every statement here is its own idempotent CREATE ... IF NOT EXISTS,
    so this migration is safe even if it partially re-runs.
    """
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

        CREATE TABLE IF NOT EXISTS signals (
            id TEXT PRIMARY KEY,
            signal_type TEXT NOT NULL,
            event_id TEXT NOT NULL,
            market_type TEXT NOT NULL,
            market_period TEXT NOT NULL,
            market_line TEXT,
            outcome TEXT,
            status TEXT NOT NULL,
            edge_percent TEXT NOT NULL,
            details TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            resolved_at TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_signals_identity
            ON signals(
                signal_type,
                event_id,
                market_type,
                market_period,
                COALESCE(market_line, ''),
                COALESCE(outcome, '')
            );

        CREATE INDEX IF NOT EXISTS idx_signals_status
            ON signals(status);

        CREATE TABLE IF NOT EXISTS movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            market_type TEXT NOT NULL,
            market_period TEXT NOT NULL,
            market_line TEXT,
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

        CREATE UNIQUE INDEX IF NOT EXISTS uq_movements_transition
            ON movements(
                event_id,
                bookmaker_id,
                market_type,
                market_period,
                COALESCE(market_line, ''),
                outcome,
                previous_observed_at,
                current_observed_at
            );

        CREATE INDEX IF NOT EXISTS idx_movements_event
            ON movements(event_id, detected_at);
        """
    )


def _migration_2_full_market_identity(connection: sqlite3.Connection) -> None:
    """Adds market_rules/market_specifier to signals and movements (they
    were missing entirely -- only odds_snapshots had them from the
    start), and rebuilds every identity/dedupe index that must cover the
    full MarketIdentity tuple but previously stopped at market_line. See
    the "Fix MarketIdentity propagation..." commit this mirrors.

    Wrapped in one transaction: this alters multiple tables and rebuilds
    several indexes, and there is no safe halfway state between them --
    an interrupted run must roll back entirely, not leave e.g.
    market_rules added but the index rebuild half-done.
    """
    with connection:
        connection.execute("ALTER TABLE signals ADD COLUMN market_rules TEXT")
        connection.execute("ALTER TABLE signals ADD COLUMN market_specifier TEXT")
        connection.execute("ALTER TABLE movements ADD COLUMN market_rules TEXT")
        connection.execute("ALTER TABLE movements ADD COLUMN market_specifier TEXT")

        connection.execute("DROP INDEX IF EXISTS idx_odds_event_market")
        connection.execute(
            """
            CREATE INDEX idx_odds_event_market
                ON odds_snapshots(
                    event_id, market_type, market_period, market_line,
                    market_rules, market_specifier
                )
            """
        )

        connection.execute("DROP INDEX IF EXISTS uq_odds_snapshot_dedupe")
        connection.execute(
            """
            CREATE UNIQUE INDEX uq_odds_snapshot_dedupe
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
                )
            """
        )

        connection.execute("DROP INDEX IF EXISTS uq_signals_identity")
        connection.execute(
            """
            CREATE UNIQUE INDEX uq_signals_identity
                ON signals(
                    signal_type,
                    event_id,
                    market_type,
                    market_period,
                    COALESCE(market_line, ''),
                    COALESCE(market_rules, ''),
                    COALESCE(market_specifier, ''),
                    COALESCE(outcome, '')
                )
            """
        )

        connection.execute("DROP INDEX IF EXISTS uq_movements_transition")
        connection.execute(
            """
            CREATE UNIQUE INDEX uq_movements_transition
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
                )
            """
        )


MIGRATIONS: list[Migration] = [
    _migration_1_initial_schema,
    _migration_2_full_market_identity,
]


def migrate(connection: sqlite3.Connection) -> None:
    """Brings connection's database up to the latest schema version,
    applying only the migrations it hasn't already recorded (via
    PRAGMA user_version) as applied. Safe to call on every startup,
    including against a freshly created empty database (user_version
    starts at 0, so every migration runs) or one already fully migrated
    (none do).
    """
    current_version = connection.execute("PRAGMA user_version").fetchone()[0]

    for version, migration in enumerate(MIGRATIONS, start=1):
        if version <= current_version:
            continue
        migration(connection)
        # PRAGMA doesn't accept bound parameters -- version is an int we
        # generated ourselves (the loop index), never external input.
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
