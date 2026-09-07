import sqlite3
from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.collectors.base import CollectionResult, OddsCollector
from anomaly_detection_engine.ingestion.service import OddsIngestionService
from anomaly_detection_engine.matching.event_matcher import EventMatcher
from anomaly_detection_engine.models.collector_run import CollectorRunStatus
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.market import (
    MarketIdentity,
    MarketPeriod,
    MarketPhase,
    MarketType,
)
from anomaly_detection_engine.models.raw_odds import RawEventOdds
from anomaly_detection_engine.normalization.team_normalizer import TeamNormalizer
from anomaly_detection_engine.storage.bookmaker_catalog import BookmakerCatalog
from anomaly_detection_engine.storage.collector_run_repository import CollectorRunRepository
from anomaly_detection_engine.storage.database import configure_connection, initialize_database
from anomaly_detection_engine.storage.odds_repository import OddsRepository
from anomaly_detection_engine.storage.raw_payload_repository import RawPayloadRepository

MARKET = MarketIdentity(
    market_type=MarketType.THREE_WAY, period=MarketPeriod.FULL_TIME, phase=MarketPhase.PRE_MATCH
)


class StubCollector(OddsCollector):
    def __init__(self, raw_events=None, error: Exception | None = None):
        self._raw_events = raw_events or []
        self._error = error

    @property
    def source(self) -> str:
        return "stub"

    @property
    def provider_id(self) -> str:
        return "stub"

    @property
    def parser_version(self) -> str:
        return "1"

    def collect(self) -> CollectionResult:
        if self._error is not None:
            raise self._error
        return CollectionResult(source_payload="stub-payload", records=self._raw_events)


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
        competition_id="competition-1",
        home_team=Team("team-001", "Manchester United"),
        away_team=Team("team-002", "Liverpool"),
        start_time=datetime.fromisoformat("2026-09-01T20:00:00+00:00"),
    )
    normalizer = TeamNormalizer(["Manchester United", "Liverpool"])
    return EventMatcher([event], normalizer)


def build_service(collector):
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
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
        bookmaker_catalog=BookmakerCatalog(connection, provider_id="test"),
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

    # Bookmaker.id is now the canonical bookmaker registry's own
    # generated id (see BookmakerCatalog), not derived from the raw
    # source_id/name -- only the display name is still the raw one.
    assert snapshots[0].bookmaker.id.startswith("bookmaker-")
    assert snapshots[0].bookmaker.name == "Mozzart"


def test_successful_run_persists_provenance_metadata_on_the_collector_run():
    # provider_id/parser_version/source_payload let a future reprocessing
    # script know which parser to re-run against exactly which historical
    # response -- see CollectorRun/CollectionResult.
    collector = StubCollector(raw_events=[build_raw_event()])
    service, _, collector_run_repository, _ = build_service(collector)

    run = service.run()

    stored = collector_run_repository.find_by_id(run.id)
    assert stored.provider_id == "stub"
    assert stored.parser_version == "1"
    assert stored.source_payload == "stub-payload"


def test_collector_failure_leaves_source_payload_unset():
    collector = StubCollector(error=ValueError("network exploded"))
    service, _, collector_run_repository, _ = build_service(collector)

    run = service.run()

    stored = collector_run_repository.find_by_id(run.id)
    assert stored.status == CollectorRunStatus.FAILED
    assert stored.source_payload is None
    # provider_id/parser_version are static collector properties, known
    # even when collect() itself raised before producing anything.
    assert stored.provider_id == "stub"
    assert stored.parser_version == "1"


def test_stable_source_id_survives_a_display_name_change():
    # A source that provides its own stable identifier (e.g.
    # the-odds-api's "bet365") must keep resolving to the same canonical
    # Bookmaker even if the display name changes ("Bet365" -> "Bet365
    # UK") between polls -- otherwise ingestion would treat the renamed
    # bookmaker as a new, unrelated one. See BookmakerCatalog: the
    # canonical id itself is never derived from source_id/source_name,
    # only cached against them.
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    bookmaker_catalog = BookmakerCatalog(connection, provider_id="stub")

    odds_repository = OddsRepository(connection)

    def run_with(source_name: str, observed_at: datetime) -> None:
        service = OddsIngestionService(
            collector=StubCollector(
                raw_events=[
                    build_raw_event(
                        source=source_name, source_id="bet365", observed_at=observed_at
                    )
                ]
            ),
            matcher=build_matcher(),
            odds_repository=odds_repository,
            collector_run_repository=CollectorRunRepository(connection),
            raw_payload_repository=RawPayloadRepository(connection),
            bookmaker_catalog=bookmaker_catalog,
        )
        service.run()

    run_with("Bet365 UK", datetime.fromisoformat("2026-08-27T10:00:00+00:00"))
    run_with("Bet365", datetime.fromisoformat("2026-08-27T11:00:00+00:00"))

    ids = {s.bookmaker.id for s in odds_repository.find_by_event("event-001")}
    assert len(ids) == 1
    assert next(iter(ids)).startswith("bookmaker-")


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


def test_touched_events_reports_only_events_actually_matched():
    # touched_events is what run_ingestion() uses (see pipeline.py) to
    # scope analysis instead of every event a persistent FixtureCatalog
    # has ever created -- a record that never resolves an event must not
    # show up here, the same as it never gets a snapshot saved.
    valid = build_raw_event()
    unmatched = build_raw_event(home_team="Totally Unknown Team FC")

    collector = StubCollector(raw_events=[valid, unmatched])
    service, _, _, _ = build_service(collector)

    service.run()

    assert [event.id for event in service.touched_events] == ["event-001"]


def test_touched_events_deduplicates_repeated_records_for_the_same_event():
    collector = StubCollector(raw_events=[build_raw_event(), build_raw_event(source="Soccer")])
    service, _, _, _ = build_service(collector)

    service.run()

    assert len(service.touched_events) == 1
    assert service.touched_events[0].id == "event-001"


def test_touched_events_is_empty_before_run_and_when_nothing_matches():
    unmatched = build_raw_event(home_team="Totally Unknown Team FC")
    collector = StubCollector(raw_events=[unmatched])
    service, _, _, _ = build_service(collector)

    assert service.touched_events == []

    service.run()

    assert service.touched_events == []


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
    configure_connection(connection)
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
        bookmaker_catalog=BookmakerCatalog(connection, provider_id="stub"),
        metrics=metrics,
    )

    service.run()

    snapshot = metrics.snapshot()
    assert snapshot["total_runs"] == 1
    assert snapshot["total_accepted"] == 1
    assert snapshot["total_rejected"] == 1
    assert snapshot["rejections_by_reason"] == {"semantic": 1}


class _FlakyMatcher:
    """Raises for one specific home_team, to simulate a matcher/
    FixtureCatalog bug or transient error partway through a run -- not a
    validation failure of the record itself."""

    def __init__(self, event: Event, *, raises_for: str):
        normalizer = TeamNormalizer(
            [event.home_team.canonical_name, event.away_team.canonical_name]
        )
        self._matcher = EventMatcher([event], normalizer)
        self._raises_for = raises_for

    def match(self, **kwargs):
        if kwargs["home_team_raw"] == self._raises_for:
            raise RuntimeError("simulated matcher failure")
        return self._matcher.match(**kwargs)


def test_a_record_that_raises_during_matching_is_rejected_without_aborting_the_run():
    good = build_raw_event()
    poison = build_raw_event(home_team="Poison FC")

    event = Event(
        id="event-001",
        sport="football",
        league="demo-league",
        competition_id="competition-1",
        home_team=Team("team-001", "Manchester United"),
        away_team=Team("team-002", "Liverpool"),
        start_time=datetime.fromisoformat("2026-09-01T20:00:00+00:00"),
    )

    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)

    odds_repository = OddsRepository(connection)
    collector_run_repository = CollectorRunRepository(connection)
    raw_payload_repository = RawPayloadRepository(connection)

    service = OddsIngestionService(
        collector=StubCollector(raw_events=[good, poison]),
        matcher=_FlakyMatcher(event, raises_for="Poison FC"),
        odds_repository=odds_repository,
        collector_run_repository=collector_run_repository,
        raw_payload_repository=raw_payload_repository,
        bookmaker_catalog=BookmakerCatalog(connection, provider_id="stub"),
    )

    # Must not raise -- the run has to reach a final CollectorRun state
    # even though one record's processing raised an unexpected exception.
    run = service.run()

    assert run.status == CollectorRunStatus.PARTIAL
    assert run.records_received == 2
    assert run.records_accepted == 1
    assert run.records_rejected == 1

    assert len(odds_repository.find_by_event("event-001")) == 3

    raw_payloads = raw_payload_repository.find_by_collector_run(run.id)
    assert len(raw_payloads) == 2
    rejected = next(p for p in raw_payloads if not p.accepted)
    assert rejected.rejection_reason.startswith("processing-error: RuntimeError")


def test_collector_failure_produces_failed_run_with_error_details():
    collector = StubCollector(error=ValueError("source unreachable"))
    service, _, collector_run_repository, _ = build_service(collector)

    run = service.run()

    assert run.status == CollectorRunStatus.FAILED
    assert run.records_received == 0
    assert run.error_type == "ValueError"
    assert run.error_message == "source unreachable"


class _RawPayloadRepositoryThatAlwaysFails:
    def save(self, **kwargs):
        raise sqlite3.OperationalError("disk full (simulated)")


def test_raw_payload_save_failure_makes_the_run_partial_not_success():
    # Every record here is otherwise perfectly valid and accepted -- the
    # only thing going wrong is the audit trail itself. Raw payload
    # retention is part of this system's auditability, not a nicety a
    # run can silently lose while still reporting SUCCESS.
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)

    service = OddsIngestionService(
        collector=StubCollector(raw_events=[build_raw_event()]),
        matcher=build_matcher(),
        odds_repository=OddsRepository(connection),
        collector_run_repository=CollectorRunRepository(connection),
        raw_payload_repository=_RawPayloadRepositoryThatAlwaysFails(),
        bookmaker_catalog=BookmakerCatalog(connection, provider_id="stub"),
    )

    run = service.run()

    assert run.status == CollectorRunStatus.PARTIAL
    assert run.records_accepted == 1
    assert run.records_rejected == 0


class _RaisesAfterFirstItem:
    """Simulates the collector's own iterable raising partway through
    iteration (not at collect()-call time) -- e.g. a generator-based
    collector whose underlying HTTP stream drops mid-response."""

    def __init__(self, first_item):
        self._first_item = first_item

    def __iter__(self):
        yield self._first_item
        raise RuntimeError("stream dropped mid-iteration")


class _RaisesImmediately:
    def __iter__(self):
        raise RuntimeError("stream dropped before any item")
        yield  # pragma: no cover -- unreachable, makes this a generator


def test_unexpected_abort_after_partial_success_is_partial_with_error_details():
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)

    service = OddsIngestionService(
        collector=StubCollector(raw_events=_RaisesAfterFirstItem(build_raw_event())),
        matcher=build_matcher(),
        odds_repository=OddsRepository(connection),
        collector_run_repository=CollectorRunRepository(connection),
        raw_payload_repository=RawPayloadRepository(connection),
        bookmaker_catalog=BookmakerCatalog(connection, provider_id="stub"),
    )

    # Must not raise -- even an abort with nothing salvageable still
    # needs a final CollectorRun state.
    run = service.run()

    assert run.status == CollectorRunStatus.PARTIAL
    assert run.records_accepted == 1
    assert run.error_type == "RuntimeError"
    assert run.error_message == "stream dropped mid-iteration"


def test_unexpected_abort_before_anything_succeeds_is_failed_with_error_details():
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)

    service = OddsIngestionService(
        collector=StubCollector(raw_events=_RaisesImmediately()),
        matcher=build_matcher(),
        odds_repository=OddsRepository(connection),
        collector_run_repository=CollectorRunRepository(connection),
        raw_payload_repository=RawPayloadRepository(connection),
        bookmaker_catalog=BookmakerCatalog(connection, provider_id="stub"),
    )

    run = service.run()

    assert run.status == CollectorRunStatus.FAILED
    assert run.records_accepted == 0
    assert run.error_type == "RuntimeError"
    assert run.error_message == "stream dropped before any item"
