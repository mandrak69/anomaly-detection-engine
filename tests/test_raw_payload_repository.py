import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.models.raw_odds import RawEventOdds
from anomaly_detection_engine.storage.database import initialize_database
from anomaly_detection_engine.storage.raw_payload_repository import (
    RawPayloadRepository,
    serialize_raw_event_odds,
)

MARKET = MarketIdentity(market_type=MarketType.THREE_WAY, period=MarketPeriod.FULL_TIME)


def build_raw_event() -> RawEventOdds:
    return RawEventOdds(
        source="Mozzart",
        sport="football",
        league="demo-league",
        home_team="Manchester United",
        away_team="Liverpool",
        start_time=datetime.fromisoformat("2026-09-01T20:00:00+00:00"),
        observed_at=datetime.fromisoformat("2026-08-27T10:00:00+00:00"),
        market=MARKET,
        odds={"1": Decimal("2.15"), "X": Decimal("3.45"), "2": Decimal("3.20")},
    )


def test_serialize_raw_event_odds_round_trips_through_json():
    payload = serialize_raw_event_odds(build_raw_event())
    decoded = json.loads(payload)

    assert decoded["source"] == "Mozzart"
    assert decoded["odds"]["1"] == "2.15"
    assert decoded["market"]["market_type"] == "three_way"
    assert decoded["start_time"] == "2026-09-01T20:00:00+00:00"


def test_saves_and_finds_raw_payloads_for_a_run():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)

    repository = RawPayloadRepository(connection)
    raw = build_raw_event()

    repository.save(
        collector_run_id="run-001",
        source=raw.source,
        payload=serialize_raw_event_odds(raw),
        accepted=True,
        received_at=datetime.fromisoformat("2026-08-27T10:00:05+00:00"),
    )
    repository.save(
        collector_run_id="run-001",
        source="BadOdds",
        payload="{}",
        accepted=False,
        received_at=datetime.fromisoformat("2026-08-27T10:00:06+00:00"),
        rejection_reason="semantic: invalid-odds-value",
    )
    repository.save(
        collector_run_id="run-002",
        source="Other",
        payload="{}",
        accepted=True,
        received_at=datetime.fromisoformat("2026-08-27T10:00:07+00:00"),
    )

    records = repository.find_by_collector_run("run-001")

    assert len(records) == 2
    assert records[0].accepted is True
    assert records[0].rejection_reason is None
    assert records[1].accepted is False
    assert records[1].rejection_reason == "semantic: invalid-odds-value"
