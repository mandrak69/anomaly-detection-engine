from sqlite3 import Connection

from anomaly_detection_engine.models.odds import OddsSnapshot


class OddsRepository:
    def __init__(self, connection: Connection):
        self._connection = connection

     def __init__(self, connection: Connection):
        self._connection = connection

    def save(self, snapshot: OddsSnapshot) -> None:
        self._connection.execute(
            """
            INSERT INTO odds_snapshots (
                event_id,
                bookmaker_id,
                bookmaker_name,
                market,
                outcome,
                odds,
                observed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.event_id,
                snapshot.bookmaker.id,
                snapshot.bookmaker.name,
                snapshot.market,
                snapshot.outcome,
                snapshot.odds,
                snapshot.observed_at.isoformat(),
            ),
        )
        self._connection.commit()

    def find_by_event(self, event_id: str) -> list[OddsSnapshot]:
        rows = self._connection.execute(
            """
            SELECT
                event_id,
                bookmaker_id,
                bookmaker_name,
                market,
                outcome,
                odds,
                observed_at
            FROM odds_snapshots
            WHERE event_id = ?
            ORDER BY observed_at ASC
            """,
            (event_id,),
        ).fetchall()

        return [self._map_row(row) for row in rows]

    def find_latest(
        self,
        *,
        event_id: str,
        bookmaker_id: str,
        market: str,
        outcome: str,
    ) -> OddsSnapshot | None:
        row = self._connection.execute(
            """
            SELECT
                event_id,
                bookmaker_id,
                bookmaker_name,
                market,
                outcome,
                odds,
                observed_at
            FROM odds_snapshots
            WHERE event_id = ?
              AND bookmaker_id = ?
              AND market = ?
              AND outcome = ?
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (
                event_id,
                bookmaker_id,
                market,
                outcome,
            ),
        ).fetchone()

        return self._map_row(row) if row else None

    @staticmethod
    def _map_row(row) -> OddsSnapshot:
        return OddsSnapshot(
            event_id=row["event_id"],
            bookmaker=Bookmaker(
                id=row["bookmaker_id"],
                name=row["bookmaker_name"],
            ),
            market=row["market"],
            outcome=row["outcome"],
            odds=row["odds"],
            observed_at=datetime.fromisoformat(row["observed_at"]),
            source_timestamp=(
                datetime.fromisoformat(row["source_timestamp"])
                if row["source_timestamp"]
                else None
            ),
        )

    def save(self, snapshot: OddsSnapshot) -> None:
    self._connection.execute(
        """
        INSERT INTO odds_snapshots (
            event_id,
            bookmaker_id,
            bookmaker_name,
            market,
            outcome,
            odds,
            observed_at,
            source_timestamp
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.event_id,
            snapshot.bookmaker.id,
            snapshot.bookmaker.name,
            snapshot.market,
            snapshot.outcome,
            snapshot.odds,
            snapshot.observed_at.isoformat(),
            (
                snapshot.source_timestamp.isoformat()
                if snapshot.source_timestamp
                else None
            ),
        ),
    )

    self._connection.commit()

    def find_last_two(
        self,
        *,
        event_id: str,
        bookmaker_id: str,
        market: str,
        outcome: str,
    ) -> list[OddsSnapshot]:
        rows = self._connection.execute(
            """
            SELECT
                event_id,
                bookmaker_id,
                bookmaker_name,
                market,
                outcome,
                odds,
                observed_at
            FROM odds_snapshots
            WHERE event_id = ?
            AND bookmaker_id = ?
            AND market = ?
            AND outcome = ?
            ORDER BY observed_at DESC
            LIMIT 2
            """,
            (
                event_id,
                bookmaker_id,
                market,
                outcome,
            ),
        ).fetchall()

        snapshots = [self._map_row(row) for row in rows]

        return list(reversed(snapshots))
    
    def find_latest_for_market(
    self,
    *,
    event_id: str,
    market: str,
    ) -> list[OddsSnapshot]:
        rows = self._connection.execute(
            """
            SELECT o.*
            FROM odds_snapshots o
            JOIN (
                SELECT
                    event_id,
                    bookmaker_id,
                    market,
                    outcome,
                    MAX(observed_at) AS max_observed_at
                FROM odds_snapshots
                WHERE event_id = ?
                AND market = ?
                GROUP BY
                    event_id,
                    bookmaker_id,
                    market,
                    outcome
            ) latest
                ON o.event_id = latest.event_id
            AND o.bookmaker_id = latest.bookmaker_id
            AND o.market = latest.market
            AND o.outcome = latest.outcome
            AND o.observed_at = latest.max_observed_at
            ORDER BY
                o.bookmaker_id,
                o.outcome
            """,
            (
                event_id,
                market,
            ),
        ).fetchall()

        return [self._map_row(row) for row in rows]