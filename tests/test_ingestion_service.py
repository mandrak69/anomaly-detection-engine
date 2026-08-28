import sqlite3
from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.collectors.base import OddsCollector
from anomaly_detection_engine.ingestion.service import OddsIngestionService
from anomaly_detection_engine.matching.event_matcher import EventMatcher
from anomaly_detection_engine.models.collector_run import CollectorRunStatus
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.models.raw_odds import RawEventOdds
from anomaly_detection_engine.normalization.team_normalizer import TeamNormalizer
from anomaly_detection_engine.storage.collector_run_repository import CollectorRunRepository
from anomaly_detection_engine.storage.database import initialize_database
from anomaly_detection_engine.storage.odds_repository import OddsRepository
from anomaly_detection_engine.storage.raw_payload_repository import RawPayloadRepository

MARKET = MarketIdentity(market_type=MarketType.THREE_WAY, period=MarketPeriod.FULL_TIME)


class StubCollector(OddsCollector):
    def __init__(self, raw_events=None, error: Exception | None = None):
        self._raw_events = raw_events or []
        self._error = error

    @property
    def source(self) -> str:
        return "stub"

    def collect(self):
        if self._error is not None:
            raise self._error
        return self._raw_events


def build_raw_event(**overrides) -> RawEventOdds:
    defaults = dict(
        source="Mozzart",
        sport="football",
        league="demo-league",
        home_team="Manchester United",
        away_team="Liverpool",
        start_time=datetime.fromisoformat("2026-09-01T20:00:00+00:00"),
        observed_at=datetime.fromisoformat("2026-08-27T10:00:00+00:00"),
        market=MARKET,
        odds={
            "1": Decimal("2.15"),
            "X": Decimal("3.45"),
            "2": Decimal("3.20"),
        },
    )
    defaults.update(overrides)
    return RawEventOdds(**defaults)


def build_matcher() -> EventMatcher:
    event = Event(
        id="event-001",
        sport="football",
        league="demo-league",
        home_team=Team("team-001", "Manchester United"),
        away_team=Team("team-002", "Liverpool"),
        start_time=datetime.fromisoformat("2026-09-01T20:00:00+00:00"),
    )
    normalizer = TeamNormalizer(["Manchester United", "Liverpool"])
    return EventMatcher([event], normalizer)


def build_service(collector):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)

    odds_repository = OddsRepository(connection)
    collector_run_repository = CollectorRunRepository(connection)
    raw_payload_repository = RawPayloadRepository(connection)

    service = OddsIngestionService(
        collector=collector,
        matcher=build_matcher(),
        odds_repository=odds_repository,
        collector_run_repository=collector_run_repository,
        raw_payload_repository=raw_payload_repository,
        collector_version="0.1.0",
    )
    return service, odds_repository, collector_run_repository, raw_payload_repository


def test_successful_run_persists_snapshots():
    collector = StubCollector(raw_events=[build_raw_event()])
    service, odds_repository, collector_run_repository, raw_payload_repository = build_service(
        collector
    )

    run = service.run()

    assert run.status == CollectorRunStatus.SUCCESS
    assert run.records_received == 1
    assert run.records_accepted == 1
    assert run.records_rejected == 0
    assert run.source == "stub"

    snapshots = odds_repository.find_by_event("event-001")
    assert len(snapshots) == 3

    assert collector_run_repository.find_by_id(run.id) is not None

    raw_payloads = raw_payload_repository.find_by_collector_run(run.id)
    assert len(raw_payloads) == 1
    assert raw_payloads[0].accepted is True
    assert raw_payloads[0].rejection_reason is None
    assert '"source": "Mozzart"' in raw_payloads[0].payload


def test_partial_run_when_one_record_fails_validation():
    valid = build_raw_event()
    invalid = build_raw_event(odds={"1": Decimal("0.5"), "X": Decimal("3.0"), "2": Decimal("3.0")})

    collector = StubCollector(raw_events=[valid, invalid])
    service, odds_repository, _, raw_payload_repository = build_service(collector)

    run = service.run()

    assert run.status == CollectorRunStatus.PARTIAL
    assert run.records_received == 2
    assert run.records_accepted == 1
    assert run.records_rejected == 1

    assert len(odds_repository.find_by_event("event-001")) == 3

    raw_payloads = raw_payload_repository.find_by_collector_run(run.id)
    assert len(raw_payloads) == 2
    rejected = next(p for p in raw_payloads if not p.accepted)
    assert rejected.rejection_reason.startswith("semantic:")


def test_partial_run_when_event_cannot_be_matched():
    valid = build_raw_event()
    unmatched = build_raw_event(home_team="Totally Unknown Team FC")

    collector = StubCollector(raw_events=[valid, unmatched])
    service, odds_repository, _, raw_payload_repository = build_service(collector)

    run = service.run()

    assert run.status == CollectorRunStatus.PARTIAL
    assert run.records_accepted == 1
    assert run.records_rejected == 1

    raw_payloads = raw_payload_repository.find_by_collector_run(run.id)
    rejected = next(p for p in raw_payloads if not p.accepted)
    assert rejected.rejection_reason.startswith("identity:")


def test_all_records_rejected_returns_failed_status():
    invalid = build_raw_event(odds={"1": Decimal("0.5"), "X": Decimal("3.0"), "2": Decimal("3.0")})

    collector = StubCollector(raw_events=[invalid])
    service, _, _, _ = build_service(collector)

    run = service.run()

    assert run.status == CollectorRunStatus.FAILED
    assert run.records_received == 1
    assert run.records_accepted == 0
    assert run.records_rejected == 1


def test_metrics_are_updated_when_provided():
    import sqlite3

    from anomaly_detection_engine.observability.metrics import IngestionMetrics
    from anomaly_detection_engine.storage.collector_run_repository import (
        CollectorRunRepository,
    )
    from anomaly_detection_engine.storage.raw_payload_repository import RawPayloadRepository

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)

    valid = build_raw_event()
    invalid = build_raw_event(odds={"1": Decimal("0.5"), "X": Decimal("3.0"), "2": Decimal("3.0")})

    metrics = IngestionMetrics()
    service = OddsIngestionService(
        collector=StubCollector(raw_events=[valid, invalid]),
        matcher=build_matcher(),
        odds_repository=OddsRepository(connection),
        collector_run_repository=CollectorRunRepository(connection),
        raw_payload_repository=RawPayloadRepository(connection),
        metrics=metrics,
    )

    service.run()

    snapshot = metrics.snapshot()
    assert snapshot["total_runs"] == 1
    assert snapshot["total_accepted"] == 1
    assert snapshot["total_rejected"] == 1
    assert snapshot["rejections_by_reason"] == {"semantic": 1}


def test_collector_failure_produces_failed_run_with_error_details():
    collector = StubCollector(error=ValueError("source unreachable"))
    service, _, collector_run_repository, _ = build_service(collector)

    run = service.run()

    assert run.status == CollectorRunStatus.FAILED
    assert run.records_received == 0
    assert run.error_type == "ValueError"
    assert run.error_message == "source unreachable"

    assert collector_run_repository.find_by_id(run.id) is not None
