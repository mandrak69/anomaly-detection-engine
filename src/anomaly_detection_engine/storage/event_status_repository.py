from datetime import datetime
from sqlite3 import Connection

from anomaly_detection_engine.models.market import EventLifecycle
from anomaly_detection_engine.storage.time_utils import to_utc_iso


class EventStatusRepository:
    """Tracks the latest known EventLifecycle per canonical event (see
    migration 9's event_status table). Last-write-wins, keyed only on
    event_id: if two providers ever disagreed on the same event's status
    at the same time, whichever's ingestion happens to run later in a
    cycle simply overwrites the other -- an accepted, narrow edge case
    given only one collector (ApiFootballCollector) reports status at
    all right now, not something worth a conflict-resolution policy
    until a second one does.
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def update(self, *, event_id: str, lifecycle: EventLifecycle, updated_at: datetime) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO event_status (event_id, lifecycle, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    lifecycle = excluded.lifecycle,
                    updated_at = excluded.updated_at
                """,
                (event_id, lifecycle.value, to_utc_iso(updated_at)),
            )

    def get_many(self, event_ids: list[str]) -> dict[str, EventLifecycle]:
        """Bulk lookup for run_detection filtering a whole cycle's touched
        events at once -- a dict comprehension over event_ids' own
        one-row-per-call() would be N queries for N events; this is one.
        Missing event_ids (never status-reported) simply aren't keys in
        the result, the same "absence means unknown" contract as the
        single-event case.
        """
        if not event_ids:
            return {}
        placeholders = ", ".join("?" for _ in event_ids)
        rows = self._connection.execute(
            f"SELECT event_id, lifecycle FROM event_status WHERE event_id IN ({placeholders})",
            event_ids,
        ).fetchall()
        return {row["event_id"]: EventLifecycle(row["lifecycle"]) for row in rows}
