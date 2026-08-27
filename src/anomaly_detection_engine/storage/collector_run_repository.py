from datetime import datetime
from sqlite3 import Connection, Row

from anomaly_detection_engine.models.collector_run import CollectorRun, CollectorRunStatus


class CollectorRunRepository:
    def __init__(self, connection: Connection):
        self._connection = connection

    def save(self, run: CollectorRun) -> None:
        self._connection.execute(
            """
            INSERT INTO collector_runs (
                id,
                source,
                started_at,
                finished_at,
                status,
                records_received,
                records_accepted,
                records_rejected,
                collector_version,
                error_type,
                error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.id,
                run.source,
                run.started_at.isoformat(),
                run.finished_at.isoformat(),
                run.status.value,
                run.records_received,
                run.records_accepted,
                run.records_rejected,
                run.collector_version,
                run.error_type,
                run.error_message,
            ),
        )
        self._connection.commit()

    def find_by_id(self, run_id: str) -> CollectorRun | None:
        row = self._connection.execute(
            "SELECT * FROM collector_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

        return self._map_row(row) if row else None

    @staticmethod
    def _map_row(row: Row) -> CollectorRun:
        return CollectorRun(
            id=row["id"],
            source=row["source"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]),
            status=CollectorRunStatus(row["status"]),
            records_received=row["records_received"],
            records_accepted=row["records_accepted"],
            records_rejected=row["records_rejected"],
            collector_version=row["collector_version"],
            error_type=row["error_type"],
            error_message=row["error_message"],
        )
