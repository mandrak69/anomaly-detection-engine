import json
import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from sqlite3 import Connection, Row
from uuid import uuid4

from anomaly_detection_engine.models.market import (
    MarketIdentity,
    MarketPeriod,
    MarketPhase,
    MarketType,
)
from anomaly_detection_engine.models.signal import SignalIdentity
from anomaly_detection_engine.storage.time_utils import to_utc_iso

logger = logging.getLogger(__name__)

ACTIVE = "ACTIVE"
RESOLVED = "RESOLVED"
EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class SignalCandidate:
    """What SignalRepository needs to identify and describe one detected
    signal, independent of which analysis module produced it -- callers
    convert their own candidate type (SurebetCandidate, ValueGapCandidate)
    into this via pipeline.from_surebet()/from_value_gap() before calling
    reconcile(). Those two adapters live in pipeline.py, not here: this
    module (storage) has no business importing analysis.
    opportunity_detection's candidate types itself, only the plain
    SignalCandidate/SignalIdentity shape it actually persists by.
    Identity for dedup/upsert purposes is (signal_type, event_id, market,
    outcome); edge_percent and details are current state that gets
    updated in place on repeated sightings, not part of what makes two
    detections "the same" signal.
    """

    signal_type: str
    event_id: str
    market: MarketIdentity
    outcome: str | None
    edge_percent: Decimal
    details: dict

    @property
    def identity(self) -> SignalIdentity:
        return SignalIdentity(event_id=self.event_id, market=self.market, outcome=self.outcome)


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
    # When status left ACTIVE -- set by both reconcile() (status=RESOLVED)
    # and expire_active_signals() (status=EXPIRED). The column name
    # reflects the first of the two states this repository ever had;
    # check status, not just whether this is set, to tell which one
    # actually happened.
    resolved_at: datetime | None


class SignalRepository:
    """Persists stateful signals (SUREBET, VALUE_GAP) -- conditions that
    can persist across multiple poll cycles, unlike a movement (see
    MovementRepository), which is a one-off point-in-time event.

    reconcile() is the whole point of this repository: called once per
    signal_type per detection sweep with every candidate found *this*
    sweep, it upserts each one (new -> insert ACTIVE; still-active ->
    update last_seen_at/edge/details; previously-resolved -> reactivate)
    and marks an ACTIVE signal as RESOLVED *if and only if* its own
    (event, market, outcome) identity is in evaluated_keys and it wasn't
    seen this sweep. An empty candidate list is not a no-op -- it
    correctly resolves everything evaluated and absent, as long as
    evaluated_keys says those identities were actually checked.

    RESOLVED and EXPIRED are both terminal, non-ACTIVE states, but mean
    different things: RESOLVED means a sweep positively evaluated this
    exact (event, market, outcome) and found the condition gone (see
    reconcile()). EXPIRED (see expire_active_signals) means detection
    stopped being able to evaluate it at all -- the event fell out of
    this project's "events touched this cycle" scope (see
    OddsIngestionService.touched_events/pipeline.run_ingestion) and its
    lifecycle has run out, not that anything about the signal itself was
    disproven. Conflating the two would claim a certainty ("the surebet
    closed") this system never actually confirmed.
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def reconcile(
        self,
        signal_type: str,
        candidates: list[SignalCandidate],
        *,
        observed_at: datetime,
        evaluated_keys: Collection[SignalIdentity],
    ) -> None:
        """evaluated_keys scopes which ACTIVE signals are even eligible
        to be resolved this sweep, at the same (event, market, outcome)
        granularity a signal's own identity is keyed on (see
        SignalIdentity) -- not just "this event", which would be too
        coarse for VALUE_GAP (one outcome can lack enough bookmakers to
        evaluate while a sibling outcome for the same event is fine), and
        not just "this market" scoped globally, which would let
        reconciling one market resolve an unrelated signal for a market
        this sweep never analyzed. An identity missing from
        evaluated_keys means "couldn't tell this sweep", which is not
        the same claim as "confirmed gone" -- required, not defaulted,
        since silently resolving too much is the failure mode this whole
        method exists to prevent.

        Every candidate's own identity must itself be a member of
        evaluated_keys (raises ValueError otherwise) -- a stronger
        version of the old "candidate belongs to the market this sweep
        is about" check, since a real candidate can only exist for
        something that was, by definition, evaluated.

        The whole reconcile is one transaction: either every candidate's
        upsert and the stale-resolution both land, or (on an unexpected
        failure partway through) none of it does -- a partially applied
        reconcile would leave some signals reflecting this sweep and
        others still reflecting the previous one.
        """
        with self._connection:
            seen_ids: set[str] = set()

            for candidate in candidates:
                if candidate.signal_type != signal_type:
                    raise ValueError(
                        f"reconcile(signal_type={signal_type!r}, ...) received a "
                        f"candidate of type {candidate.signal_type!r} -- call "
                        f"reconcile() once per signal type with only that type's "
                        f"candidates."
                    )
                if candidate.identity not in evaluated_keys:
                    raise ValueError(
                        f"reconcile() received a candidate for {candidate.identity!r} "
                        f"that is not in evaluated_keys -- every candidate must "
                        f"correspond to something this sweep actually evaluated."
                    )

                existing = self._find(candidate)
                if existing is None:
                    signal_id = self._insert(candidate, observed_at)
                else:
                    signal_id = existing["id"]
                    self._touch(signal_id, candidate, observed_at)
                seen_ids.add(signal_id)

            self._resolve_stale(signal_type, evaluated_keys, seen_ids, observed_at)

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

    def expire_active_signals(
        self, *, event_start_cutoff: datetime, expired_at: datetime
    ) -> list[str]:
        """Marks EXPIRED every ACTIVE signal whose event started before
        event_start_cutoff -- a lifecycle boundary the *caller* computes
        (see AppConfig.signal_ttl/pipeline.run_detection); this
        repository has no opinion on what "too old" means, only how to
        act once told, so a future per-sport or per-MarketPhase TTL
        policy can change how that cutoff is computed without touching
        this method's signature.

        Also requires last_seen_at < expired_at: without this, a signal
        reconcile() just reconfirmed as ACTIVE *this same* detection
        cycle would be immediately re-expired by this call running right
        after it, since event_start_cutoff alone only looks at how long
        ago the event started, not whether it is still actively being
        observed. Pass the exact same "now" to both calls in one
        run_detection() invocation (see persist_detected_signals's
        observed_at parameter) so a signal touched this cycle has
        last_seen_at == expired_at and is correctly excluded here.

        Deliberately separate from reconcile(): reconcile() reflects
        what *this sweep* positively found; this is lifecycle
        maintenance independent of any sweep's outcome, and runs even
        for signal types/events that produced no candidates at all this
        cycle (or weren't touched by it -- see class docstring).
        """
        with self._connection:
            rows = self._connection.execute(
                """
                SELECT s.id FROM signals s
                JOIN events e ON e.id = s.event_id
                WHERE s.status = ?
                  AND e.start_time < ?
                  AND s.last_seen_at < ?
                """,
                (ACTIVE, to_utc_iso(event_start_cutoff), expired_at.isoformat()),
            ).fetchall()
            expired_ids = [row["id"] for row in rows]

            for signal_id in expired_ids:
                self._connection.execute(
                    "UPDATE signals SET status = ?, resolved_at = ? WHERE id = ?",
                    (EXPIRED, expired_at.isoformat(), signal_id),
                )

        if expired_ids:
            logger.info(
                "signal.expired",
                extra={"count": len(expired_ids), "ids": expired_ids},
            )

        return expired_ids

    def _find(self, candidate: SignalCandidate) -> Row | None:
        return self._connection.execute(
            f"""
            SELECT * FROM signals
            WHERE signal_type = ?
              AND event_id = ?
              AND {_market_where()}
              AND COALESCE(outcome, '') = COALESCE(?, '')
            """,
            (
                candidate.signal_type,
                candidate.event_id,
                *_market_params(candidate.market),
                candidate.outcome,
            ),
        ).fetchone()

    def _insert(self, candidate: SignalCandidate, observed_at: datetime) -> str:
        signal_id = f"signal-{uuid4().hex[:10]}"
        self._connection.execute(
            """
            INSERT INTO signals (
                id, signal_type, event_id, market_type, market_period,
                market_phase, market_line, market_rules, market_specifier,
                outcome, status, edge_percent, details,
                first_seen_at, last_seen_at, resolved_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                signal_id,
                candidate.signal_type,
                candidate.event_id,
                candidate.market.market_type.value,
                candidate.market.period.value,
                candidate.market.phase.value,
                _line_str(candidate.market),
                candidate.market.rules,
                candidate.market.specifier,
                candidate.outcome,
                ACTIVE,
                str(candidate.edge_percent),
                json.dumps(candidate.details),
                observed_at.isoformat(),
                observed_at.isoformat(),
            ),
        )
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
            (
                ACTIVE,
                str(candidate.edge_percent),
                json.dumps(candidate.details),
                observed_at.isoformat(),
                signal_id,
            ),
        )

    def _resolve_stale(
        self,
        signal_type: str,
        evaluated_keys: Collection[SignalIdentity],
        seen_ids: set[str],
        observed_at: datetime,
    ) -> None:
        if not evaluated_keys:
            return

        active_rows = self._connection.execute(
            "SELECT * FROM signals WHERE signal_type = ? AND status = ?",
            (signal_type, ACTIVE),
        ).fetchall()

        stale_ids = [
            row["id"]
            for row in active_rows
            if row["id"] not in seen_ids and _identity_from_row(row) in evaluated_keys
        ]
        for stale_id in stale_ids:
            self._connection.execute(
                "UPDATE signals SET status = ?, resolved_at = ? WHERE id = ?",
                (RESOLVED, observed_at.isoformat(), stale_id),
            )

        if stale_ids:
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
                phase=MarketPhase(row["market_phase"]),
                line=Decimal(row["market_line"]) if row["market_line"] is not None else None,
                rules=row["market_rules"],
                specifier=row["market_specifier"],
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


def _market_where(alias: str | None = None) -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}market_type = ? "
        f"AND {prefix}market_period = ? "
        f"AND {prefix}market_phase = ? "
        f"AND COALESCE({prefix}market_line, '') = COALESCE(?, '') "
        f"AND COALESCE({prefix}market_rules, '') = COALESCE(?, '') "
        f"AND COALESCE({prefix}market_specifier, '') = COALESCE(?, '')"
    )


def _market_params(market: MarketIdentity) -> tuple:
    return (
        market.market_type.value,
        market.period.value,
        market.phase.value,
        _line_str(market),
        market.rules,
        market.specifier,
    )


def _identity_from_row(row: Row) -> SignalIdentity:
    return SignalIdentity(
        event_id=row["event_id"],
        market=MarketIdentity(
            market_type=MarketType(row["market_type"]),
            period=MarketPeriod(row["market_period"]),
            phase=MarketPhase(row["market_phase"]),
            line=Decimal(row["market_line"]) if row["market_line"] is not None else None,
            rules=row["market_rules"],
            specifier=row["market_specifier"],
        ),
        outcome=row["outcome"],
    )
