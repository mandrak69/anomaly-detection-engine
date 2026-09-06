import sqlite3
from collections.abc import Callable

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

    Idempotent, not transactional: Python's sqlite3 module does not roll
    back DDL (CREATE/ALTER/DROP) or PRAGMA statements the way it does
    plain DML, even inside `with connection:` -- verified directly (a
    CREATE TABLE/ALTER TABLE/PRAGMA survives an exception raised right
    after it inside a `with connection:` block, unlike an INSERT in the
    same position). True cross-statement atomicity for this kind of
    migration isn't available without Python 3.12's `autocommit=False`,
    which this project can't rely on (requires-python >=3.11). So instead
    every step here is safe to re-run: _add_column_if_missing() only
    ALTERs a column that isn't already there (a bare second ALTER TABLE
    ADD COLUMN would raise "duplicate column"), and each index rebuild
    already DROPs (IF EXISTS) immediately before recreating it, so a
    retry after a crash between any two statements here -- including one
    between this function finishing and migrate() recording the new
    PRAGMA user_version below -- converges to the same end state rather
    than erroring.
    """
    _add_column_if_missing(connection, "signals", "market_rules", "TEXT")
    _add_column_if_missing(connection, "signals", "market_specifier", "TEXT")
    _add_column_if_missing(connection, "movements", "market_rules", "TEXT")
    _add_column_if_missing(connection, "movements", "market_specifier", "TEXT")

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


def _add_column_if_missing(
    connection: sqlite3.Connection, table: str, column: str, column_type: str
) -> None:
    """ALTER TABLE ADD COLUMN has no IF NOT EXISTS in SQLite, and running
    it twice raises "duplicate column name" -- this makes it safe to
    re-run, which migrations need to be (see
    _migration_2_full_market_identity's docstring for why).
    """
    existing_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing_columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def _migration_3_competitions(connection: sqlite3.Connection) -> None:
    """Adds the canonical competition registry FixtureCatalog uses to
    resolve raw league/competition strings the same way it already
    resolves team names -- see storage.fixture_catalog. Every statement
    is its own idempotent CREATE ... IF NOT EXISTS, so this is safe to
    re-run for the same reason migration 1 is.
    """
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS competitions (
            id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            sport TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_competitions_name_sport
            ON competitions(canonical_name, sport);

        CREATE TABLE IF NOT EXISTS source_competition_mappings (
            source TEXT NOT NULL,
            sport TEXT NOT NULL,
            source_competition_name TEXT NOT NULL,
            competition_id TEXT NOT NULL REFERENCES competitions(id),
            PRIMARY KEY (source, sport, source_competition_name)
        );
        """
    )


MIGRATIONS: list[Migration] = [
    _migration_1_initial_schema,
    _migration_2_full_market_identity,
    _migration_3_competitions,
]


def migrate(connection: sqlite3.Connection) -> None:
    """Brings connection's database up to the latest schema version,
    applying only the migrations it hasn't already recorded (via
    PRAGMA user_version) as applied. Safe to call on every startup,
    including against a freshly created empty database (user_version
    starts at 0, so every migration runs) or one already fully migrated
    (none do).

    Not wrapped in a transaction: SQLite's DDL/PRAGMA statements are not
    rolled back by Python's sqlite3 module the way plain DML is (a
    portability constraint, not an oversight -- true DDL transactions
    need Python 3.12's autocommit=False, and this project supports
    >=3.11). If the process is interrupted between a migration finishing
    and the PRAGMA user_version write just below landing, the next
    startup re-runs that same migration -- which is exactly why every
    migration must be idempotent (safe to apply twice), not merely
    "wrapped in a transaction" that SQLite would not actually honor here.
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
