#!/usr/bin/env python3
"""Development/admin data-sanity tooling for the persistent database --
not a report, not a dashboard: quick answers to "is the dataset actually
good" while running a real, continuous ingest (see poller.py) instead of
a one-shot demo run. Every command here reads directly off the
already-persisted tables; nothing here recomputes or reinterprets
anything the core pipeline already decided.

Usage:
    python scripts/inspect_data.py summary
    python scripts/inspect_data.py events --limit 20
    python scripts/inspect_data.py event <event_id>
    python scripts/inspect_data.py cross-provider

By default this opens whatever DB_PATH (or its default) load_config()
resolves to -- the same database app.py/poller.py write to. Pass
--db-path to point at a specific file instead (e.g. a copy taken for
offline inspection).
"""

import argparse
import sqlite3

from anomaly_detection_engine.config import load_config
from anomaly_detection_engine.storage.database import create_connection, initialize_database


def cmd_summary(connection: sqlite3.Connection) -> None:
    def count(sql: str) -> int:
        return connection.execute(sql).fetchone()[0]

    print("-- Ingestion --")
    print(f"odds_snapshots:       {count('SELECT COUNT(*) FROM odds_snapshots')}")
    print(f"events:               {count('SELECT COUNT(*) FROM events')}")
    print(f"teams:                {count('SELECT COUNT(*) FROM teams')}")
    print(f"competitions:         {count('SELECT COUNT(*) FROM competitions')}")
    print(f"canonical bookmakers: {count('SELECT COUNT(*) FROM bookmakers')}")
    print(
        "distinct markets:     "
        + str(
            count(
                """
                SELECT COUNT(*) FROM (
                    SELECT DISTINCT market_type, market_period, market_phase, market_line
                    FROM odds_snapshots
                )
                """
            )
        )
    )

    print("\n-- Collector runs (by provider) --")
    rows = connection.execute(
        """
        SELECT
            COALESCE(provider_id, '(unknown)') AS provider_id,
            COUNT(*) AS runs,
            COALESCE(SUM(records_accepted), 0) AS accepted,
            COALESCE(SUM(records_rejected), 0) AS rejected
        FROM collector_runs
        GROUP BY provider_id
        ORDER BY provider_id
        """
    ).fetchall()
    if not rows:
        print("  (no collector runs yet)")
    for row in rows:
        print(
            f"  {row['provider_id']:<20} runs={row['runs']:<5} "
            f"accepted={row['accepted']:<8} rejected={row['rejected']}"
        )

    print("\n-- Signals (by type/status) --")
    rows = connection.execute(
        """
        SELECT signal_type, status, COUNT(*) AS n
        FROM signals
        GROUP BY signal_type, status
        ORDER BY signal_type, status
        """
    ).fetchall()
    if not rows:
        print("  (no signals yet)")
    for row in rows:
        print(f"  {row['signal_type']:<10} {row['status']:<10} {row['n']}")


def cmd_events(connection: sqlite3.Connection, *, limit: int) -> None:
    rows = connection.execute(
        """
        SELECT e.id, e.league, e.start_time,
               h.canonical_name AS home, a.canonical_name AS away
        FROM events e
        JOIN teams h ON h.id = e.home_team_id
        JOIN teams a ON a.id = e.away_team_id
        ORDER BY e.start_time DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        print("(no events yet)")
        return

    for row in rows:
        print(
            f"{row['id']}  {row['start_time']}  {row['home']} vs {row['away']}  "
            f"({row['league']})"
        )


def cmd_event(connection: sqlite3.Connection, event_id: str) -> None:
    event = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if event is None:
        print(f"No event {event_id!r}")
        return

    home = connection.execute(
        "SELECT canonical_name FROM teams WHERE id = ?", (event["home_team_id"],)
    ).fetchone()
    away = connection.execute(
        "SELECT canonical_name FROM teams WHERE id = ?", (event["away_team_id"],)
    ).fetchone()
    print(
        f"{event_id}: {home['canonical_name']} vs {away['canonical_name']} "
        f"({event['league']}, start {event['start_time']})"
    )
    print()

    rows = connection.execute(
        """
        SELECT bookmaker_name, market_type, market_period, market_phase, market_line,
               outcome, odds, observed_at, source_timestamp
        FROM odds_snapshots
        WHERE event_id = ?
        ORDER BY market_type, COALESCE(market_line, ''), bookmaker_name, outcome, observed_at DESC
        """,
        (event_id,),
    ).fetchall()

    if not rows:
        print("(no odds snapshots for this event)")
        return

    for row in rows:
        line = f" line={row['market_line']}" if row["market_line"] is not None else ""
        print(
            f"  [{row['market_type']}/{row['market_phase']}{line}] "
            f"{row['bookmaker_name']:<15} {row['outcome']:<6} {row['odds']:<8} "
            f"observed_at={row['observed_at']} source_timestamp={row['source_timestamp']}"
        )


def cmd_cross_provider(connection: sqlite3.Connection) -> None:
    """Lists events whose home or away team was independently resolved
    by 2+ distinct providers (source_team_mappings.source -- still named
    `source` at the SQL level, but it means provider_id; see
    FixtureCatalog) -- the direct, existing-data proof that two
    different real-world providers reporting the same real match under
    different team-name spellings/ids actually converged on ONE
    canonical event, without needing any new schema.

    Deliberately not traced through odds_snapshots itself: that table
    has no provider_id/collector_run_id column, so there is no way to
    say "this specific snapshot came from provider X" directly -- only
    "this canonical bookmaker/team has been seen from provider X at
    some point". Team-mapping provider diversity is the closest direct,
    already-existing signal for "this event is a genuine cross-provider
    match", short of adding new provenance columns.
    """
    rows = connection.execute(
        """
        SELECT e.id, e.league, e.start_time,
               h.canonical_name AS home, a.canonical_name AS away,
               GROUP_CONCAT(DISTINCT m.source) AS providers
        FROM events e
        JOIN teams h ON h.id = e.home_team_id
        JOIN teams a ON a.id = e.away_team_id
        JOIN source_team_mappings m ON m.team_id IN (e.home_team_id, e.away_team_id)
        GROUP BY e.id
        HAVING COUNT(DISTINCT m.source) >= 2
        ORDER BY e.start_time DESC
        """
    ).fetchall()

    if not rows:
        print("No event yet resolved by 2+ distinct providers.")
        return

    for row in rows:
        print(f"{row['id']}  {row['home']} vs {row['away']}  providers={row['providers']}")


def _connect(db_path: str | None) -> sqlite3.Connection:
    resolved_path = db_path or load_config().db_path
    connection = create_connection(resolved_path)
    initialize_database(connection)
    return connection


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db-path", default=None, help="Override the database file to open")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("summary", help="Counts across every table")

    events_parser = subparsers.add_parser("events", help="List recent events")
    events_parser.add_argument("--limit", type=int, default=20)

    event_parser = subparsers.add_parser("event", help="Show one event's full odds history")
    event_parser.add_argument("event_id")

    subparsers.add_parser(
        "cross-provider", help="Events whose teams were resolved by 2+ distinct providers"
    )

    args = parser.parse_args()
    connection = _connect(args.db_path)

    if args.command == "summary":
        cmd_summary(connection)
    elif args.command == "events":
        cmd_events(connection, limit=args.limit)
    elif args.command == "event":
        cmd_event(connection, args.event_id)
    elif args.command == "cross-provider":
        cmd_cross_provider(connection)


if __name__ == "__main__":
    main()
