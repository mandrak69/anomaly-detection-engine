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

    Idempotent, not transactional: Python's sqlite3 module (legacy
    isolation_level-based transaction handling) only auto-opens an
    implicit transaction before DML (INSERT/UPDATE/DELETE), never before
    DDL (CREATE/ALTER/DROP) or PRAGMA -- so a bare execute() of one of
    those runs in autocommit mode and survives a later rollback()/an
    exception inside `with connection:`, even though SQLite itself can
    genuinely roll back DDL -- verified directly (a CREATE TABLE/
    ALTER TABLE/PRAGMA survives an exception raised right after it
    inside a `with connection:` block, unlike an INSERT in the same
    position). DDL *can* be made transactional with an explicit BEGIN
    first (the same technique FixtureCatalog.match() uses for its own
    writes) -- Python 3.12's `autocommit=False` is not actually required
    for that. The real obstacle for *this* migration specifically is
    that migration 1/3 use executescript(), which always commits any
    pending transaction before running the script, so wrapping it in a
    manual BEGIN would just get silently committed away before the
    script even starts. So instead every step here is made safe to
    re-run: _add_column_if_missing() only ALTERs a column that isn't
    already there (a bare second ALTER TABLE ADD COLUMN would raise
    "duplicate column"), and each index rebuild already DROPs
    (IF EXISTS) immediately before recreating it, so a retry after a
    crash between any two statements here -- including one between this
    function finishing and migrate() recording the new PRAGMA
    user_version below -- converges to the same end state rather than
    erroring.
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
    connection: sqlite3.Connection,
    table: str,
    column: str,
    column_type: str,
    *,
    default_sql: str | None = None,
) -> None:
    """ALTER TABLE ADD COLUMN has no IF NOT EXISTS in SQLite, and running
    it twice raises "duplicate column name" -- this makes it safe to
    re-run, which migrations need to be (see
    _migration_2_full_market_identity's docstring for why). default_sql,
    when given, becomes a `DEFAULT <default_sql>` clause -- required to
    add a NOT NULL column to a table that already has rows (SQLite
    allows this only when a non-NULL default is supplied).
    """
    existing_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing_columns:
        default_clause = f" DEFAULT {default_sql}" if default_sql else ""
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {column_type}{default_clause}"
        )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    """The sqlite_master check a table-rebuild migration needs to be
    resumable from any interruption point (see
    _migration_11_collector_run_running_status) -- "CREATE TABLE IF NOT
    EXISTS" covers re-running a CREATE safely, but DROP TABLE/ALTER
    TABLE ... RENAME TO have no equivalent conditional form, so each step
    of a rebuild needs its own explicit existence check instead.
    """
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


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


def _migration_4_market_phase(connection: sqlite3.Connection) -> None:
    """Adds market_phase (PRE_MATCH vs LIVE, see models.market.MarketPhase)
    to odds_snapshots/signals/movements and rebuilds every identity/
    dedupe index to include it -- a pre-match price and a live price for
    the same event/market/outcome are not the same market and must never
    be compared or deduped together.

    Existing rows are backfilled as 'pre_match': every source this
    project ingested before this migration (the JSON demo, the-odds-api)
    was genuinely pre-match, and Mozzart's live captures were -- this is
    exactly the bug this migration fixes going forward -- already being
    treated as indistinguishable from pre-match data, so backfilling them
    as 'pre_match' preserves how they compared before rather than
    fabricating a phase this migration cannot actually recover from the
    data as stored.
    """
    _add_column_if_missing(
        connection, "odds_snapshots", "market_phase", "TEXT NOT NULL", default_sql="'pre_match'"
    )
    _add_column_if_missing(
        connection, "signals", "market_phase", "TEXT NOT NULL", default_sql="'pre_match'"
    )
    _add_column_if_missing(
        connection, "movements", "market_phase", "TEXT NOT NULL", default_sql="'pre_match'"
    )

    connection.execute("DROP INDEX IF EXISTS idx_odds_event_market")
    connection.execute(
        """
        CREATE INDEX idx_odds_event_market
            ON odds_snapshots(
                event_id, market_type, market_period, market_phase,
                market_line, market_rules, market_specifier
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
                market_phase,
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
                market_phase,
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
                market_phase,
                COALESCE(market_line, ''),
                COALESCE(market_rules, ''),
                COALESCE(market_specifier, ''),
                outcome,
                previous_observed_at,
                current_observed_at
            )
        """
    )


def _migration_5_event_competition_id(connection: sqlite3.Connection) -> None:
    """Adds events.competition_id (FK to competitions) and backfills it,
    so FixtureCatalog can key an event's identity on the competition's
    stable id instead of its display string (events.league) -- renaming
    a canonical competition's name later would otherwise silently break
    matching for every event still keyed on the old league text.

    A database that only ever ran migration 3 (which created the
    competitions table but never backfilled it from pre-existing events)
    can have events whose league was never registered as a competitions
    row -- the first statement here fills that gap by inserting one
    competitions row per distinct (sport, league) still missing one.
    Every Event.league value already *is* a canonical name (see
    FixtureCatalog._resolve_competition), so this is an exact join, not
    a guess, the same reasoning migration 4's backfill relies on.

    competition_id is left nullable at the SQL level rather than
    NOT NULL -- the same trade-off already made for market_line/
    market_rules/market_specifier: every row this migration touches gets
    backfilled, and FixtureCatalog always sets it for every row it
    creates from now on, but a NOT NULL constraint here would need a
    single literal DEFAULT, which can't express "look up the right id
    per row".
    """
    _add_column_if_missing(
        connection, "events", "competition_id", "TEXT REFERENCES competitions(id)"
    )

    connection.execute(
        """
        INSERT OR IGNORE INTO competitions (id, canonical_name, sport)
        SELECT 'competition-' || lower(hex(randomblob(5))), e.league, e.sport
        FROM events e
        WHERE NOT EXISTS (
            SELECT 1 FROM competitions c
            WHERE c.canonical_name = e.league AND c.sport = e.sport
        )
        GROUP BY e.sport, e.league
        """
    )

    connection.execute(
        """
        UPDATE events
        SET competition_id = (
            SELECT c.id FROM competitions c
            WHERE c.canonical_name = events.league AND c.sport = events.sport
        )
        WHERE competition_id IS NULL
        """
    )


def _migration_6_collector_run_provenance(connection: sqlite3.Connection) -> None:
    """Adds provider_id/parser_version/source_payload to collector_runs,
    so a run's exact source response survives alongside the RawEventOdds
    already stored per-record in raw_payloads (see
    storage.raw_payload_repository) -- without the original payload, a
    parser bug can only be fixed going forward; with it, a fixed parser
    can be re-run against exactly what a source returned historically.

    One row per CollectorRun, not per raw_payloads row: source_payload
    is the one response a whole run's records were parsed from (a
    the-odds-api poll returns many bookmakers across many events in one
    response), so storing it once per run avoids repeating the same
    payload once per record it produced.

    All three columns are nullable and left unbackfilled for existing
    rows -- no collector captured a raw payload before this migration,
    so there is nothing to recover, the same reasoning migration 4's
    market_phase backfill did not need to apply here.
    """
    _add_column_if_missing(connection, "collector_runs", "provider_id", "TEXT")
    _add_column_if_missing(connection, "collector_runs", "parser_version", "TEXT")
    _add_column_if_missing(connection, "collector_runs", "source_payload", "TEXT")


def _migration_7_bookmaker_catalog(connection: sqlite3.Connection) -> None:
    """Adds the canonical bookmaker registry BookmakerCatalog uses to
    resolve a raw (provider, bookmaker) sighting to one stable identity
    across providers -- see storage.bookmaker_catalog. Deliberately not a
    backfill of odds_snapshots.bookmaker_id/movements.bookmaker_id: every
    row written before this migration already has a stable bookmaker_id
    string (the old raw.source_id-or-lowercased-name scheme), and
    retroactively resolving those historical strings through the new
    catalog is a one-off data-migration script, not something this
    schema migration should silently attempt. Every statement here is
    its own idempotent CREATE ... IF NOT EXISTS, so this is safe to
    re-run for the same reason migration 1/3 are.
    """
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS bookmakers (
            id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_bookmakers_normalized_name
            ON bookmakers(normalized_name);

        CREATE TABLE IF NOT EXISTS source_bookmaker_mappings (
            provider_id TEXT NOT NULL,
            source_bookmaker_id TEXT NOT NULL,
            source_name TEXT NOT NULL,
            bookmaker_id TEXT NOT NULL REFERENCES bookmakers(id),
            PRIMARY KEY (provider_id, source_bookmaker_id)
        );
        """
    )


def _migration_8_odds_snapshot_provenance(connection: sqlite3.Connection) -> None:
    """Adds odds_snapshots.collector_run_id -- which CollectorRun
    actually produced each snapshot -- closing the one remaining gap in
    this project's provenance chain: collector_runs already records
    provider_id/parser_version/source_payload per run (migration 6), but
    nothing on odds_snapshots pointed back to which run any given row
    came from, so "this Bet365 quote of 2.15 came from which
    API-Football poll" was unanswerable without guessing from timing.

    Deliberately NOT a SQL foreign key (unlike the REFERENCES columns
    migration 3/7 added): at the time this migration shipped,
    OddsIngestionService.run() generated its own run_id and started
    saving odds_snapshots rows against it *before* the matching
    collector_runs row was written (that only happened at the very end)
    -- a real FK constraint, with this project's connections running
    with PRAGMA foreign_keys = ON, would have rejected every snapshot
    insert since the parent row didn't exist yet at that point.
    odds_snapshots.event_id/bookmaker_id already follow this same "plain
    TEXT, no REFERENCES" precedent for an unrelated reason (see
    migration 1); this column followed it for its own, ordering-specific
    reason.

    Migration 11 later closed that ordering gap (the collector_runs row
    is now written as RUNNING before any snapshot referencing it), and
    migration 13 adds the real FK this column always should have had
    once that was safe.

    Nullable and left unbackfilled for existing rows, the same reasoning
    migration 6's own nullable columns used: no row written before this
    migration recorded which run produced it, so there is nothing to
    recover -- NULL here means "provenance unknown" (pre-migration data,
    or a snapshot saved directly rather than through
    OddsIngestionService), a legitimate, permanent state this project's
    query logic (see OddsRepository.find_last_two_same_provider) already
    has to handle, not a temporary gap this migration should try to
    paper over.
    """
    _add_column_if_missing(connection, "odds_snapshots", "collector_run_id", "TEXT")


def _migration_9_event_status(connection: sqlite3.Connection) -> None:
    """Adds event_status, a one-row-per-event table tracking the latest
    known models.market.EventLifecycle for a canonical event -- see
    storage.event_status_repository. Separate from the events table
    itself (rather than a column on it) because lifecycle is observed,
    mutable state that changes on every poll a status-reporting collector
    (currently only ApiFootballCollector) touches, unlike every existing
    events column, which is fixed identity decided once at creation --
    keeping it in its own table means Event (models.event) stays a pure
    identity value, and a provider that never reports status simply never
    gets a row here rather than forcing every Event construction
    elsewhere to carry a lifecycle field it cannot actually populate.

    No FK to events(id): consistent with collector_run_id's ordering
    reasoning (migration 8) is not the issue here (events always exist
    before this is written, via FixtureCatalog.match() already having
    run) -- plain TEXT is used instead simply to match every other
    id-shaped column in this schema (event_id, bookmaker_id, ...), none
    of which are FKs either except where a migration specifically needed
    referential integrity for a resolve-or-create cache (source_team_
    mappings, source_bookmaker_mappings).
    """
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS event_status (
            event_id TEXT PRIMARY KEY,
            lifecycle TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )


def _migration_10_source_event_mappings(connection: sqlite3.Connection) -> None:
    """Adds source_event_mappings, the same resolve-once-cache-forever
    pattern source_team_mappings/source_competition_mappings already use
    (see storage.fixture_catalog), but keyed on a provider's own stable
    event/fixture id (e.g. api-football's fixture.id -- see
    RawEventOdds.source_event_id) instead of a raw name needing fuzzy
    resolution. A provider with no such stable id (the JSON demo,
    Mozzart, the-odds-api at time of writing) simply never gets a row
    here; FixtureCatalog.match() falls back to its existing team/
    competition/start_time resolution exactly as before whenever
    source_event_id is absent or not yet mapped.

    `source` (not `provider_id`) is the column name here for the same
    internal-storage-detail reason FixtureCatalog's own docstring gives
    for source_team_mappings/source_competition_mappings: every
    Python-facing name is provider_id, but renaming the column would be
    a migration with no external consumer benefiting from it.
    """
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_event_mappings (
            source TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            event_id TEXT NOT NULL REFERENCES events(id),
            PRIMARY KEY (source, source_event_id)
        );
        """
    )


def _migration_11_collector_run_running_status(connection: sqlite3.Connection) -> None:
    """Relaxes collector_runs.finished_at from NOT NULL to nullable, so a
    row can be inserted while a run is still in progress (status=RUNNING,
    finished_at=None) and updated in place once it finishes -- see
    CollectorRunStatus.RUNNING, CollectorRun.finished_at,
    CollectorRunRepository.start()/finish(), and
    OddsIngestionService.run().

    Before this, OddsIngestionService.run() generated its own run_id up
    front but only ever wrote the matching collector_runs row at the
    very end (via what is now CollectorRunRepository.save()) -- every
    odds_snapshots/raw_payloads row this run produced was written with
    that run_id in the meantime, referencing a run that, if the process
    had died before reaching that final write, would never have existed
    at all (see migration 8's docstring, which documents this exact
    ordering and why collector_run_id deliberately isn't a SQL foreign
    key). Starting the row as RUNNING closes that window: from this
    migration forward, every snapshot/raw-payload row is written only
    after its run's own row already exists, so a crash mid-run leaves an
    honestly-stuck RUNNING row behind (observable, not silently missing)
    rather than a snapshot pointing at nothing.

    SQLite has no ALTER TABLE ... ALTER COLUMN to drop a NOT NULL
    constraint, so the table is rebuilt: a new table under the relaxed
    schema, existing rows copied across, the old table dropped, the new
    one renamed into place. Every step is guarded by a Python-level
    sqlite_master check (not just "IF NOT EXISTS" in the SQL, which
    doesn't cover DROP/RENAME the same way `_add_column_if_missing`'s
    ALTER guard covers ADD COLUMN) so a crash between any two steps
    converges to the same end state on retry -- the same idempotency
    discipline every migration here follows, just applied to a rebuild
    instead of a single ALTER/CREATE. collector_runs is small (nowhere
    near the size of odds_snapshots), so a full rebuild costs nothing
    worth optimizing away.
    """
    if not _table_exists(connection, "collector_runs_new"):
        connection.execute(
            """
            CREATE TABLE collector_runs_new (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                records_received INTEGER NOT NULL,
                records_accepted INTEGER NOT NULL,
                records_rejected INTEGER NOT NULL,
                collector_version TEXT,
                error_type TEXT,
                error_message TEXT,
                provider_id TEXT,
                parser_version TEXT,
                source_payload TEXT
            )
            """
        )

    if _table_exists(connection, "collector_runs"):
        connection.execute(
            "INSERT OR IGNORE INTO collector_runs_new SELECT * FROM collector_runs"
        )
        connection.execute("DROP TABLE collector_runs")

    connection.execute("ALTER TABLE collector_runs_new RENAME TO collector_runs")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_collector_runs_source ON collector_runs(source, started_at)"
    )


def _migration_12_mapping_resolution_audit(connection: sqlite3.Connection) -> None:
    """Adds resolution_method/confidence/created_at to
    source_team_mappings and source_competition_mappings -- see
    FixtureCatalog._save_mapping/_save_competition_mapping.

    FixtureCatalog already computes exactly this information every time
    it resolves a raw name (TeamNormalizer.normalize()'s own
    NormalizationResult -- "exact"/"alias"/"fuzzy"/"ambiguous"/"unknown"
    plus a confidence score), but only ever logged it for the
    "ambiguous" case before this, then discarded it once the mapping row
    was written. A mapping is cached *permanently* the moment it's
    created (see FixtureCatalog's own docstring) -- a wrong fuzzy match
    becomes a permanently wrong mapping just as easily as a correct one
    becomes a permanently correct one, and this project has already hit
    exactly that in practice (a rapidfuzz scorer bug that silently
    merged distinct teams -- see TeamNormalizer's own docstring). Without
    this, finding which existing mappings were low-confidence guesses
    worth a human's review meant re-deriving it after the fact by
    re-running the matcher against current data, which cannot reconstruct
    what the catalog actually looked like at the moment the original
    resolution happened (more teams may have been added since, changing
    what today's fuzzy match would even find).

    All three columns are nullable and left unbackfilled for existing
    rows, the same reasoning migration 6/8's own nullable columns used:
    no row written before this migration recorded which method/
    confidence produced it, so there is nothing to honestly recover --
    NULL means "resolution provenance unknown (pre-migration mapping)",
    a legitimate permanent state, not a gap to paper over with a
    fabricated value.
    """
    _add_column_if_missing(connection, "source_team_mappings", "resolution_method", "TEXT")
    _add_column_if_missing(connection, "source_team_mappings", "confidence", "REAL")
    _add_column_if_missing(connection, "source_team_mappings", "created_at", "TEXT")
    _add_column_if_missing(
        connection, "source_competition_mappings", "resolution_method", "TEXT"
    )
    _add_column_if_missing(connection, "source_competition_mappings", "confidence", "REAL")
    _add_column_if_missing(connection, "source_competition_mappings", "created_at", "TEXT")


def _migration_13_odds_snapshot_and_raw_payload_foreign_keys(
    connection: sqlite3.Connection,
) -> None:
    """Adds real SQL foreign keys from odds_snapshots.collector_run_id
    and raw_payloads.collector_run_id to collector_runs(id) -- possible
    now that migration 11 guarantees a run's collector_runs row (written
    as RUNNING) always exists before either table's rows referencing it
    are ever written (see OddsIngestionService.run()). Migration 8's own
    docstring documented exactly why this wasn't safe before that fix:
    a real FK, with this project's connections running PRAGMA
    foreign_keys = ON, would have rejected every snapshot insert since
    the parent row didn't exist yet at that point in the old ordering.

    odds_snapshots.collector_run_id stays nullable -- NULL means
    "provenance unknown" (pre-migration-8 historical data), a legitimate
    permanent state (see migration 8), and NULL never violates a foreign
    key in SQLite regardless -- only a non-NULL value with no matching
    parent row does. raw_payloads.collector_run_id has always been NOT
    NULL (every row, from migration 1 onward, was written with a real
    run_id) and stays that way.

    Both tables are rebuilt the same way migration 11 rebuilt
    collector_runs: a new table under the target schema, existing rows
    copied across, the old table dropped, the new one renamed into
    place, every step guarded by the same Python-level sqlite_master
    check for resumability after an interruption -- see migration 11's
    docstring for the full reasoning (SQLite has no ALTER TABLE ... ADD
    CONSTRAINT). odds_snapshots is this project's largest table by a
    wide margin; confirmed empirically before this shipped, against a
    copy of the real production database, that the rebuild completes in
    well under a second and PRAGMA foreign_key_check finds zero
    violations afterward (every existing non-NULL collector_run_id
    already referenced a real collector_runs row -- verified separately,
    before writing this migration, precisely so it would ship only once
    that was confirmed true).
    """
    if not _table_exists(connection, "odds_snapshots_new"):
        connection.execute(
            """
            CREATE TABLE odds_snapshots_new (
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
                source_timestamp TEXT,
                market_phase TEXT NOT NULL DEFAULT 'pre_match',
                collector_run_id TEXT REFERENCES collector_runs(id)
            )
            """
        )

    if _table_exists(connection, "odds_snapshots"):
        connection.execute(
            "INSERT OR IGNORE INTO odds_snapshots_new SELECT * FROM odds_snapshots"
        )
        connection.execute("DROP TABLE odds_snapshots")

    connection.execute("ALTER TABLE odds_snapshots_new RENAME TO odds_snapshots")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_odds_event ON odds_snapshots(event_id)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_odds_event_time ON odds_snapshots(event_id, observed_at)"
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_odds_event_market
            ON odds_snapshots(
                event_id, market_type, market_period, market_phase,
                market_line, market_rules, market_specifier
            )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_odds_snapshot_dedupe
            ON odds_snapshots(
                event_id,
                bookmaker_id,
                market_type,
                market_period,
                market_phase,
                COALESCE(market_line, ''),
                COALESCE(market_rules, ''),
                COALESCE(market_specifier, ''),
                outcome,
                observed_at
            )
        """
    )

    if not _table_exists(connection, "raw_payloads_new"):
        connection.execute(
            """
            CREATE TABLE raw_payloads_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collector_run_id TEXT NOT NULL REFERENCES collector_runs(id),
                source TEXT NOT NULL,
                payload TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                rejection_reason TEXT,
                received_at TEXT NOT NULL
            )
            """
        )

    if _table_exists(connection, "raw_payloads"):
        connection.execute("INSERT OR IGNORE INTO raw_payloads_new SELECT * FROM raw_payloads")
        connection.execute("DROP TABLE raw_payloads")

    connection.execute("ALTER TABLE raw_payloads_new RENAME TO raw_payloads")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_raw_payloads_run ON raw_payloads(collector_run_id)"
    )


MIGRATIONS: list[Migration] = [
    _migration_1_initial_schema,
    _migration_2_full_market_identity,
    _migration_3_competitions,
    _migration_4_market_phase,
    _migration_5_event_competition_id,
    _migration_6_collector_run_provenance,
    _migration_7_bookmaker_catalog,
    _migration_8_odds_snapshot_provenance,
    _migration_9_event_status,
    _migration_10_source_event_mappings,
    _migration_11_collector_run_running_status,
    _migration_12_mapping_resolution_audit,
    _migration_13_odds_snapshot_and_raw_payload_foreign_keys,
]


def migrate(connection: sqlite3.Connection) -> None:
    """Brings connection's database up to the latest schema version,
    applying only the migrations it hasn't already recorded (via
    PRAGMA user_version) as applied. Safe to call on every startup,
    including against a freshly created empty database (user_version
    starts at 0, so every migration runs) or one already fully migrated
    (none do).

    Not wrapped in a transaction: Python's sqlite3 module only
    auto-opens an implicit transaction before DML, never before DDL/
    PRAGMA, so those run in autocommit mode by default and are not
    rolled back the way plain INSERT/UPDATE/DELETE are (see
    _migration_2_full_market_identity's docstring for the verified
    details, including why an explicit BEGIN -- not actually Python
    3.12's autocommit=False -- would fix this for individual execute()
    calls, and why executescript() specifically defeats even that). If
    the process is interrupted between a migration finishing and the
    PRAGMA user_version write just below landing, the next startup
    re-runs that same migration -- which is exactly why every migration
    must be idempotent (safe to apply twice), not merely "wrapped in a
    transaction" that would not actually cover DDL here anyway.
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
