import json
from datetime import datetime
from decimal import Decimal

import anomaly_detection_engine.app as app
from anomaly_detection_engine.collectors.json_collector import DEFAULT_MARKET
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
    # "auto-" prefix specifically so these can never collide with
    # build_demo_events()'s fixed "event-NNN" IDs when combined.
    assert all(e.id.startswith("auto-") for e in events)


def test_default_source_uses_two_json_collector_polls(monkeypatch):
    monkeypatch.delenv("ODDS_SOURCE", raising=False)
    monkeypatch.delenv("MOZZART_CAPTURE_DIR", raising=False)

    collectors, events = app.build_collectors_and_events()

    assert len(collectors) == 2
    assert collectors[0].source == "json:odds_sample.json"
    assert collectors[1].source == "json:odds_sample_poll2.json"
    assert len(events) == 2
    assert {e.id for e in events} == {"event-001", "event-002"}


def test_the_odds_api_source_replays_a_single_collected_batch(monkeypatch):
    monkeypatch.setenv("ODDS_SOURCE", "the-odds-api")
    monkeypatch.setenv("ODDS_SPORT_KEY", "soccer_epl")
    monkeypatch.delenv("MOZZART_CAPTURE_DIR", raising=False)

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


def _mozzart_capture(tmp_path, home="Partizan", away="Crvena Zvezda"):
    match = {
        "sport": {"name": "Fudbal"},
        "competition": {"name": "Super liga Srbije"},
        "home": {"name": home},
        "visitor": {"name": away},
        "startTime": 1787904000000,
        "oddsGroup": [
            {
                "groupName": "Konačan ishod",
                "odds": [
                    {"subgame": {"shortName": "1"}, "value": 2.10, "oddStatus": "ACTIVE"},
                    {"subgame": {"shortName": "X"}, "value": 3.40, "oddStatus": "ACTIVE"},
                    {"subgame": {"shortName": "2"}, "value": 3.20, "oddStatus": "ACTIVE"},
                ],
            }
        ],
    }
    (tmp_path / "live.json").write_text(json.dumps({"items": [match]}), encoding="utf-8")


def test_mozzart_capture_dir_adds_a_supplemental_collector_alongside_json_demo(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("ODDS_SOURCE", raising=False)
    monkeypatch.setenv("MOZZART_CAPTURE_DIR", str(tmp_path))
    _mozzart_capture(tmp_path)

    collectors, events = app.build_collectors_and_events()

    # The two JSON demo polls, plus Mozzart as a third, supplemental one.
    assert len(collectors) == 3
    assert collectors[2].source == f"mozzart-file:{tmp_path.name}"

    # Fixed demo events (event-001/event-002) plus one auto-bootstrapped
    # from Mozzart's own match, with no id collision between the two.
    assert len(events) == 3
    ids = {e.id for e in events}
    assert {"event-001", "event-002"} <= ids
    auto_ids = {i for i in ids if i.startswith("auto-")}
    assert len(auto_ids) == 1

    mozzart_event = next(e for e in events if e.id in auto_ids)
    assert mozzart_event.home_team.canonical_name == "Partizan"
    assert mozzart_event.away_team.canonical_name == "Crvena Zvezda"

    # The capture file must already be archived (collected during the
    # discovery pass), not left waiting to be read again.
    assert not (tmp_path / "live.json").exists()
    assert list((tmp_path / "history").glob("live_*.json"))


def test_no_mozzart_capture_dir_means_no_supplemental_collector(monkeypatch):
    monkeypatch.delenv("ODDS_SOURCE", raising=False)
    monkeypatch.delenv("MOZZART_CAPTURE_DIR", raising=False)

    collectors, events = app.build_collectors_and_events()

    assert len(collectors) == 2
    assert len(events) == 2
