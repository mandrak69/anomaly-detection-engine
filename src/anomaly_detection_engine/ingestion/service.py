import logging
from datetime import UTC, datetime
from uuid import uuid4

from anomaly_detection_engine.collectors.base import OddsCollector
from anomaly_detection_engine.matching.event_matcher import EventResolver
from anomaly_detection_engine.models.collector_run import CollectorRun, CollectorRunStatus
from anomaly_detection_engine.models.event import Event
from anomaly_detection_engine.models.odds import OddsSnapshot
from anomaly_detection_engine.models.raw_odds import RawEventOdds
from anomaly_detection_engine.observability.metrics import IngestionMetrics
from anomaly_detection_engine.storage.bookmaker_catalog import BookmakerCatalog
from anomaly_detection_engine.storage.collector_run_repository import CollectorRunRepository
from anomaly_detection_engine.storage.odds_repository import OddsRepository
from anomaly_detection_engine.storage.raw_payload_repository import (
    RawPayloadRepository,
    serialize_raw_event_odds,
)
from anomaly_detection_engine.validation.raw_odds_validator import validate_raw_event_odds

logger = logging.getLogger(__name__)


class OddsIngestionService:
    """Coordinates one ingestion cycle: collect -> validate -> match -> persist.

    Freshness checks and analysis are deliberately kept out of this service
    (see docs/architecture.md - ingestion orchestration stays separate from
    pure analysis logic).
    """

    def __init__(
        self,
        collector: OddsCollector,
        matcher: EventResolver,
        odds_repository: OddsRepository,
        collector_run_repository: CollectorRunRepository,
        raw_payload_repository: RawPayloadRepository,
        bookmaker_catalog: BookmakerCatalog,
        collector_version: str | None = None,
        metrics: IngestionMetrics | None = None,
    ) -> None:
        self._collector = collector
        self._matcher = matcher
        self._odds_repository = odds_repository
        self._collector_run_repository = collector_run_repository
        self._raw_payload_repository = raw_payload_repository
        self._bookmaker_catalog = bookmaker_catalog
        self._collector_version = collector_version
        self._metrics = metrics
        self._touched_events: dict[str, Event] = {}

    @property
    def touched_events(self) -> list[Event]:
        """Every event this run actually resolved a raw record against
        -- not every event the matcher/FixtureCatalog has ever seen.

        Deliberately narrower than "every event in the catalog": a
        persistent FixtureCatalog remembers every event it has ever
        created, including matches that finished months ago and will
        never be polled again, and nothing about this project's data
        sources says when a match is over. Scoping the caller's working
        set to "events actually touched this cycle" instead means a
        long-finished match simply stops being re-evaluated once
        collectors stop reporting anything for it, rather than being
        evaluated forever and permanently flagged stale.
        """
        return list(self._touched_events.values())

    def run(self) -> CollectorRun:
        run_id = str(uuid4())
        started_at = datetime.now(UTC)
        source = self._collector.source

        logger.info("ingestion.run.started", extra={"run_id": run_id, "source": source})

        try:
            collection = self._collector.collect()
        except Exception as exc:
            logger.error(
                "ingestion.collector.failed",
                extra={
                    "run_id": run_id,
                    "source": source,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            return self._record_run(
                run_id=run_id,
                started_at=started_at,
                status=CollectorRunStatus.FAILED,
                records_received=0,
                records_accepted=0,
                records_rejected=0,
                rejection_reasons=[],
                error_type=type(exc).__name__,
                error_message=str(exc),
                # No CollectionResult -- collect() itself raised, so
                # there is no source_payload to keep (it was never even
                # fetched/read), but provider_id/parser_version are
                # static collector properties, known regardless.
                source_payload=None,
            )

        raw_events = collection.records
        records_received = 0
        records_accepted = 0
        records_rejected = 0
        audit_failures = 0
        rejection_reasons: list[str] = []
        unexpected_error: Exception | None = None

        try:
            for raw in raw_events:
                records_received += 1
                accepted, reason = self._ingest_one(raw)

                try:
                    self._save_raw_payload(
                        run_id=run_id,
                        raw=raw,
                        accepted=accepted,
                        reason=reason,
                        received_at=started_at,
                    )
                except Exception as exc:
                    # Audit-trail write failing must not also lose the
                    # CollectorRun itself -- log and keep going rather
                    # than letting this exception escape the loop. Still
                    # counted (audit_failures below): raw-payload
                    # retention is part of this system's auditability,
                    # not a nicety a run can silently lose and still
                    # report SUCCESS.
                    audit_failures += 1
                    logger.error(
                        "ingestion.raw_payload.save_failed",
                        extra={
                            "run_id": run_id,
                            "source": raw.source,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                        },
                    )

                if accepted:
                    records_accepted += 1
                else:
                    records_rejected += 1
                    rejection_reasons.append(reason)
                    logger.warning(
                        "ingestion.record.rejected",
                        extra={
                            "run_id": run_id,
                            "source": raw.source,
                            "home_team": raw.home_team,
                            "away_team": raw.away_team,
                            "reason": reason,
                        },
                    )
        except Exception as exc:
            # Should be unreachable given _ingest_one's own exception
            # handling below, but the run's final state (however partial)
            # must never depend on that holding true forever -- losing the
            # CollectorRun record here would hide exactly the failure
            # observability most needs to see. Deliberately catches
            # Exception, not BaseException: a real KeyboardInterrupt/
            # SystemExit should still propagate, not be swallowed here.
            unexpected_error = exc
            logger.error(
                "ingestion.run.aborted_unexpectedly",
                extra={"run_id": run_id, "source": source},
                exc_info=True,
            )

        if unexpected_error is not None:
            # An aborted run is never SUCCESS, even if every record
            # processed before the abort happened to succeed -- the loop
            # not finishing is itself the failure being reported here.
            status = (
                CollectorRunStatus.PARTIAL
                if records_accepted > 0
                else CollectorRunStatus.FAILED
            )
        elif records_accepted == 0 and records_rejected > 0:
            status = CollectorRunStatus.FAILED
        elif records_rejected > 0 or audit_failures > 0:
            status = CollectorRunStatus.PARTIAL
        else:
            status = CollectorRunStatus.SUCCESS

        return self._record_run(
            run_id=run_id,
            started_at=started_at,
            status=status,
            records_received=records_received,
            records_accepted=records_accepted,
            records_rejected=records_rejected,
            rejection_reasons=rejection_reasons,
            error_type=type(unexpected_error).__name__ if unexpected_error else None,
            error_message=str(unexpected_error) if unexpected_error else None,
            source_payload=collection.source_payload,
        )

    def _ingest_one(self, raw: RawEventOdds) -> tuple[bool, str | None]:
        """Validates, matches, and persists one raw record.

        Catches everything (validator, matcher/FixtureCatalog, or
        OddsRepository can all raise for reasons that have nothing to do
        with this particular record being invalid, e.g. a transient
        matcher/DB error) and reports it as a rejected record with a
        distinguishable reason, rather than letting it escape and abort
        the whole run -- see run()'s docstring/comments for why the run
        must always reach a final state.
        """
        try:
            validation = validate_raw_event_odds(raw)
            if not validation.valid:
                codes = ", ".join(error.code for error in validation.errors)
                return False, f"{validation.stage.value}: {codes}"

            match = self._matcher.match(
                sport=raw.sport,
                league=raw.league,
                home_team_raw=raw.home_team,
                away_team_raw=raw.away_team,
                start_time=raw.start_time,
            )
            if match.event is None:
                return False, f"identity: {match.reason}"

            self._touched_events[match.event.id] = match.event

            # Resolved through the canonical bookmaker registry, not
            # built directly from raw.source_id/raw.source -- the same
            # real bookmaker reported by two different providers under
            # two different provider-specific ids (the-odds-api's
            # "bet365" vs api-football's "8") must resolve to the same
            # canonical Bookmaker.id, or every downstream min_bookmakers/
            # consensus/outlier check would see them as two unrelated
            # bookmakers. See storage.bookmaker_catalog.BookmakerCatalog.
            bookmaker = self._bookmaker_catalog.resolve(
                source_bookmaker_id=raw.source_id, source_name=raw.source
            )

            snapshots = [
                OddsSnapshot(
                    event_id=match.event.id,
                    bookmaker=bookmaker,
                    market=raw.market,
                    outcome=outcome,
                    odds=odds,
                    observed_at=raw.observed_at,
                    source_timestamp=raw.source_timestamp,
                )
                for outcome, odds in raw.odds.items()
            ]
            # One transaction for every outcome of this record, so a
            # failure partway through never leaves a partial market
            # snapshot (e.g. "1" and "X" saved but "2" missing).
            self._odds_repository.save_all(snapshots)

            return True, None
        except Exception as exc:
            logger.error(
                "ingestion.record.processing_failed",
                extra={
                    "source": raw.source,
                    "home_team": raw.home_team,
                    "away_team": raw.away_team,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            return False, f"processing-error: {type(exc).__name__}: {exc}"

    def _save_raw_payload(
        self,
        *,
        run_id: str,
        raw: RawEventOdds,
        accepted: bool,
        reason: str | None,
        received_at: datetime,
    ) -> None:
        self._raw_payload_repository.save(
            collector_run_id=run_id,
            source=raw.source,
            payload=serialize_raw_event_odds(raw),
            accepted=accepted,
            received_at=received_at,
            rejection_reason=reason,
        )

    def _record_run(
        self,
        *,
        run_id: str,
        started_at: datetime,
        status: CollectorRunStatus,
        records_received: int,
        records_accepted: int,
        records_rejected: int,
        rejection_reasons: list[str],
        source_payload: str | None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> CollectorRun:
        run = CollectorRun(
            id=run_id,
            source=self._collector.source,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            status=status,
            records_received=records_received,
            records_accepted=records_accepted,
            records_rejected=records_rejected,
            collector_version=self._collector_version,
            error_type=error_type,
            error_message=error_message,
            provider_id=self._collector.provider_id,
            parser_version=self._collector.parser_version,
            source_payload=source_payload,
        )
        self._collector_run_repository.save(run)

        logger.info(
            "ingestion.run.completed",
            extra={
                "run_id": run.id,
                "source": run.source,
                "status": run.status.value,
                "records_received": run.records_received,
                "records_accepted": run.records_accepted,
                "records_rejected": run.records_rejected,
                "duration_seconds": run.duration_seconds,
                "acceptance_rate": run.acceptance_rate,
            },
        )

        if self._metrics is not None:
            self._metrics.record_run(run, rejection_reasons)

        return run
