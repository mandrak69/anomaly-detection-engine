import sqlite3

import pytest

from anomaly_detection_engine.storage.database import configure_connection, initialize_database
from anomaly_detection_engine.storage.migrations import (
    MIGRATIONS,
    _migration_1_initial_schema,
    _migration_2_full_market_identity,
    _migration_3_competitions,
    _migration_4_market_phase,
    _migration_5_event_competition_id,
    _migration_6_collector_run_provenance,
    _migration_11_collector_run_running_status,
    _migration_12_mapping_resolution_audit,
    _migration_13_odds_snapshot_and_raw_payload_foreign_keys,
    _migration_14_signal_history,
    _migration_15_signal_peak_edge_percent,
    _migration_16_retention_cleanup_indexes,
    _migration_17_odds_snapshot_quote_time,
    _migration_19_reference_identity_and_mapping_trust,
)


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


def test_migration_2_is_safe_to_re_run():
    # Regression test for the crash-before-user_version-bump scenario:
    # SQLite's DDL/PRAGMA statements are not rolled back by Python's
    # sqlite3 module (verified directly, see migrate()'s docstring), so
    # a process interrupted between a migration finishing and its
    # PRAGMA user_version write landing would re-run that same migration
    # on the next startup. A bare second `ALTER TABLE ... ADD COLUMN`
    # would raise "duplicate column name" -- migration 2 must tolerate
    # running twice.
    connection = make_connection()
    _migration_1_initial_schema(connection)

    _migration_2_full_market_identity(connection)
    _migration_2_full_market_identity(connection)  # must not raise

    columns = {row[1] for row in connection.execute("PRAGMA table_info(signals)")}
    assert "market_rules" in columns
    assert "market_specifier" in columns
    index_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'uq_movements_transition'"
    ).fetchone()[0]
    assert "market_rules" in index_sql


def test_migration_3_adds_the_competitions_registry():
    connection = make_connection()

    initialize_database(connection)

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "competitions" in tables
    assert "source_competition_mappings" in tables


def test_migration_4_backfills_existing_rows_as_pre_match():
    # A database from before market_phase existed has no way to say
    # what phase its rows actually were -- every source ingested before
    # this migration (JSON demo, the-odds-api) was genuinely pre-match,
    # and Mozzart's live captures were already being treated as
    # indistinguishable from pre-match data (exactly the bug this
    # migration fixes going forward), so 'pre_match' is the correct
    # backfill: it preserves how existing rows compared before, rather
    # than fabricating a phase this migration can't actually recover.
    connection = make_connection()
    _migration_1_initial_schema(connection)
    _migration_2_full_market_identity(connection)
    _migration_3_competitions(connection)
    connection.execute(
        """
        INSERT INTO odds_snapshots (
            event_id, bookmaker_id, bookmaker_name, market_type,
            market_period, outcome, odds, observed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("e1", "mozzart", "Mozzart", "three_way", "full_time", "1", "2.10",
         "2026-01-01T00:00:00+00:00"),
    )
    connection.commit()

    initialize_database(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(odds_snapshots)")}
    assert "market_phase" in columns

    row = connection.execute("SELECT * FROM odds_snapshots WHERE event_id = 'e1'").fetchone()
    assert row["market_phase"] == "pre_match"

    for table, index in [
        ("odds_snapshots", "uq_odds_snapshot_dedupe"),
        ("signals", "uq_signals_identity"),
        ("movements", "uq_movements_transition"),
    ]:
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (index,)
        ).fetchone()[0]
        assert "market_phase" in index_sql, f"{index} on {table} missing market_phase"


def test_migration_4_is_safe_to_re_run():
    connection = make_connection()
    _migration_1_initial_schema(connection)
    _migration_2_full_market_identity(connection)
    _migration_3_competitions(connection)

    _migration_4_market_phase(connection)
    _migration_4_market_phase(connection)  # must not raise

    columns = {row[1] for row in connection.execute("PRAGMA table_info(odds_snapshots)")}
    assert "market_phase" in columns


def _insert_team(connection, team_id, name, sport="football"):
    connection.execute(
        "INSERT INTO teams (id, canonical_name, sport) VALUES (?, ?, ?)",
        (team_id, name, sport),
    )


def _insert_pre_migration_5_event(connection, event_id, league, sport="football"):
    _insert_team(connection, f"{event_id}-home", "A", sport)
    _insert_team(connection, f"{event_id}-away", "B", sport)
    connection.execute(
        """
        INSERT INTO events (id, sport, league, home_team_id, away_team_id, start_time)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (event_id, sport, league, f"{event_id}-home", f"{event_id}-away",
         "2026-01-01T00:00:00+00:00"),
    )


def test_migration_5_backfills_competition_id_from_existing_events():
    # A database that only ever ran migrations 1-3 can have events whose
    # league was never registered in competitions (migration 3 only
    # created the table, it never backfilled it from pre-existing
    # events) -- migration 5 must create a competitions row for such an
    # event's (sport, league) and point events.competition_id at it, not
    # just add an empty column.
    connection = make_connection()
    _migration_1_initial_schema(connection)
    _migration_2_full_market_identity(connection)
    _migration_3_competitions(connection)
    _insert_pre_migration_5_event(connection, "e1", "Premier League")
    connection.commit()

    initialize_database(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
    assert "competition_id" in columns

    row = connection.execute("SELECT * FROM events WHERE id = 'e1'").fetchone()
    assert row["competition_id"] is not None

    competition = connection.execute(
        "SELECT * FROM competitions WHERE id = ?", (row["competition_id"],)
    ).fetchone()
    assert competition["canonical_name"] == "Premier League"
    assert competition["sport"] == "football"


def test_migration_5_reuses_an_existing_competitions_row_instead_of_duplicating():
    connection = make_connection()
    _migration_1_initial_schema(connection)
    _migration_2_full_market_identity(connection)
    _migration_3_competitions(connection)
    connection.execute(
        "INSERT INTO competitions (id, canonical_name, sport) VALUES (?, ?, ?)",
        ("comp-1", "Premier League", "football"),
    )
    _insert_pre_migration_5_event(connection, "e1", "Premier League")
    connection.commit()

    initialize_database(connection)

    row = connection.execute("SELECT * FROM events WHERE id = 'e1'").fetchone()
    assert row["competition_id"] == "comp-1"
    competitions = connection.execute(
        "SELECT COUNT(*) AS n FROM competitions WHERE sport = 'football'"
    ).fetchone()["n"]
    assert competitions == 1


def test_migration_5_is_safe_to_re_run():
    connection = make_connection()
    _migration_1_initial_schema(connection)
    _migration_2_full_market_identity(connection)
    _migration_3_competitions(connection)
    _insert_pre_migration_5_event(connection, "e1", "Premier League")
    connection.commit()

    _migration_5_event_competition_id(connection)
    _migration_5_event_competition_id(connection)  # must not raise

    columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
    assert "competition_id" in columns
    row = connection.execute("SELECT * FROM events WHERE id = 'e1'").fetchone()
    assert row["competition_id"] is not None


def test_migration_6_adds_collector_run_provenance_columns():
    connection = make_connection()
    _migration_1_initial_schema(connection)
    connection.execute(
        """
        INSERT INTO collector_runs (
            id, source, started_at, finished_at, status,
            records_received, records_accepted, records_rejected
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("run-1", "old-source", "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:01+00:00", "success", 1, 1, 0),
    )
    connection.commit()

    initialize_database(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(collector_runs)")}
    assert {"provider_id", "parser_version", "source_payload"} <= columns

    # Pre-existing rows are left unbackfilled (nothing to recover) --
    # not NOT NULL, so this must not raise, and stays NULL.
    row = connection.execute(
        "SELECT * FROM collector_runs WHERE id = 'run-1'"
    ).fetchone()
    assert row["provider_id"] is None
    assert row["source_payload"] is None


def test_migration_6_is_safe_to_re_run():
    connection = make_connection()
    _migration_1_initial_schema(connection)

    _migration_6_collector_run_provenance(connection)
    _migration_6_collector_run_provenance(connection)  # must not raise

    columns = {row[1] for row in connection.execute("PRAGMA table_info(collector_runs)")}
    assert {"provider_id", "parser_version", "source_payload"} <= columns


def test_migration_11_preserves_existing_collector_runs_rows():
    connection = make_connection()
    initialize_database(connection)  # up through migration 10
    connection.execute(
        """
        INSERT INTO collector_runs (
            id, source, started_at, finished_at, status,
            records_received, records_accepted, records_rejected,
            collector_version, provider_id, parser_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("run-1", "mozzart-file:mozzart", "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:04+00:00", "success", 3, 3, 0, "0.1.0", "mozzart", "1"),
    )
    connection.commit()

    _migration_11_collector_run_running_status(connection)

    row = connection.execute("SELECT * FROM collector_runs WHERE id = 'run-1'").fetchone()
    assert row is not None
    assert row["source"] == "mozzart-file:mozzart"
    assert row["finished_at"] == "2026-01-01T00:00:04+00:00"
    assert row["status"] == "success"
    assert row["records_received"] == 3
    assert row["provider_id"] == "mozzart"


def test_migration_11_allows_a_null_finished_at():
    # The entire point of this migration -- a RUNNING row (see
    # CollectorRunStatus.RUNNING) has no finished_at yet. Before this
    # migration, finished_at was NOT NULL and this insert would raise
    # sqlite3.IntegrityError.
    connection = make_connection()
    initialize_database(connection)

    connection.execute(
        """
        INSERT INTO collector_runs (
            id, source, started_at, finished_at, status,
            records_received, records_accepted, records_rejected
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("run-1", "mozzart-file:mozzart", "2026-01-01T00:00:00+00:00",
         None, "running", 0, 0, 0),
    )
    connection.commit()

    row = connection.execute("SELECT * FROM collector_runs WHERE id = 'run-1'").fetchone()
    assert row["finished_at"] is None
    assert row["status"] == "running"


def test_migration_11_preserves_the_source_started_at_index():
    connection = make_connection()
    initialize_database(connection)

    index_names = {
        row[1]
        for row in connection.execute("PRAGMA index_list(collector_runs)")
    }
    assert "idx_collector_runs_source" in index_names


def test_migration_11_is_safe_to_re_run():
    connection = make_connection()
    for migration in MIGRATIONS[:10]:
        migration(connection)
    connection.execute(
        """
        INSERT INTO collector_runs (
            id, source, started_at, finished_at, status,
            records_received, records_accepted, records_rejected
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("run-1", "mozzart-file:mozzart", "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:04+00:00", "success", 3, 3, 0),
    )
    connection.commit()

    _migration_11_collector_run_running_status(connection)
    _migration_11_collector_run_running_status(connection)  # must not raise

    row = connection.execute("SELECT * FROM collector_runs WHERE id = 'run-1'").fetchone()
    assert row is not None
    assert row["source"] == "mozzart-file:mozzart"


def test_migration_11_recovers_from_an_interruption_after_copy_before_drop():
    # Simulates a crash between the copy-into-collector_runs_new step and
    # the drop-old/rename step -- both collector_runs and
    # collector_runs_new exist at once, exactly like a real interrupted
    # run would leave things. Re-running must still converge on one
    # correct final collector_runs table with the row intact.
    connection = make_connection()
    for migration in MIGRATIONS[:10]:
        migration(connection)
    connection.execute(
        """
        INSERT INTO collector_runs (
            id, source, started_at, finished_at, status,
            records_received, records_accepted, records_rejected
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("run-1", "mozzart-file:mozzart", "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:04+00:00", "success", 3, 3, 0),
    )
    connection.commit()

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
    connection.execute("INSERT INTO collector_runs_new SELECT * FROM collector_runs")
    connection.commit()

    _migration_11_collector_run_running_status(connection)

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "collector_runs_new" not in tables
    assert "collector_runs" in tables
    rows = connection.execute("SELECT * FROM collector_runs").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == "run-1"


def test_migration_12_adds_resolution_audit_columns():
    connection = make_connection()
    for migration in MIGRATIONS[:11]:
        migration(connection)
    connection.execute(
        "INSERT INTO teams (id, canonical_name, sport) VALUES ('team-1', 'Old Team', 'football')"
    )
    connection.execute(
        """
        INSERT INTO source_team_mappings (source, sport, source_team_name, team_id)
        VALUES ('old-source', 'football', 'Old Team', 'team-1')
        """
    )
    connection.commit()

    _migration_12_mapping_resolution_audit(connection)

    team_columns = {row[1] for row in connection.execute("PRAGMA table_info(source_team_mappings)")}
    competition_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(source_competition_mappings)")
    }
    assert {"resolution_method", "confidence", "created_at"} <= team_columns
    assert {"resolution_method", "confidence", "created_at"} <= competition_columns

    # Pre-existing rows are left unbackfilled (nothing to honestly
    # recover) -- not NOT NULL, so this must not raise, and stays NULL.
    row = connection.execute(
        "SELECT * FROM source_team_mappings WHERE source_team_name = 'Old Team'"
    ).fetchone()
    assert row["resolution_method"] is None
    assert row["confidence"] is None
    assert row["created_at"] is None


def test_migration_12_is_safe_to_re_run():
    connection = make_connection()
    for migration in MIGRATIONS[:11]:
        migration(connection)

    _migration_12_mapping_resolution_audit(connection)
    _migration_12_mapping_resolution_audit(connection)  # must not raise

    team_columns = {row[1] for row in connection.execute("PRAGMA table_info(source_team_mappings)")}
    assert {"resolution_method", "confidence", "created_at"} <= team_columns


def _seed_run_and_snapshot(connection, run_id="run-1", snapshot_collector_run_id="run-1"):
    connection.execute(
        """
        INSERT INTO collector_runs (
            id, source, started_at, finished_at, status,
            records_received, records_accepted, records_rejected
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, "mozzart-file:mozzart", "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:04+00:00", "success", 1, 1, 0),
    )
    connection.execute(
        """
        INSERT INTO odds_snapshots (
            event_id, bookmaker_id, bookmaker_name, market_type, market_period,
            market_line, market_rules, market_specifier, outcome, odds,
            observed_at, market_phase, collector_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("event-1", "bet1", "Bet1", "three_way", "full_time",
         None, None, None, "1", "2.10",
         "2026-01-01T00:00:00+00:00", "pre_match", snapshot_collector_run_id),
    )
    connection.execute(
        """
        INSERT INTO raw_payloads (
            collector_run_id, source, payload, accepted, received_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, "Mozzart", "{}", 1, "2026-01-01T00:00:00+00:00"),
    )
    connection.commit()


def test_migration_13_preserves_existing_odds_snapshots_and_raw_payloads():
    connection = make_connection()
    for migration in MIGRATIONS[:12]:
        migration(connection)
    _seed_run_and_snapshot(connection)

    _migration_13_odds_snapshot_and_raw_payload_foreign_keys(connection)

    snapshot = connection.execute("SELECT * FROM odds_snapshots").fetchone()
    assert snapshot["event_id"] == "event-1"
    assert snapshot["collector_run_id"] == "run-1"
    payload = connection.execute("SELECT * FROM raw_payloads").fetchone()
    assert payload["collector_run_id"] == "run-1"
    assert payload["source"] == "Mozzart"


def test_migration_13_preserves_a_null_collector_run_id_on_odds_snapshots():
    # NULL means "provenance unknown" (pre-migration-8 historical data)
    # -- a legitimate permanent state that must survive the rebuild and
    # must not itself violate the new foreign key (NULL never does).
    connection = make_connection()
    for migration in MIGRATIONS[:12]:
        migration(connection)
    connection.execute(
        """
        INSERT INTO odds_snapshots (
            event_id, bookmaker_id, bookmaker_name, market_type, market_period,
            market_line, market_rules, market_specifier, outcome, odds,
            observed_at, market_phase, collector_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("event-1", "bet1", "Bet1", "three_way", "full_time",
         None, None, None, "1", "2.10",
         "2026-01-01T00:00:00+00:00", "pre_match", None),
    )
    connection.commit()

    _migration_13_odds_snapshot_and_raw_payload_foreign_keys(connection)

    snapshot = connection.execute("SELECT * FROM odds_snapshots").fetchone()
    assert snapshot["collector_run_id"] is None


def test_migration_13_enforces_the_new_foreign_key_on_odds_snapshots():
    connection = make_connection()
    for migration in MIGRATIONS[:12]:
        migration(connection)
    _migration_13_odds_snapshot_and_raw_payload_foreign_keys(connection)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO odds_snapshots (
                event_id, bookmaker_id, bookmaker_name, market_type, market_period,
                market_line, market_rules, market_specifier, outcome, odds,
                observed_at, market_phase, collector_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("event-1", "bet1", "Bet1", "three_way", "full_time",
             None, None, None, "1", "2.10",
             "2026-01-01T00:00:00+00:00", "pre_match", "does-not-exist"),
        )


def test_migration_13_enforces_the_new_foreign_key_on_raw_payloads():
    connection = make_connection()
    for migration in MIGRATIONS[:12]:
        migration(connection)
    _migration_13_odds_snapshot_and_raw_payload_foreign_keys(connection)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO raw_payloads (
                collector_run_id, source, payload, accepted, received_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("does-not-exist", "Mozzart", "{}", 1, "2026-01-01T00:00:00+00:00"),
        )


def test_migration_13_preserves_all_odds_snapshots_indexes():
    connection = make_connection()
    for migration in MIGRATIONS[:12]:
        migration(connection)
    _migration_13_odds_snapshot_and_raw_payload_foreign_keys(connection)

    index_names = {row[1] for row in connection.execute("PRAGMA index_list(odds_snapshots)")}
    assert {"idx_odds_event", "idx_odds_event_time", "idx_odds_event_market"} <= index_names
    unique_names = {
        row[1] for row in connection.execute("PRAGMA index_list(odds_snapshots)") if row[2]
    }
    assert "uq_odds_snapshot_dedupe" in unique_names

    raw_payload_index_names = {
        row[1] for row in connection.execute("PRAGMA index_list(raw_payloads)")
    }
    assert "idx_raw_payloads_run" in raw_payload_index_names


def test_migration_13_still_enforces_the_dedupe_unique_index():
    connection = make_connection()
    for migration in MIGRATIONS[:12]:
        migration(connection)
    _seed_run_and_snapshot(connection)
    _migration_13_odds_snapshot_and_raw_payload_foreign_keys(connection)

    # Same identity as the row _seed_run_and_snapshot already inserted --
    # ON CONFLICT DO NOTHING (odds_repository._INSERT_SQL), not tested
    # here directly, but the underlying unique index it targets must
    # still exist and still fire after the rebuild.
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO odds_snapshots (
                event_id, bookmaker_id, bookmaker_name, market_type, market_period,
                market_line, market_rules, market_specifier, outcome, odds,
                observed_at, market_phase, collector_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("event-1", "bet1", "Bet1", "three_way", "full_time",
             None, None, None, "1", "9.99",
             "2026-01-01T00:00:00+00:00", "pre_match", "run-1"),
        )


def test_migration_13_is_safe_to_re_run():
    connection = make_connection()
    for migration in MIGRATIONS[:12]:
        migration(connection)
    _seed_run_and_snapshot(connection)

    _migration_13_odds_snapshot_and_raw_payload_foreign_keys(connection)
    _migration_13_odds_snapshot_and_raw_payload_foreign_keys(connection)  # must not raise

    assert connection.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0] == 1


def test_migration_13_recovers_from_an_interruption_after_copy_before_drop():
    connection = make_connection()
    for migration in MIGRATIONS[:12]:
        migration(connection)
    _seed_run_and_snapshot(connection)

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
    connection.execute("INSERT INTO odds_snapshots_new SELECT * FROM odds_snapshots")
    connection.commit()

    _migration_13_odds_snapshot_and_raw_payload_foreign_keys(connection)

    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "odds_snapshots_new" not in tables
    assert "odds_snapshots" in tables
    rows = connection.execute("SELECT * FROM odds_snapshots").fetchall()
    assert len(rows) == 1
    assert rows[0]["event_id"] == "event-1"


def test_migration_14_adds_signal_history_table():
    connection = make_connection()
    for migration in MIGRATIONS[:13]:
        migration(connection)

    _migration_14_signal_history(connection)

    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "signal_history" in tables

    connection.execute(
        """
        INSERT INTO signals (
            id, signal_type, event_id, market_type, market_period,
            market_phase, outcome, status, edge_percent, details,
            first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "signal-1", "SUREBET", "event-1", "three_way", "full_time",
            "pre_match", None, "ACTIVE", "10.0", "{}",
            "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
        ),
    )
    connection.execute(
        """
        INSERT INTO signal_history
            (signal_id, event_type, status, edge_percent, details, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("signal-1", "created", "ACTIVE", "10.0", "{}", "2026-01-01T00:00:00+00:00"),
    )
    connection.commit()

    row = connection.execute("SELECT * FROM signal_history WHERE signal_id = 'signal-1'").fetchone()
    assert row["event_type"] == "created"


def test_migration_14_is_safe_to_re_run():
    connection = make_connection()
    for migration in MIGRATIONS[:13]:
        migration(connection)

    _migration_14_signal_history(connection)
    _migration_14_signal_history(connection)  # must not raise

    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "signal_history" in tables


def test_migration_15_backfills_peak_edge_percent_from_edge_percent():
    connection = make_connection()
    for migration in MIGRATIONS[:14]:
        migration(connection)
    connection.execute(
        """
        INSERT INTO signals (
            id, signal_type, event_id, market_type, market_period,
            market_phase, outcome, status, edge_percent, details,
            first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "signal-1", "SUREBET", "event-1", "three_way", "full_time",
            "pre_match", None, "ACTIVE", "12.5", "{}",
            "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
        ),
    )
    connection.commit()

    _migration_15_signal_peak_edge_percent(connection)

    row = connection.execute("SELECT * FROM signals WHERE id = 'signal-1'").fetchone()
    assert row["peak_edge_percent"] == "12.5"


def test_migration_15_is_safe_to_re_run():
    connection = make_connection()
    for migration in MIGRATIONS[:14]:
        migration(connection)

    _migration_15_signal_peak_edge_percent(connection)
    _migration_15_signal_peak_edge_percent(connection)  # must not raise

    columns = {row[1] for row in connection.execute("PRAGMA table_info(signals)")}
    assert "peak_edge_percent" in columns


def test_migration_16_adds_retention_cleanup_indexes():
    connection = make_connection()
    for migration in MIGRATIONS[:15]:
        migration(connection)

    _migration_16_retention_cleanup_indexes(connection)

    for table, index in [
        ("odds_snapshots", "idx_odds_snapshots_observed_at"),
        ("raw_payloads", "idx_raw_payloads_received_at"),
        ("movements", "idx_movements_detected_at"),
        ("signal_history", "idx_signal_history_recorded_at"),
        ("collector_runs", "idx_collector_runs_finished_at"),
    ]:
        index_names = {row[1] for row in connection.execute(f"PRAGMA index_list({table})")}
        assert index in index_names, f"{index} missing on {table}"


def test_migration_16_is_safe_to_re_run():
    connection = make_connection()
    for migration in MIGRATIONS[:15]:
        migration(connection)

    _migration_16_retention_cleanup_indexes(connection)
    _migration_16_retention_cleanup_indexes(connection)  # must not raise

    index_names = {row[1] for row in connection.execute("PRAGMA index_list(odds_snapshots)")}
    assert "idx_odds_snapshots_observed_at" in index_names


def test_migration_17_backfills_quote_time_from_source_timestamp_or_observed_at():
    connection = make_connection()
    for migration in MIGRATIONS[:16]:
        migration(connection)
    connection.execute(
        """
        INSERT INTO odds_snapshots (
            event_id, bookmaker_id, bookmaker_name, market_type, market_period,
            outcome, odds, observed_at, source_timestamp, market_phase
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "e1", "bet1", "Bet1", "three_way", "full_time", "1", "2.10",
            "2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00", "pre_match",
        ),
    )
    connection.execute(
        """
        INSERT INTO odds_snapshots (
            event_id, bookmaker_id, bookmaker_name, market_type, market_period,
            outcome, odds, observed_at, market_phase
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "e2", "bet1", "Bet1", "three_way", "full_time", "1", "2.10",
            "2026-01-01T00:00:00+00:00", "pre_match",
        ),
    )
    connection.commit()

    _migration_17_odds_snapshot_quote_time(connection)

    columns = {row[1] for row in connection.execute("PRAGMA table_info(odds_snapshots)")}
    assert "quote_time" in columns

    with_source_ts = connection.execute(
        "SELECT quote_time FROM odds_snapshots WHERE event_id = 'e1'"
    ).fetchone()
    assert with_source_ts["quote_time"] == "2026-01-01T01:00:00+00:00"

    without_source_ts = connection.execute(
        "SELECT quote_time FROM odds_snapshots WHERE event_id = 'e2'"
    ).fetchone()
    assert without_source_ts["quote_time"] == "2026-01-01T00:00:00+00:00"


def test_migration_17_adds_the_covering_index():
    connection = make_connection()
    for migration in MIGRATIONS[:16]:
        migration(connection)

    _migration_17_odds_snapshot_quote_time(connection)

    index_names = {row[1] for row in connection.execute("PRAGMA index_list(odds_snapshots)")}
    assert "idx_odds_latest" in index_names


def test_migration_17_is_safe_to_re_run():
    connection = make_connection()
    for migration in MIGRATIONS[:16]:
        migration(connection)
    connection.execute(
        """
        INSERT INTO odds_snapshots (
            event_id, bookmaker_id, bookmaker_name, market_type, market_period,
            outcome, odds, observed_at, market_phase
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "e1", "bet1", "Bet1", "three_way", "full_time", "1", "2.10",
            "2026-01-01T00:00:00+00:00", "pre_match",
        ),
    )
    connection.commit()

    _migration_17_odds_snapshot_quote_time(connection)
    _migration_17_odds_snapshot_quote_time(connection)  # must not raise

    row = connection.execute(
        "SELECT quote_time FROM odds_snapshots WHERE event_id = 'e1'"
    ).fetchone()
    assert row["quote_time"] == "2026-01-01T00:00:00+00:00"


def test_migration_19_preserves_rows_and_adds_reference_and_trust_metadata():
    connection = make_connection()
    for migration in MIGRATIONS[:18]:
        migration(connection)
    connection.execute(
        "INSERT INTO teams (id, canonical_name, sport) VALUES ('team-1', 'Sabah', 'football')"
    )
    connection.execute(
        """
        INSERT INTO competitions (id, canonical_name, sport)
        VALUES ('competition-1', 'Premyer Liqa', 'football')
        """
    )
    connection.execute(
        """
        INSERT INTO events (
            id, sport, league, competition_id, home_team_id, away_team_id, start_time
        ) VALUES (
            'event-1', 'football', 'Premyer Liqa', 'competition-1',
            'team-1', 'team-1', '2026-01-01T00:00:00+00:00'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO source_team_mappings (
            source, sport, source_team_name, team_id,
            resolution_method, confidence, created_at
        ) VALUES ('mozzart', 'football', 'Sabah Masazir', 'team-1',
                  'fuzzy', 88.0, '2026-01-01T00:00:00+00:00')
        """
    )
    connection.execute(
        """
        INSERT INTO source_team_id_mappings (
            source, source_team_id, team_id, last_seen_raw_name, created_at
        ) VALUES ('api-football', '100', 'team-1', 'Sabah',
                  '2026-01-01T00:00:00+00:00')
        """
    )
    connection.execute(
        """
        INSERT INTO source_competition_id_mappings (
            source, source_competition_id, competition_id, last_seen_raw_name, created_at
        ) VALUES ('api-football', '200', 'competition-1', 'Premyer Liqa',
                  '2026-01-01T00:00:00+00:00')
        """
    )
    connection.execute(
        """
        INSERT INTO source_event_mappings (source, source_event_id, event_id)
        VALUES ('api-football', '300', 'event-1')
        """
    )
    connection.commit()

    _migration_19_reference_identity_and_mapping_trust(connection)

    team = connection.execute("SELECT * FROM teams WHERE id = 'team-1'").fetchone()
    assert team["identity_status"] == "REFERENCE"
    assert team["reference_provider"] == "api-football"
    assert team["reference_provider_id"] == "100"

    fuzzy = connection.execute(
        """
        SELECT trust_state, resolver_version FROM source_team_mappings
        WHERE source = 'mozzart' AND source_team_name = 'Sabah Masazir'
        """
    ).fetchone()
    assert fuzzy["trust_state"] == "UNVERIFIED"
    assert fuzzy["resolver_version"] == 1

    id_mapping = connection.execute(
        """
        SELECT sport, trust_state, last_verified_raw_name
        FROM source_team_id_mappings
        WHERE source = 'api-football' AND source_team_id = '100'
        """
    ).fetchone()
    assert id_mapping["sport"] == "football"
    assert id_mapping["trust_state"] == "VERIFIED"
    assert id_mapping["last_verified_raw_name"] == "Sabah"

    event_mapping = connection.execute(
        """
        SELECT trust_state, resolver_version FROM source_event_mappings
        WHERE source_event_id = '300'
        """
    ).fetchone()
    assert event_mapping["trust_state"] == "UNVERIFIED"
    assert event_mapping["resolver_version"] == 1


def test_migration_19_is_safe_to_re_run():
    connection = make_connection()
    for migration in MIGRATIONS[:18]:
        migration(connection)

    _migration_19_reference_identity_and_mapping_trust(connection)
    _migration_19_reference_identity_and_mapping_trust(connection)

    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(source_team_id_mappings)")
    }
    assert {"sport", "trust_state", "resolver_version", "last_verified_raw_name"} <= columns
