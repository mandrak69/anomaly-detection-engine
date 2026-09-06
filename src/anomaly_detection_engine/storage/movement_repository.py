from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from sqlite3 import Connection, Row

from anomaly_detection_engine.analysis.movement_detection import MovementCandidate
from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType


@dataclass(frozen=True)
class MovementRecord:
    id: int
    event_id: str
    market: MarketIdentity
    outcome: str
    bookmaker_id: str
    bookmaker_name: str
    previous_odds: Decimal
    current_odds: Decimal
    change_percent: Decimal
    previous_observed_at: datetime
    current_observed_at: datetime
    detected_at: datetime


class MovementRepository:
    """Append-only store for detected movements.

    A movement is a point-in-time event (a transition that already
    happened), not an ongoing condition -- unlike SignalRepository there
    is no reconcile()/status/resolved_at here, just save(). Deduped on
    the full transition (event, bookmaker, market, outcome, both
    observed_at timestamps): re-saving the same detected movement (e.g. a
    detection sweep re-run against the same underlying data) is a no-op
    rather than a duplicate row, the same idempotency approach
    OddsRepository.save uses for snapshots.
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def save(self, candidate: MovementCandidate, *, detected_at: datetime) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO movements (
                event_id, market_type, market_period, market_line, outcome,
                bookmaker_id, bookmaker_name, previous_odds, current_odds,
                change_percent, previous_observed_at, current_observed_at, detected_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.event.id,
                candidate.market.market_type.value,
                candidate.market.period.value,
                str(candidate.market.line) if candidate.market.line is not None else None,
                candidate.outcome,
                candidate.bookmaker_id,
                candidate.bookmaker_name,
                str(candidate.previous_odds),
                str(candidate.current_odds),
                str(candidate.change_percent),
                candidate.previous_observed_at.isoformat(),
                candidate.current_observed_at.isoformat(),
                detected_at.isoformat(),
            ),
        )
        self._connection.commit()

    def find_by_event(self, event_id: str) -> list[MovementRecord]:
        rows = self._connection.execute(
            "SELECT * FROM movements WHERE event_id = ? ORDER BY detected_at ASC",
            (event_id,),
        ).fetchall()
        return [self._map_row(row) for row in rows]

    def _map_row(self, row: Row) -> MovementRecord:
        return MovementRecord(
            id=row["id"],
            event_id=row["event_id"],
            market=MarketIdentity(
                market_type=MarketType(row["market_type"]),
                period=MarketPeriod(row["market_period"]),
                line=Decimal(row["market_line"]) if row["market_line"] is not None else None,
            ),
            outcome=row["outcome"],
            bookmaker_id=row["bookmaker_id"],
            bookmaker_name=row["bookmaker_name"],
            previous_odds=Decimal(row["previous_odds"]),
            current_odds=Decimal(row["current_odds"]),
            change_percent=Decimal(row["change_percent"]),
            previous_observed_at=datetime.fromisoformat(row["previous_observed_at"]),
            current_observed_at=datetime.fromisoformat(row["current_observed_at"]),
            detected_at=datetime.fromisoformat(row["detected_at"]),
        )
