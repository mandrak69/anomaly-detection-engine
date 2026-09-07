from datetime import datetime
from decimal import Decimal
from pathlib import Path

from anomaly_detection_engine.collectors.json_collector import JsonOddsCollector
from anomaly_detection_engine.models.market import DEFAULT_MARKET


def test_json_collector_reads_sample_data():
    sample_path = Path("data/samples/odds_sample.json")

    collector = JsonOddsCollector(sample_path)

    collection = collector.collect()
    result = collection.records

    assert collection.source_payload == sample_path.read_text(encoding="utf-8")
    assert len(result) == 6

    first = result[0]

    assert first.source == "Mozzart"
    assert first.sport == "football"
    assert first.league == "demo-league"
    assert first.home_team == "Man Utd"
    assert first.away_team == "Liv"
    assert first.market == DEFAULT_MARKET

    assert first.odds["1"] == Decimal("2.15")
    assert first.odds["X"] == Decimal("3.45")
    assert first.odds["2"] == Decimal("3.20")
    assert all(isinstance(value, Decimal) for value in first.odds.values())

    assert isinstance(first.start_time, datetime)
    assert isinstance(first.observed_at, datetime)
    assert first.start_time.tzinfo is not None
    assert first.observed_at.tzinfo is not None


def test_two_demo_polls_share_one_provider_id():
    # The two sample-data polls each get a distinct source (the filename
    # differs), but both are the same "json-demo" provider -- so
    # FixtureCatalog resolves the second poll's team/competition names
    # against the mapping cache the first poll already built, instead of
    # each poll re-resolving from scratch.
    first_poll = JsonOddsCollector(Path("data/samples/odds_sample.json"))
    second_poll = JsonOddsCollector(Path("data/samples/odds_sample_poll2.json"))

    assert first_poll.provider_id == "json-demo"
    assert second_poll.provider_id == "json-demo"
    assert first_poll.source != second_poll.source
    assert first_poll.parser_version == "1"
