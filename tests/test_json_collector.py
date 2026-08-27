from datetime import datetime
from pathlib import Path

from anomaly_detection_engine.collectors.json_collector import JsonOddsCollector


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
    assert first.market == "1X2"

    assert first.odds["1"] == 2.15
    assert first.odds["X"] == 3.45
    assert first.odds["2"] == 3.20

    assert isinstance(first.start_time, datetime)
    assert isinstance(first.observed_at, datetime)