import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from enum import Enum
from sqlite3 import Connection, Row

from anomaly_detection_engine.models.raw_odds import RawEventOdds
from anomaly_detection_engine.models.raw_payload import RawPayloadRecord


class _RawEventEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Enum):
            return obj.value
        return super().default(obj)


def serialize_raw_event_odds(raw: RawEventOdds) -> str:
    """Serializes the RawEventOdds contract for storage/reprocessing.

    This is the source-independent observation itself (architecture.md's
    "Raw Payload Layer"), not the original bytes from the wire -- the
    collector boundary is where any truly source-specific payload would
    need to be captured, and no collector currently retains that.
    """
    return json.dumps(asdict(raw), cls=_RawEventEncoder)


class RawPayloadRepository:
    def __init__(self, connection: Connection):
        self._connection = connection

    def save(
        self,
        *,
        collector_run_id: str,
        source: str,
        payload: str,
        accepted: bool,
        received_at: datetime,
        rejection_reason: str | None = None,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO raw_payloads (
                collector_run_id,
                source,
                payload,
                accepted,
                rejection_reason,
                received_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                collector_run_id,
                source,
                payload,
                1 if accepted else 0,
                rejection_reason,
                received_at.isoformat(),
            ),
        )
        self._connection.commit()

    def find_by_collector_run(self, collector_run_id: str) -> list[RawPayloadRecord]:
        rows = self._connection.execute(
            """
            SELECT * FROM raw_payloads
            WHERE collector_run_id = ?
            ORDER BY id ASC
            """,
            (collector_run_id,),
        ).fetchall()

        return [self._map_row(row) for row in rows]

    @staticmethod
    def _map_row(row: Row) -> RawPayloadRecord:
        return RawPayloadRecord(
            id=row["id"],
            collector_run_id=row["collector_run_id"],
            source=row["source"],
            payload=row["payload"],
            accepted=bool(row["accepted"]),
            received_at=datetime.fromisoformat(row["received_at"]),
            rejection_reason=row["rejection_reason"],
        )
