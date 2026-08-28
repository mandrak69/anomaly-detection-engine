import logging
from datetime import datetime, timezone
from uuid import uuid4

from anomaly_detection_engine.collectors.base import OddsCollector
from anomaly_detection_engine.matching.event_matcher import EventMatcher
from anomaly_detection_engine.models.collector_run import CollectorRun, CollectorRunStatus
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.models.raw_odds import RawEventOdds
from anomaly_detection_engine.observability.metrics import IngestionMetrics
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
        matcher: EventMatcher,
        odds_repository: OddsRepository,
        collector_run_repository: CollectorRunRepository,
        raw_payload_repository: RawPayloadRepository,
        collector_version: str | None = None,
        metrics: IngestionMetrics | None = None,
    ) -> None:
        self._collector = collector
        self._matcher = matcher
        self._odds_repository = odds_repository
        self._collector_run_repository = collector_run_repository
        self._raw_payload_repository = raw_payload_repository
        self._collector_version = collector_version
        self._metrics = metrics

    def run(self) -> CollectorRun:
        run_id = str(uuid4())
        started_at = datetime.now(timezone.utc)
        source = self._collector.source

        logger.info("ingestion.run.started", extra={"run_id": run_id, "source": source})

        try:
            raw_events = self._collector.collect()
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
            )

        records_accepted = 0
        records_rejected = 0
        rejection_reasons: list[str] = []

        for raw in raw_events:
            accepted, reason = self._ingest_one(raw)
            self._save_raw_payload(
                run_id=run_id,
                raw=raw,
                accepted=accepted,
                reason=reason,
                received_at=started_at,
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

        if records_accepted == 0 and records_rejected > 0:
            status = CollectorRunStatus.FAILED
        elif records_rejected > 0:
            status = CollectorRunStatus.PARTIAL
        else:
            status = CollectorRunStatus.SUCCESS

        return self._record_run(
            run_id=run_id,
            started_at=started_at,
            status=status,
            records_received=len(raw_events),
            records_accepted=records_accepted,
            records_rejected=records_rejected,
            rejection_reasons=rejection_reasons,
        )

    def _ingest_one(self, raw: RawEventOdds) -> tuple[bool, str | None]:
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

        bookmaker = Bookmaker(raw.source.lower(), raw.source)

        for outcome, odds in raw.odds.items():
            self._odds_repository.save(
                OddsSnapshot(
                    event_id=match.event.id,
                    bookmaker=bookmaker,
                    market=raw.market,
                    outcome=outcome,
                    odds=odds,
                    observed_at=raw.observed_at,
                    source_timestamp=raw.source_timestamp,
                )
            )

        return True, None

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
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> CollectorRun:
        run = CollectorRun(
            id=run_id,
            source=self._collector.source,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            status=status,
            records_received=records_received,
            records_accepted=records_accepted,
            records_rejected=records_rejected,
            collector_version=self._collector_version,
            error_type=error_type,
            error_message=error_message,
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
