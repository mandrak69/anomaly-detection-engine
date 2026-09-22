#!/usr/bin/env python3
"""Deletes (or, with --dry-run, only counts) rows older than each
table's configured retention period -- see AppConfig's own
raw_payload_retention/collector_run_source_payload_retention/
odds_snapshot_retention/movement_retention/signal_history_retention
fields (RAW_PAYLOAD_RETENTION_DAYS/... env vars) for the defaults and
the reasoning behind each one differing.

Not run automatically by the poller -- deleting real historical data
is a deliberate, explicitly-invoked operation, the same way a schema
migration is something a human decides to run. Run this by hand, or
wire it into your own scheduled task (cron, Task Scheduler, ...) if you
want it recurring.

Usage:
    python scripts/run_retention_cleanup.py --dry-run
    python scripts/run_retention_cleanup.py

By default this opens whatever DB_PATH (or its default) load_config()
resolves to -- the same database app.py/poller.py write to. Pass
--db-path to point at a specific file instead.
"""

import argparse
from datetime import UTC, datetime

from anomaly_detection_engine.config import load_config, load_dotenv
from anomaly_detection_engine.maintenance import run_retention_cleanup
from anomaly_detection_engine.storage.database import create_connection, initialize_database


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db-path", default=None, help="Override the database file to open")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be deleted/cleared without changing anything",
    )
    args = parser.parse_args()

    load_dotenv()
    config = load_config()
    db_path = args.db_path or config.db_path
    connection = create_connection(db_path)
    initialize_database(connection)

    now = datetime.now(UTC)
    summary = run_retention_cleanup(connection, config, now=now, dry_run=args.dry_run)

    label = "Would delete/clear" if args.dry_run else "Deleted/cleared"
    print(f"{label} (retention cutoffs relative to {now.isoformat()}):")
    print(f"  raw_payloads:                          {summary.raw_payloads_deleted}")
    print(
        "  collector_runs.source_payload cleared:  "
        f"{summary.collector_run_source_payloads_cleared}"
    )
    print(f"  odds_snapshots:                         {summary.odds_snapshots_deleted}")
    print(f"  movements:                              {summary.movements_deleted}")
    print(f"  signal_history:                         {summary.signal_history_deleted}")
    print(f"  total:                                  {summary.total_rows_affected}")


if __name__ == "__main__":
    main()
