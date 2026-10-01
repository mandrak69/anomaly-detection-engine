#!/usr/bin/env python3
"""Read-only per-source collector health and data-freshness report.

Reports, for every distinct collector_runs.source:

- the most recent run (timestamp, status, error if any);
- the most recent run that did not fail (SUCCESS or PARTIAL);
- the most recent run that actually produced fresh data -- a non-empty
  source_payload, not merely "ran without error". A manual-capture source
  (mozzart/meridianbet file drops) reports SUCCESS on every cycle even
  when no new HAR import has happened in days; this is the one metric
  that would otherwise hide that;
- consecutive failures (the current trailing streak, from the most
  recent run backward);
- run/failure counts over a recent window (--window-hours).

Never writes. Opens the database with SQLite mode=ro and query_only.

Usage:
    python scripts/provider_health.py
    python scripts/provider_health.py --stale-after-hours 6
    python scripts/provider_health.py --json
    python scripts/provider_health.py --fail-on-stale --fail-on-failures
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from anomaly_detection_engine.config import load_config, load_dotenv
from anomaly_detection_engine.provider_health import ProviderHealth, provider_health_report


def render_text(
    reports: list[ProviderHealth], *, now: datetime, stale_after_seconds: float
) -> str:
    if not reports:
        return "No collector runs recorded."

    lines = [f"Provider health ({len(reports)} source(s)):"]
    for report in reports:
        staleness = report.staleness_seconds(now=now)
        freshness_label = (
            "never produced fresh data"
            if staleness is None
            else f"{staleness / 3600:.1f}h ago"
        )
        is_stale = report.is_stale(now=now, stale_after_seconds=stale_after_seconds)
        flag = "STALE" if is_stale else "ok"
        lines.append(f"\n{report.source}")
        lines.append(
            f"  last run:        {report.last_run_at.isoformat()} ({report.last_run_status})"
        )
        lines.append(
            "  last success:    "
            + (report.last_success_at.isoformat() if report.last_success_at else "never")
        )
        lines.append(f"  last fresh data: {freshness_label} [{flag}]")
        lines.append(
            f"  failures:        {report.consecutive_failures} consecutive, "
            f"{report.failures_in_window}/{report.runs_in_window} in last "
            f"{report.window_hours:.0f}h"
        )
        if report.last_error_type:
            lines.append(
                f"  last error:      {report.last_error_type}: {report.last_error_message}"
            )
    return "\n".join(lines)


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"database does not exist: {path}")
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument(
        "--window-hours", type=float, default=24.0,
        help="Window for run/failure counts (default: 24)",
    )
    parser.add_argument(
        "--stale-after-hours", type=float, default=24.0,
        help="A source with no fresh capture within this many hours is STALE (default: 24)",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--fail-on-stale", action="store_true",
        help="Exit with status 2 if any source is STALE",
    )
    parser.add_argument(
        "--fail-on-failures", action="store_true",
        help="Exit with status 2 if any source's most recent run FAILED",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    db_path = args.db_path or Path(load_config().db_path)
    try:
        connection = _open_read_only(db_path)
    except ValueError as error:
        parser.error(str(error))

    now = datetime.now(UTC)
    try:
        reports = provider_health_report(connection, window_hours=args.window_hours, now=now)
    finally:
        connection.close()

    stale_after_seconds = args.stale_after_hours * 3600

    if args.json:
        print(
            json.dumps(
                [
                    report.to_evidence(now=now, stale_after_seconds=stale_after_seconds)
                    for report in reports
                ],
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(render_text(reports, now=now, stale_after_seconds=stale_after_seconds))

    any_stale = any(
        report.is_stale(now=now, stale_after_seconds=stale_after_seconds) for report in reports
    )
    any_failed = any(report.last_run_status == "failed" for report in reports)
    if (args.fail_on_stale and any_stale) or (args.fail_on_failures and any_failed):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
