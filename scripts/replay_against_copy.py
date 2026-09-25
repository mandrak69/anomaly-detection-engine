#!/usr/bin/env python3
"""Run an ingestion/replay command against a consistent database copy.

The source database is never modified. SQLite's online-backup API creates a
consistent working copy (including committed WAL pages), the requested command
runs with DB_PATH pointing at that copy, and a JSON before/after report records
identity and row-count changes.

Examples:
    python scripts/replay_against_copy.py --source-db data/anomaly_detection.db \
        --output-db data/replay.db -- python -m anomaly_detection_engine.app

    python scripts/replay_against_copy.py --source-db prod-copy.db \
        --report replay-report.json -- python scripts/watch_capture.py --help
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from anomaly_detection_engine.storage.database import create_connection

COUNTED_TABLES = (
    "teams",
    "competitions",
    "events",
    "odds_snapshots",
    "collector_runs",
    "raw_payloads",
    "signals",
    "movements",
)

MAPPING_TABLES = (
    "source_team_mappings",
    "source_competition_mappings",
    "source_team_id_mappings",
    "source_competition_id_mappings",
    "source_event_mappings",
)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _group_counts(
    connection: sqlite3.Connection, table: str, column: str
) -> dict[str, int]:
    if not _table_exists(connection, table) or column not in _columns(connection, table):
        return {}
    return {
        str(row["value"] if row["value"] is not None else "(null)"): row["n"]
        for row in connection.execute(
            f"SELECT {column} AS value, COUNT(*) AS n FROM {table} GROUP BY {column}"
        )
    }


def database_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    """Return stable, JSON-serializable counters useful for replay review."""
    table_counts = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in COUNTED_TABLES
        if _table_exists(connection, table)
    }
    trust_states = {
        table: _group_counts(connection, table, "trust_state")
        for table in MAPPING_TABLES
        if _table_exists(connection, table)
    }
    resolution_methods = {
        table: _group_counts(connection, table, "resolution_method")
        for table in MAPPING_TABLES
        if _table_exists(connection, table)
    }
    identity_states = {
        table: _group_counts(connection, table, "identity_status")
        for table in ("teams", "competitions", "events")
        if _table_exists(connection, table)
    }

    orphan_counts: dict[str, int] = {}
    if _table_exists(connection, "teams") and _table_exists(connection, "events"):
        orphan_counts["teams"] = connection.execute(
            """
            SELECT COUNT(*) FROM teams t
            WHERE NOT EXISTS (
                SELECT 1 FROM events e
                WHERE e.home_team_id = t.id OR e.away_team_id = t.id
            )
            """
        ).fetchone()[0]
    if _table_exists(connection, "competitions") and _table_exists(connection, "events"):
        orphan_counts["competitions"] = connection.execute(
            """
            SELECT COUNT(*) FROM competitions c
            WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.competition_id = c.id)
            """
        ).fetchone()[0]

    return {
        "table_counts": table_counts,
        "trust_states": trust_states,
        "resolution_methods": resolution_methods,
        "identity_states": identity_states,
        "orphan_counts": orphan_counts,
    }


def _numeric_diff(before: Any, after: Any) -> Any:
    if isinstance(before, dict) and isinstance(after, dict):
        keys = sorted(set(before) | set(after))
        return {key: _numeric_diff(before.get(key, 0), after.get(key, 0)) for key in keys}
    if isinstance(before, int) and isinstance(after, int):
        return after - before
    return None if before == after else {"before": before, "after": after}


def copy_database(source: Path, target: Path) -> None:
    if not source.is_file():
        raise ValueError(f"source database does not exist: {source}")
    if source.resolve() == target.resolve():
        raise ValueError("--output-db must differ from --source-db")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError(f"output database already exists: {target}")

    source_connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


def run_replay(
    source_db: Path,
    output_db: Path,
    command: Sequence[str],
    *,
    report_path: Path | None = None,
) -> tuple[dict[str, Any], int]:
    copy_database(source_db, output_db)

    before_connection = create_connection(output_db)
    try:
        before = database_snapshot(before_connection)
        integrity_before = before_connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        before_connection.close()

    environment = os.environ.copy()
    environment["DB_PATH"] = str(output_db.resolve())
    completed = subprocess.run(list(command), env=environment, check=False)

    after_connection = create_connection(output_db)
    try:
        after = database_snapshot(after_connection)
        integrity_after = after_connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        after_connection.close()

    report = {
        "source_db": str(source_db.resolve()),
        "output_db": str(output_db.resolve()),
        "command": list(command),
        "command_exit_code": completed.returncode,
        "integrity_before": integrity_before,
        "integrity_after": integrity_after,
        "before": before,
        "after": after,
        "diff": _numeric_diff(before, after),
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return report, completed.returncode


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--output-db", type=Path, default=Path("data/replay.db"))
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run against the copy; put -- before it",
    )
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a replay command is required after --")

    try:
        _, exit_code = run_replay(
            args.source_db, args.output_db, command, report_path=args.report
        )
    except ValueError as error:
        parser.error(str(error))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
