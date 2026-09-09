from datetime import datetime
from decimal import Decimal
from sqlite3 import Connection, Row

from anomaly_detection_engine.models.market import (
    MarketIdentity,
    MarketPeriod,
    MarketPhase,
    MarketType,
)
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.storage.time_utils import to_utc_iso

_INSERT_SQL = """
    INSERT OR IGNORE INTO odds_snapshots (
        event_id,
        bookmaker_id,
        bookmaker_name,
        market_type,
        market_period,
        market_phase,
        market_line,
        market_rules,
        market_specifier,
        outcome,
        odds,
        observed_at,
        source_timestamp,
        collector_run_id
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# The "latest"/"most current" ordering every selection query below
# shares: quote_time (source_timestamp if the source provided one,
# otherwise observed_at -- see OddsSnapshot.quote_time) is when the
# price actually was/is current, not when *we* happened to poll it.
# Ordering by observed_at alone (the old behavior) picks whichever row
# this process fetched most recently, which is not the same claim --
# two providers reporting the same canonical bookmaker can be polled at
# different times, so the row fetched last is not necessarily the row
# describing the freshest real price. observed_at DESC/id DESC stay as
# tiebreakers for whenever quote_time is genuinely equal (e.g. neither
# source reports a source_timestamp at all).
def _latest_order_by(alias: str | None = None) -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"COALESCE({prefix}source_timestamp, {prefix}observed_at) DESC, "
        f"{prefix}observed_at DESC, {prefix}id DESC"
    )


def _snapshot_params(snapshot: OddsSnapshot) -> tuple:
    return (
        snapshot.event_id,
        snapshot.bookmaker.id,
        snapshot.bookmaker.name,
        snapshot.market.market_type.value,
        snapshot.market.period.value,
        snapshot.market.phase.value,
        str(snapshot.market.line) if snapshot.market.line is not None else None,
        snapshot.market.rules,
        snapshot.market.specifier,
        snapshot.outcome,
        str(snapshot.odds),
        to_utc_iso(snapshot.observed_at),
        to_utc_iso(snapshot.source_timestamp) if snapshot.source_timestamp else None,
        snapshot.collector_run_id,
    )


def _market_identity_where(alias: str | None = None) -> str:
    """Builds a WHERE fragment matching every part of MarketIdentity --
    type, period, phase, line, rules, specifier. Two markets that only
    differ in phase (a pre-match price vs. a live price for the same
    event) or rules/specifier are not the same market (see
    models.market), so filtering on type/period/line alone (the old
    behavior) could silently mix snapshots from different markets
    together.
    """
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}market_type = ? "
        f"AND {prefix}market_period = ? "
        f"AND {prefix}market_phase = ? "
        f"AND COALESCE({prefix}market_line, '') = COALESCE(?, '') "
        f"AND COALESCE({prefix}market_rules, '') = COALESCE(?, '') "
        f"AND COALESCE({prefix}market_specifier, '') = COALESCE(?, '')"
    )


def _market_identity_params(market: MarketIdentity) -> tuple:
    return (
        market.market_type.value,
        market.period.value,
        market.phase.value,
        str(market.line) if market.line is not None else None,
        market.rules,
        market.specifier,
    )


class OddsRepository:
    def __init__(self, connection: Connection):
        self._connection = connection

    def save(self, snapshot: OddsSnapshot) -> None:
        with self._connection:
            self._connection.execute(_INSERT_SQL, _snapshot_params(snapshot))

    def save_all(self, snapshots: list[OddsSnapshot]) -> None:
        """Persists every snapshot in one transaction -- for the several
        outcomes of a single raw ingested record (one market, multiple
        outcomes), so a failure partway through never leaves that
        record's snapshot set half-written the way calling save() once
        per outcome could (see OddsIngestionService._ingest_one).

        `with self._connection:` (not a manual commit()) is what makes
        this atomic: it commits on success and rolls back everything
        executed so far if any iteration raises, rather than committing
        whatever happened to succeed before the failure.
        """
        with self._connection:
            for snapshot in snapshots:
                self._connection.execute(_INSERT_SQL, _snapshot_params(snapshot))

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
            ORDER BY {_latest_order_by()}
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
            ORDER BY {_latest_order_by()}
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

    def find_last_two_same_provider(
        self,
        *,
        event_id: str,
        bookmaker_id: str,
        market: MarketIdentity,
        outcome: str,
    ) -> list[OddsSnapshot]:
        """Like find_last_two, but the two returned readings (if any) are
        guaranteed to come from the same provider -- the latest reading's
        own provider (via its collector_run_id -> collector_runs.
        provider_id) is what every earlier candidate must match.

        Comparing "Bet365 via the-odds-api" against "Bet365 via
        api-football" as if they were one continuous quote stream could
        report a "movement" that is really just two providers
        disagreeing about the current price, or reporting on different
        schedules, not the same bookmaker's price actually changing --
        see analysis.movement_detection, the only caller.

        A snapshot with no known collector_run_id (pre-migration-8
        historical data, or one saved directly rather than through
        OddsIngestionService) has an *unknown* provider, not "no
        provider" -- two such unknown-provider readings are treated as a
        match, the same fallback find_last_two itself always had, so
        data/tests with no provenance information keep behaving exactly
        as before rather than being silently excluded from movement
        detection entirely.

        Looks at only the most recent 10 readings (across every
        provider combined), not the whole history -- correct as long as
        a genuine same-provider reading, if one exists, is among the
        last 10 combined readings, true for any realistic difference in
        polling cadence between providers.
        """
        rows = self._connection.execute(
            f"""
            SELECT o.*, cr.provider_id AS run_provider_id
            FROM odds_snapshots o
            LEFT JOIN collector_runs cr ON cr.id = o.collector_run_id
            WHERE o.event_id = ?
              AND o.bookmaker_id = ?
              AND {_market_identity_where("o")}
              AND o.outcome = ?
            ORDER BY {_latest_order_by("o")}
            LIMIT 10
            """,
            (
                event_id,
                bookmaker_id,
                *_market_identity_params(market),
                outcome,
            ),
        ).fetchall()

        if not rows:
            return []

        latest_row = rows[0]
        latest_provider = latest_row["run_provider_id"]

        previous_row = next(
            (row for row in rows[1:] if row["run_provider_id"] == latest_provider),
            None,
        )

        if previous_row is None:
            return [self._map_row(latest_row)]

        return [self._map_row(previous_row), self._map_row(latest_row)]

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

        "Latest" is by quote_time (see _latest_order_by), not observed_at
        alone: the same canonical bookmaker reported by two providers can
        be polled at different times, so the row *we* fetched most
        recently is not necessarily the row describing the freshest real
        price -- an older, later-polled quote could otherwise shadow a
        genuinely fresher one just because it happened to be fetched
        after it.
        """
        rows = self._connection.execute(
            f"""
            SELECT *
            FROM (
                SELECT
                    o.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY o.bookmaker_id, o.outcome
                        ORDER BY {_latest_order_by("o")}
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
                phase=MarketPhase(row["market_phase"]),
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
            collector_run_id=row["collector_run_id"],
        )
