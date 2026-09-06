from datetime import datetime, timezone
from decimal import Decimal
from sqlite3 import Connection, Row

from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot

_INSERT_SQL = """
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
"""


def _to_utc_iso(value: datetime) -> str:
    """Normalizes any timezone-aware datetime to UTC before storing it as
    text. Without this, two snapshots saved with different but equally
    valid offsets (+00:00 vs +02:00) sort incorrectly against each other
    under plain lexicographic ORDER BY -- internal storage is always UTC,
    regardless of what offset a given source reported in."""
    return value.astimezone(timezone.utc).isoformat()


def _snapshot_params(snapshot: OddsSnapshot) -> tuple:
    return (
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
        _to_utc_iso(snapshot.observed_at),
        _to_utc_iso(snapshot.source_timestamp) if snapshot.source_timestamp else None,
    )


def _market_identity_where(alias: str | None = None) -> str:
    """Builds a WHERE fragment matching every part of MarketIdentity --
    type, period, line, rules, specifier. Two markets that only differ in
    rules/specifier are not the same market (see models.market), so
    filtering on type/period/line alone (the old behavior) could silently
    mix snapshots from different markets together.
    """
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}market_type = ? "
        f"AND {prefix}market_period = ? "
        f"AND COALESCE({prefix}market_line, '') = COALESCE(?, '') "
        f"AND COALESCE({prefix}market_rules, '') = COALESCE(?, '') "
        f"AND COALESCE({prefix}market_specifier, '') = COALESCE(?, '')"
    )


def _market_identity_params(market: MarketIdentity) -> tuple:
    return (
        market.market_type.value,
        market.period.value,
        str(market.line) if market.line is not None else None,
        market.rules,
        market.specifier,
    )


class OddsRepository:
    def __init__(self, connection: Connection):
        self._connection = connection

    def save(self, snapshot: OddsSnapshot) -> None:
        self._connection.execute(_INSERT_SQL, _snapshot_params(snapshot))
        self._connection.commit()

    def save_all(self, snapshots: list[OddsSnapshot]) -> None:
        """Persists every snapshot in one transaction, committed once at
        the end -- for the several outcomes of a single raw ingested
        record (one market, multiple outcomes), so a failure partway
        through never leaves that record's snapshot set half-written the
        way calling save() once per outcome could (see
        OddsIngestionService._ingest_one).
        """
        for snapshot in snapshots:
            self._connection.execute(_INSERT_SQL, _snapshot_params(snapshot))
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
        market: MarketIdentity,
        outcome: str,
    ) -> OddsSnapshot | None:
        row = self._connection.execute(
            f"""
            SELECT *
            FROM odds_snapshots
            WHERE event_id = ?
              AND bookmaker_id = ?
              AND {_market_identity_where()}
              AND outcome = ?
            ORDER BY observed_at DESC, id DESC
            LIMIT 1
            """,
            (
                event_id,
                bookmaker_id,
                *_market_identity_params(market),
                outcome,
            ),
        ).fetchone()

        return self._map_row(row) if row else None

    def find_last_two(
        self,
        *,
        event_id: str,
        bookmaker_id: str,
        market: MarketIdentity,
        outcome: str,
    ) -> list[OddsSnapshot]:
        rows = self._connection.execute(
            f"""
            SELECT *
            FROM odds_snapshots
            WHERE event_id = ?
              AND bookmaker_id = ?
              AND {_market_identity_where()}
              AND outcome = ?
            ORDER BY observed_at DESC, id DESC
            LIMIT 2
            """,
            (
                event_id,
                bookmaker_id,
                *_market_identity_params(market),
                outcome,
            ),
        ).fetchall()

        snapshots = [self._map_row(row) for row in rows]

        return list(reversed(snapshots))

    def find_latest_for_market(
        self,
        *,
        event_id: str,
        market: MarketIdentity,
    ) -> list[OddsSnapshot]:
        """Returns the single latest snapshot per (bookmaker, outcome) for
        the given event/market.

        Uses ROW_NUMBER() partitioned by (bookmaker_id, outcome) rather
        than joining separate MAX(observed_at)/MAX(id) subqueries: the two
        maxima are not guaranteed to come from the same row (a
        later-arriving snapshot can carry an *older* observed_at than one
        already stored), so that join could silently miss the true latest
        row -- or return none at all -- for a given (bookmaker, outcome).
        ROW_NUMBER's ORDER BY picks one row's rn=1, always a real row.
        """
        rows = self._connection.execute(
            f"""
            SELECT *
            FROM (
                SELECT
                    o.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY o.bookmaker_id, o.outcome
                        ORDER BY o.observed_at DESC, o.id DESC
                    ) AS rn
                FROM odds_snapshots o
                WHERE o.event_id = ?
                  AND {_market_identity_where("o")}
            )
            WHERE rn = 1
            ORDER BY bookmaker_id, outcome
            """,
            (
                event_id,
                *_market_identity_params(market),
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
