from datetime import datetime
from pathlib import Path

from anomaly_detection_engine.collectors.json_collector import JsonOddsCollector
from anomaly_detection_engine.ingestion.service import OddsIngestionService
from anomaly_detection_engine.matching.event_matcher import EventMatcher
from anomaly_detection_engine.models.collector_run import CollectorRunStatus
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.normalization.team_normalizer import TeamNormalizer

import sqlite3

from anomaly_detection_engine.storage.collector_run_repository import CollectorRunRepository
from anomaly_detection_engine.storage.database import initialize_database
from anomaly_detection_engine.storage.odds_repository import OddsRepository
from anomaly_detection_engine.storage.raw_payload_repository import RawPayloadRepository

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dirty_odds_sample.json"


def build_matcher() -> EventMatcher:
    event = Event(
        id="event-001",
        sport="football",
        league="demo-league",
        home_team=Team("team-001", "Manchester United"),
        away_team=Team("team-002", "Liverpool"),
        start_time=datetime.fromisoformat("2026-09-01T20:00:00+00:00"),
    )
    normalizer = TeamNormalizer(
        ["Manchester United", "Liverpool"],
        fuzzy_threshold=90,
    )
    return EventMatcher([event], normalizer)


def test_ingestion_rejects_each_kind_of_dirty_record_but_keeps_the_valid_one():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)

    odds_repository = OddsRepository(connection)
    collector_run_repository = CollectorRunRepository(connection)
    raw_payload_repository = RawPayloadRepository(connection)

    service = OddsIngestionService(
        collector=JsonOddsCollector(FIXTURE_PATH),
        matcher=build_matcher(),
        odds_repository=odds_repository,
        collector_run_repository=collector_run_repository,
        raw_payload_repository=raw_payload_repository,
    )

    run = service.run()

    # Row 1 is valid; rows 2-5 are rejected for structural (empty home_team),
    # semantic (naive observed_at), semantic (odds <= 1.0), and identity
    # (unknown teams, no event match) reasons respectively.
    assert run.records_received == 5
    assert run.records_accepted == 1
    assert run.records_rejected == 4
    assert run.status == CollectorRunStatus.PARTIAL

    snapshots = odds_repository.find_by_event("event-001")
    assert len(snapshots) == 3
    assert {snapshot.bookmaker.name for snapshot in snapshots} == {"Mozzart"}

    raw_payloads = raw_payload_repository.find_by_collector_run(run.id)
    assert len(raw_payloads) == 5

    reasons = {p.source: p.rejection_reason for p in raw_payloads if not p.accepted}
    assert reasons["MaxBet"].startswith("structural:")
    assert reasons["Soccer"].startswith("semantic:")
    assert reasons["BadOdds"].startswith("semantic:")
    assert reasons["GhostBook"].startswith("identity:")
