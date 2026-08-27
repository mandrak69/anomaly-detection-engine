from datetime import datetime, timezone
from uuid import uuid4

from anomaly_detection_engine.collectors.base import OddsCollector
from anomaly_detection_engine.matching.event_matcher import EventMatcher
from anomaly_detection_engine.models.collector_run import CollectorRun, CollectorRunStatus
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.models.raw_odds import RawEventOdds
from anomaly_detection_engine.storage.collector_run_repository import CollectorRunRepository
from anomaly_detection_engine.storage.odds_repository import OddsRepository
from anomaly_detection_engine.validation.raw_odds_validator import validate_raw_event_odds


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
        collector_version: str | None = None,
    ) -> None:
        self._collector = collector
        self._matcher = matcher
        self._odds_repository = odds_repository
        self._collector_run_repository = collector_run_repository
        self._collector_version = collector_version

    def run(self) -> CollectorRun:
        started_at = datetime.now(timezone.utc)

        try:
            raw_events = self._collector.collect()
        except Exception as exc:
            return self._record_run(
                started_at=started_at,
                status=CollectorRunStatus.FAILED,
                records_received=0,
                records_accepted=0,
                records_rejected=0,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

        records_accepted = 0
        records_rejected = 0

        for raw in raw_events:
            if self._ingest_one(raw):
                records_accepted += 1
            else:
                records_rejected += 1

        if records_accepted == 0 and records_rejected > 0:
            status = CollectorRunStatus.FAILED
        elif records_rejected > 0:
            status = CollectorRunStatus.PARTIAL
        else:
            status = CollectorRunStatus.SUCCESS

        return self._record_run(
            started_at=started_at,
            status=status,
            records_received=len(raw_events),
            records_accepted=records_accepted,
            records_rejected=records_rejected,
        )

    def _ingest_one(self, raw: RawEventOdds) -> bool:
        validation = validate_raw_event_odds(raw)
        if not validation.valid:
            return False

        match = self._matcher.match(
            sport=raw.sport,
            league=raw.league,
            home_team_raw=raw.home_team,
            away_team_raw=raw.away_team,
            start_time=raw.start_time,
        )
        if match.event is None:
            return False

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

        return True

    def _record_run(
        self,
        *,
        started_at: datetime,
        status: CollectorRunStatus,
        records_received: int,
        records_accepted: int,
        records_rejected: int,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> CollectorRun:
        run = CollectorRun(
            id=str(uuid4()),
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
        return run
