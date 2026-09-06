import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from sqlite3 import Connection, Row
from uuid import uuid4

from anomaly_detection_engine.analysis.opportunity_detection import (
    SUREBET,
    VALUE_GAP,
    SurebetCandidate,
    ValueGapCandidate,
)
from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType

logger = logging.getLogger(__name__)

ACTIVE = "ACTIVE"
RESOLVED = "RESOLVED"


@dataclass(frozen=True)
class SignalCandidate:
    """What SignalRepository needs to identify and describe one detected
    signal, independent of which analysis module produced it -- callers
    convert their own candidate type (SurebetCandidate, ValueGapCandidate)
    into this via from_surebet()/from_value_gap() before calling
    reconcile(). Identity for dedup/upsert purposes is
    (signal_type, event_id, market, outcome); edge_percent and details
    are current state that gets updated in place on repeated sightings,
    not part of what makes two detections "the same" signal.
    """

    signal_type: str
    event_id: str
    market: MarketIdentity
    outcome: str | None
    edge_percent: Decimal
    details: dict


@dataclass(frozen=True)
class SignalRecord:
    id: str
    signal_type: str
    event_id: str
    market: MarketIdentity
    outcome: str | None
    status: str
    edge_percent: Decimal
    details: dict
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None


def from_surebet(candidate: SurebetCandidate) -> SignalCandidate:
    return SignalCandidate(
        signal_type=SUREBET,
        event_id=candidate.event.id,
        market=candidate.market,
        outcome=None,
        edge_percent=candidate.profit_percent,
        details={
            "legs": [
                {"outcome": leg.outcome, "bookmaker": leg.bookmaker, "odds": str(leg.odds)}
                for leg in candidate.legs
            ]
        },
    )


def from_value_gap(candidate: ValueGapCandidate) -> SignalCandidate:
    return SignalCandidate(
        signal_type=VALUE_GAP,
        event_id=candidate.event.id,
        market=candidate.market,
        outcome=candidate.outcome,
        edge_percent=candidate.deviation_percent,
        details={"bookmaker": candidate.bookmaker, "odds": str(candidate.odds)},
    )


class SignalRepository:
    """Persists stateful signals (SUREBET, VALUE_GAP) -- conditions that
    can persist across multiple poll cycles, unlike a movement (see
    MovementRepository), which is a one-off point-in-time event.

    reconcile() is the whole point of this repository: called once per
    signal type per detection sweep with every candidate found *this*
    sweep, it upserts each one (new -> insert ACTIVE; still-active ->
    update last_seen_at/edge/details; previously-resolved -> reactivate)
    and marks any signal of that type that was ACTIVE before this sweep
    but isn't in the current candidates as RESOLVED. An empty candidate
    list is not a no-op -- it correctly resolves everything of that type
    that was previously active.
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def reconcile(
        self,
        signal_type: str,
        candidates: list[SignalCandidate],
        *,
        observed_at: datetime,
    ) -> None:
        seen_ids: set[str] = set()

        for candidate in candidates:
            if candidate.signal_type != signal_type:
                raise ValueError(
                    f"reconcile(signal_type={signal_type!r}, ...) received a "
                    f"candidate of type {candidate.signal_type!r} -- call "
                    f"reconcile() once per signal type with only that type's "
                    f"candidates."
                )

            existing = self._find(candidate)
            if existing is None:
                signal_id = self._insert(candidate, observed_at)
            else:
                signal_id = existing["id"]
                self._touch(signal_id, candidate, observed_at)
            seen_ids.add(signal_id)

        self._resolve_stale(signal_type, seen_ids, observed_at)

    def find_active(self, signal_type: str | None = None) -> list[SignalRecord]:
        if signal_type is None:
            rows = self._connection.execute(
                "SELECT * FROM signals WHERE status = ?", (ACTIVE,)
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM signals WHERE status = ? AND signal_type = ?",
                (ACTIVE, signal_type),
            ).fetchall()
        return [self._map_row(row) for row in rows]

    def _find(self, candidate: SignalCandidate) -> Row | None:
        return self._connection.execute(
            """
            SELECT * FROM signals
            WHERE signal_type = ?
              AND event_id = ?
              AND market_type = ?
              AND market_period = ?
              AND COALESCE(market_line, '') = COALESCE(?, '')
              AND COALESCE(outcome, '') = COALESCE(?, '')
            """,
            (
                candidate.signal_type,
                candidate.event_id,
                candidate.market.market_type.value,
                candidate.market.period.value,
                _line_str(candidate.market),
                candidate.outcome,
            ),
        ).fetchone()

    def _insert(self, candidate: SignalCandidate, observed_at: datetime) -> str:
        signal_id = f"signal-{uuid4().hex[:10]}"
        self._connection.execute(
            """
            INSERT INTO signals (
                id, signal_type, event_id, market_type, market_period,
                market_line, outcome, status, edge_percent, details,
                first_seen_at, last_seen_at, resolved_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                signal_id,
                candidate.signal_type,
                candidate.event_id,
                candidate.market.market_type.value,
                candidate.market.period.value,
                _line_str(candidate.market),
                candidate.outcome,
                ACTIVE,
                str(candidate.edge_percent),
                json.dumps(candidate.details),
                observed_at.isoformat(),
                observed_at.isoformat(),
            ),
        )
        self._connection.commit()
        logger.info(
            "signal.created",
            extra={
                "signal_id": signal_id,
                "signal_type": candidate.signal_type,
                "event_id": candidate.event_id,
                "edge_percent": str(candidate.edge_percent),
            },
        )
        return signal_id

    def _touch(self, signal_id: str, candidate: SignalCandidate, observed_at: datetime) -> None:
        self._connection.execute(
            """
            UPDATE signals
            SET status = ?, edge_percent = ?, details = ?, last_seen_at = ?, resolved_at = NULL
            WHERE id = ?
            """,
            (ACTIVE, str(candidate.edge_percent), json.dumps(candidate.details), observed_at.isoformat(), signal_id),
        )
        self._connection.commit()

    def _resolve_stale(self, signal_type: str, seen_ids: set[str], observed_at: datetime) -> None:
        active_rows = self._connection.execute(
            "SELECT id FROM signals WHERE signal_type = ? AND status = ?",
            (signal_type, ACTIVE),
        ).fetchall()

        stale_ids = [row["id"] for row in active_rows if row["id"] not in seen_ids]
        for stale_id in stale_ids:
            self._connection.execute(
                "UPDATE signals SET status = ?, resolved_at = ? WHERE id = ?",
                (RESOLVED, observed_at.isoformat(), stale_id),
            )

        if stale_ids:
            self._connection.commit()
            logger.info(
                "signal.resolved",
                extra={"signal_type": signal_type, "count": len(stale_ids), "ids": stale_ids},
            )

    def _map_row(self, row: Row) -> SignalRecord:
        return SignalRecord(
            id=row["id"],
            signal_type=row["signal_type"],
            event_id=row["event_id"],
            market=MarketIdentity(
                market_type=MarketType(row["market_type"]),
                period=MarketPeriod(row["market_period"]),
                line=Decimal(row["market_line"]) if row["market_line"] is not None else None,
            ),
            outcome=row["outcome"],
            status=row["status"],
            edge_percent=Decimal(row["edge_percent"]),
            details=json.loads(row["details"]),
            first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
            last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
            resolved_at=(
                datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None
            ),
        )


def _line_str(market: MarketIdentity) -> str | None:
    return str(market.line) if market.line is not None else None
