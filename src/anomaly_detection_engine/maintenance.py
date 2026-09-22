import logging
from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Connection

from anomaly_detection_engine.config import AppConfig
from anomaly_detection_engine.storage.time_utils import to_utc_iso

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetentionCleanupSummary:
    """How many rows this cleanup run removed (or, in dry-run mode,
    would remove) per table -- what a maintenance job/script reports,
    logs, or exposes as a metric. Field names match the table each
    count came from, except collector_run_source_payloads_cleared:
    that one clears a single column, not whole rows (see
    run_retention_cleanup's own docstring for why).
    """

    raw_payloads_deleted: int
    collector_run_source_payloads_cleared: int
    odds_snapshots_deleted: int
    movements_deleted: int
    signal_history_deleted: int

    @property
    def total_rows_affected(self) -> int:
        return (
            self.raw_payloads_deleted
            + self.collector_run_source_payloads_cleared
            + self.odds_snapshots_deleted
            + self.movements_deleted
            + self.signal_history_deleted
        )


def run_retention_cleanup(
    connection: Connection,
    config: AppConfig,
    *,
    now: datetime,
    dry_run: bool = False,
) -> RetentionCleanupSummary:
    """Deletes (or, with dry_run=True, only counts) rows older than each
    table's own configured retention period -- see AppConfig's own
    retention fields for the default periods and the reasoning behind
    each one differing.

    Never called automatically by the poller or any ordinary ingestion/
    detection cycle -- this is a separate, explicitly-invoked operation
    (see scripts/run_retention_cleanup.py), the same way a schema
    migration is something a human decides to run, not a side effect of
    starting the app. Deleting real historical data deserves that same
    deliberateness, not silent automatic execution on some background
    schedule nobody is watching.

    collector_runs.source_payload is the one exception to "delete whole
    rows": only that column is cleared (set to NULL) once its own run is
    old enough -- the collector_runs row itself (status, counts,
    timestamps) stays, since it's cheap and useful for operational
    history (recover_stale_running, find_latest_by_source, ...) far
    longer than the raw response body it once carried is worth keeping.

    odds_snapshots.collector_run_id and raw_payloads.collector_run_id
    are real foreign keys to collector_runs(id) (see migration 13), but
    deleting old snapshots/payloads here never touches collector_runs
    rows themselves, so no foreign-key ordering concern arises -- only
    clearing source_payload (an UPDATE, not a DELETE) ever touches that
    table, and it's independent of whichever child rows still exist.

    One transaction for every table when dry_run is False: either every
    table's cleanup lands or (on an unexpected failure partway through)
    none of it does, the same "no partially-applied cleanup" discipline
    SignalRepository.reconcile() already follows for its own multi-step
    writes.
    """
    raw_payload_cutoff = to_utc_iso(now - config.raw_payload_retention)
    source_payload_cutoff = to_utc_iso(now - config.collector_run_source_payload_retention)
    odds_snapshot_cutoff = to_utc_iso(now - config.odds_snapshot_retention)
    movement_cutoff = to_utc_iso(now - config.movement_retention)
    signal_history_cutoff = to_utc_iso(now - config.signal_history_retention)

    if dry_run:
        summary = RetentionCleanupSummary(
            raw_payloads_deleted=_count_older_than(
                connection, "raw_payloads", "received_at", raw_payload_cutoff
            ),
            collector_run_source_payloads_cleared=_count_source_payloads_to_clear(
                connection, source_payload_cutoff
            ),
            odds_snapshots_deleted=_count_older_than(
                connection, "odds_snapshots", "observed_at", odds_snapshot_cutoff
            ),
            movements_deleted=_count_older_than(
                connection, "movements", "detected_at", movement_cutoff
            ),
            signal_history_deleted=_count_older_than(
                connection, "signal_history", "recorded_at", signal_history_cutoff
            ),
        )
    else:
        with connection:
            summary = RetentionCleanupSummary(
                raw_payloads_deleted=_delete_older_than(
                    connection, "raw_payloads", "received_at", raw_payload_cutoff
                ),
                collector_run_source_payloads_cleared=_clear_source_payloads(
                    connection, source_payload_cutoff
                ),
                odds_snapshots_deleted=_delete_older_than(
                    connection, "odds_snapshots", "observed_at", odds_snapshot_cutoff
                ),
                movements_deleted=_delete_older_than(
                    connection, "movements", "detected_at", movement_cutoff
                ),
                signal_history_deleted=_delete_older_than(
                    connection, "signal_history", "recorded_at", signal_history_cutoff
                ),
            )

    logger.info(
        "maintenance.retention_cleanup.completed",
        extra={
            "dry_run": dry_run,
            "raw_payloads_deleted": summary.raw_payloads_deleted,
            "collector_run_source_payloads_cleared": summary.collector_run_source_payloads_cleared,
            "odds_snapshots_deleted": summary.odds_snapshots_deleted,
            "movements_deleted": summary.movements_deleted,
            "signal_history_deleted": summary.signal_history_deleted,
            "total_rows_affected": summary.total_rows_affected,
        },
    )

    return summary


def _count_older_than(connection: Connection, table: str, column: str, cutoff_iso: str) -> int:
    row = connection.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {column} < ?", (cutoff_iso,)
    ).fetchone()
    count: int = row[0]
    return count


def _delete_older_than(connection: Connection, table: str, column: str, cutoff_iso: str) -> int:
    cursor = connection.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff_iso,))
    return cursor.rowcount


def _count_source_payloads_to_clear(connection: Connection, cutoff_iso: str) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) FROM collector_runs
        WHERE finished_at < ? AND source_payload IS NOT NULL
        """,
        (cutoff_iso,),
    ).fetchone()
    count: int = row[0]
    return count


def _clear_source_payloads(connection: Connection, cutoff_iso: str) -> int:
    # finished_at < ? is NULL (excluded) for a still-RUNNING row, never
    # true -- a run that hasn't finished yet is never a candidate here,
    # regardless of how old started_at is.
    cursor = connection.execute(
        """
        UPDATE collector_runs SET source_payload = NULL
        WHERE finished_at < ? AND source_payload IS NOT NULL
        """,
        (cutoff_iso,),
    )
    return cursor.rowcount
