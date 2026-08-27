from datetime import datetime
from decimal import Decimal
from sqlite3 import Connection, Row

from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot


class OddsRepository:
    def __init__(self, connection: Connection):
        self._connection = connection

    def save(self, snapshot: OddsSnapshot) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO odds_snapshots (
                event_id,
                bookmaker_id,
                bookmaker_name,
                market_type,
                market_period,
                market_line,
                market_rules,
                market_specifier,
                outcome,
                odds,
                observed_at,
                source_timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.event_id,
                snapshot.bookmaker.id,
                snapshot.bookmaker.name,
                snapshot.market.market_type.value,
                snapshot.market.period.value,
                str(snapshot.market.line) if snapshot.market.line is not None else None,
                snapshot.market.rules,
                snapshot.market.specifier,
                snapshot.outcome,
                str(snapshot.odds),
                snapshot.observed_at.isoformat(),
                (
                    snapshot.source_timestamp.isoformat()
                    if snapshot.source_timestamp
                    else None
                ),
            ),
        )
        self._connection.commit()

    def find_by_event(self, event_id: str) -> list[OddsSnapshot]:
        rows = self._connection.execute(
            """
            SELECT *
            FROM odds_snapshots
            WHERE event_id = ?
            ORDER BY observed_at ASC, id ASC
            """,
            (event_id,),
        ).fetchall()

        return [self._map_row(row) for row in rows]

    def find_latest(
        self,
        *,
        event_id: str,
        bookmaker_id: str,
        market_type: str,
        market_period: str,
        outcome: str,
    ) -> OddsSnapshot | None:
        row = self._connection.execute(
            """
            SELECT *
            FROM odds_snapshots
            WHERE event_id = ?
              AND bookmaker_id = ?
              AND market_type = ?
              AND market_period = ?
              AND outcome = ?
            ORDER BY observed_at DESC, id DESC
            LIMIT 1
            """,
            (
                event_id,
                bookmaker_id,
                market_type,
                market_period,
                outcome,
            ),
        ).fetchone()

        return self._map_row(row) if row else None

    def find_last_two(
        self,
        *,
        event_id: str,
        bookmaker_id: str,
        market_type: str,
        market_period: str,
        outcome: str,
    ) -> list[OddsSnapshot]:
        rows = self._connection.execute(
            """
            SELECT *
            FROM odds_snapshots
            WHERE event_id = ?
              AND bookmaker_id = ?
              AND market_type = ?
              AND market_period = ?
              AND outcome = ?
            ORDER BY observed_at DESC, id DESC
            LIMIT 2
            """,
            (
                event_id,
                bookmaker_id,
                market_type,
                market_period,
                outcome,
            ),
        ).fetchall()

        snapshots = [self._map_row(row) for row in rows]

        return list(reversed(snapshots))

    def find_latest_for_market(
        self,
        *,
        event_id: str,
        market_type: str,
        market_period: str,
    ) -> list[OddsSnapshot]:
        rows = self._connection.execute(
            """
            SELECT o.*
            FROM odds_snapshots o
            JOIN (
                SELECT
                    event_id,
                    bookmaker_id,
                    market_type,
                    market_period,
                    outcome,
                    MAX(observed_at) AS max_observed_at,
                    MAX(id) AS max_id
                FROM odds_snapshots
                WHERE event_id = ?
                  AND market_type = ?
                  AND market_period = ?
                GROUP BY
                    event_id,
                    bookmaker_id,
                    market_type,
                    market_period,
                    outcome
            ) latest
                ON o.event_id = latest.event_id
                AND o.bookmaker_id = latest.bookmaker_id
                AND o.market_type = latest.market_type
                AND o.market_period = latest.market_period
                AND o.outcome = latest.outcome
                AND o.observed_at = latest.max_observed_at
                AND o.id = latest.max_id
            ORDER BY
                o.bookmaker_id,
                o.outcome
            """,
            (
                event_id,
                market_type,
                market_period,
            ),
        ).fetchall()

        return [self._map_row(row) for row in rows]

    @staticmethod
    def _map_row(row: Row) -> OddsSnapshot:
        return OddsSnapshot(
            event_id=row["event_id"],
            bookmaker=Bookmaker(
                id=row["bookmaker_id"],
                name=row["bookmaker_name"],
            ),
            market=MarketIdentity(
                market_type=MarketType(row["market_type"]),
                period=MarketPeriod(row["market_period"]),
                line=(
                    Decimal(row["market_line"])
                    if row["market_line"] is not None
                    else None
                ),
                rules=row["market_rules"],
                specifier=row["market_specifier"],
            ),
            outcome=row["outcome"],
            odds=Decimal(row["odds"]),
            observed_at=datetime.fromisoformat(row["observed_at"]),
            source_timestamp=(
                datetime.fromisoformat(row["source_timestamp"])
                if row["source_timestamp"]
                else None
            ),
        )
