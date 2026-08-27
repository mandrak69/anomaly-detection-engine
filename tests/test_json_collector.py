from datetime import datetime
from decimal import Decimal
from pathlib import Path

from anomaly_detection_engine.collectors.json_collector import (
    DEFAULT_MARKET,
    JsonOddsCollector,
)


def test_json_collector_reads_sample_data():
    sample_path = Path("data/samples/odds_sample.json")

    collector = JsonOddsCollector(sample_path)

    result = collector.collect()

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
