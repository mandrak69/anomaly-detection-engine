from datetime import datetime
from decimal import Decimal

import anomaly_detection_engine.app as app
from anomaly_detection_engine.collectors.json_collector import DEFAULT_MARKET, JsonOddsCollector
from anomaly_detection_engine.models.raw_odds import RawEventOdds


def build_raw_event(**overrides) -> RawEventOdds:
    defaults = dict(
        source="Bet365",
        sport="football",
        league="EPL",
        home_team="Manchester United",
        away_team="Liverpool",
        start_time=datetime.fromisoformat("2026-09-01T20:00:00+00:00"),
        observed_at=datetime.fromisoformat("2026-08-27T10:00:00+00:00"),
        market=DEFAULT_MARKET,
        odds={"1": Decimal("2.15"), "X": Decimal("3.45"), "2": Decimal("3.20")},
    )
    defaults.update(overrides)
    return RawEventOdds(**defaults)


def test_build_events_from_raw_deduplicates_by_matchup():
    raw_events = [
        build_raw_event(source="Bet365"),
        build_raw_event(source="Pinnacle"),
        build_raw_event(source="William Hill", home_team="Real Madrid", away_team="Barcelona"),
    ]

    events = app.build_events_from_raw(raw_events)

    assert len(events) == 2
    matchups = {(e.home_team.canonical_name, e.away_team.canonical_name) for e in events}
    assert matchups == {
        ("Manchester United", "Liverpool"),
        ("Real Madrid", "Barcelona"),
    }


def test_default_source_uses_two_json_collector_polls(monkeypatch):
    monkeypatch.delenv("ODDS_SOURCE", raising=False)

    collectors, events = app.build_collectors_and_events()

    assert len(collectors) == 2
    assert all(isinstance(collector, JsonOddsCollector) for collector in collectors)
    assert collectors[0].path.name == "odds_sample.json"
    assert collectors[1].path.name == "odds_sample_poll2.json"
    assert len(events) == 2


def test_the_odds_api_source_replays_a_single_collected_batch(monkeypatch):
    monkeypatch.setenv("ODDS_SOURCE", "the-odds-api")
    monkeypatch.setenv("ODDS_SPORT_KEY", "soccer_epl")

    raw_events = [build_raw_event()]
    call_count = {"n": 0}

    class FakeLiveCollector:
        source = "the-odds-api:soccer_epl"

        def __init__(self, sport_key):
            assert sport_key == "soccer_epl"

        def collect(self):
            call_count["n"] += 1
            return raw_events

    monkeypatch.setattr(app, "TheOddsApiCollector", FakeLiveCollector)

    collectors, events = app.build_collectors_and_events()

    assert len(collectors) == 1
    collector = collectors[0]
    assert collector.source == "the-odds-api:soccer_epl"
    assert len(events) == 1
    # Calling collect() again must not hit the (fake) live collector a
    # second time -- it should just replay what was already fetched.
    assert collector.collect() == raw_events
    assert call_count["n"] == 1
