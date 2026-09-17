import json
from decimal import Decimal

import pytest

from anomaly_detection_engine.collectors.meridianbet_file_collector import (
    MeridianbetFileCollector,
    MeridianbetResponseError,
)
from anomaly_detection_engine.models.market import MarketPhase, MarketType


def selection(name: str, price, state: str = "ACTIVE", placeholder: bool = False) -> dict:
    return {"name": name, "price": price, "state": state, "placeholder": placeholder}


def group(name: str, selections: list, over_under=None) -> dict:
    return {"name": name, "overUnder": over_under, "selections": selections}


def football_event(
    event_id=1,
    home="FK Partizan",
    away="FK Crvena Zvezda",
    league="Super Liga",
    start_time_ms=1787904000000,
    sport_name="Fudbal",
    include_match_winner=True,
    include_totals=True,
    match_winner_selections=None,
    totals_selections=None,
    totals_line=2.5,
) -> dict:
    positions = []
    if include_match_winner:
        sels = match_winner_selections or [
            selection("1", "2.10"), selection("X", "3.40"), selection("2", "3.20"),
        ]
        positions.append({"index": 0, "groups": [group("Konačan Ishod", sels)]})
    if include_totals:
        sels = totals_selections or [selection("Manje", "2.01"), selection("Više", "1.63")]
        positions.append(
            {"index": 1, "groups": [group("Ukupno golova", sels, over_under=totals_line)]}
        )
    return {
        "header": {
            "eventId": event_id,
            "sport": {"name": sport_name},
            "league": {"name": league},
            "startTime": start_time_ms,
            "rivals": [home, away],
            "state": "ACTIVE",
        },
        "positions": positions,
    }


def drop_capture(capture_dir, events, filename="meridianbet.json"):
    capture_dir.mkdir(parents=True, exist_ok=True)
    path = capture_dir / filename
    body = {
        "errorCode": None,
        "parameters": None,
        "errorMessages": None,
        "payload": {
            "leagues": [{"regionName": "Srbija", "leagueName": "Super Liga", "events": events}]
        },
    }
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def drop_flat_capture(capture_dir, events, filename="meridianbet.json"):
    # meridianbet.com's frontend has been observed using a second,
    # differently-wrapped envelope (payload.events directly, no leagues
    # grouping) for the same underlying header/positions event shape --
    # both must be accepted.
    capture_dir.mkdir(parents=True, exist_ok=True)
    path = capture_dir / filename
    body = {
        "errorCode": None,
        "parameters": None,
        "errorMessages": None,
        "payload": {"events": events},
    }
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_maps_a_clean_event_into_two_market_records(tmp_path):
    drop_capture(tmp_path, [football_event()])

    collector = MeridianbetFileCollector(tmp_path)
    collection = collector.collect()
    result = collection.records

    assert collection.source_payload is not None
    assert len(result) == 2

    three_way = next(r for r in result if r.market.market_type == MarketType.THREE_WAY)
    assert three_way.source == "Meridianbet"
    assert three_way.sport == "football"
    assert three_way.league == "Super Liga"
    assert three_way.home_team == "FK Partizan"
    assert three_way.away_team == "FK Crvena Zvezda"
    assert three_way.odds == {"1": Decimal("2.10"), "X": Decimal("3.40"), "2": Decimal("3.20")}
    assert three_way.start_time.tzinfo is not None
    assert three_way.observed_at.tzinfo is not None
    assert three_way.source_event_id == "1"
    # meridianbet.com's pre-match listing is exactly that -- pre-match,
    # never live (see models.market.MarketPhase).
    assert three_way.market.phase == MarketPhase.PRE_MATCH

    totals = next(r for r in result if r.market.market_type == MarketType.TOTALS)
    assert totals.market.line == Decimal("2.5")
    assert totals.odds == {"UNDER": Decimal("2.01"), "OVER": Decimal("1.63")}


def test_accepts_the_flat_payload_events_envelope_shape(tmp_path):
    drop_flat_capture(tmp_path, [football_event()])

    collector = MeridianbetFileCollector(tmp_path)
    result = collector.collect().records

    assert len(result) == 2


def test_returns_empty_when_no_capture_is_waiting(tmp_path):
    collector = MeridianbetFileCollector(tmp_path)
    collection = collector.collect()
    assert collection.records == []
    assert collection.source_payload is None


def test_archives_the_capture_after_reading_it(tmp_path):
    drop_path = drop_capture(tmp_path, [football_event()])

    collector = MeridianbetFileCollector(tmp_path)
    collector.collect()

    assert not drop_path.exists()

    history_dir = tmp_path / "history"
    archived = list(history_dir.glob("meridianbet_*.json"))
    assert len(archived) == 1


def test_second_capture_after_first_is_archived_separately(tmp_path):
    collector = MeridianbetFileCollector(tmp_path)

    drop_capture(tmp_path, [football_event(home="First")])
    first = collector.collect().records

    drop_capture(tmp_path, [football_event(home="Second")])
    second = collector.collect().records

    assert first[0].home_team == "First"
    assert second[0].home_team == "Second"

    history_dir = tmp_path / "history"
    assert len(list(history_dir.glob("meridianbet_*.json"))) == 2


def test_corrupt_capture_is_left_in_place_not_archived(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    drop_path = tmp_path / "meridianbet.json"
    drop_path.write_text("{not valid json", encoding="utf-8")

    collector = MeridianbetFileCollector(tmp_path)

    with pytest.raises(json.JSONDecodeError):
        collector.collect()

    assert drop_path.exists()
    assert not (tmp_path / "history").exists()


def test_wrong_shaped_json_raises_instead_of_silently_returning_empty(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    drop_path = tmp_path / "meridianbet.json"
    # Valid JSON, but not shaped like a Meridianbet pre-match listing
    # response -- e.g. the "market column config" endpoint was captured
    # by mistake (observed live: payload.positions/configuredPositions,
    # no leagues/odds at all). Must not be mistaken for "zero matches
    # this cycle".
    drop_path.write_text(
        json.dumps({"errorCode": None, "payload": {"positions": [], "configuredPositions": True}}),
        encoding="utf-8",
    )

    collector = MeridianbetFileCollector(tmp_path)

    with pytest.raises(MeridianbetResponseError):
        collector.collect()

    assert drop_path.exists()
    assert not (tmp_path / "history").exists()


def test_non_object_json_raises_instead_of_silently_returning_empty(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    drop_path = tmp_path / "meridianbet.json"
    drop_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    collector = MeridianbetFileCollector(tmp_path)

    with pytest.raises(MeridianbetResponseError):
        collector.collect()

    assert drop_path.exists()


def test_skips_events_missing_the_match_winner_group(tmp_path):
    drop_capture(tmp_path, [football_event(include_match_winner=False)])

    collector = MeridianbetFileCollector(tmp_path)
    result = collector.collect().records

    assert len(result) == 1
    assert result[0].market.market_type == MarketType.TOTALS


def test_skips_deactivated_or_incomplete_match_winner_outcomes(tmp_path):
    incomplete = football_event(
        match_winner_selections=[
            selection("1", "2.10"),
            selection("X", "3.40", state="SUSPENDED"),
            selection("2", "3.20"),
        ],
    )
    drop_capture(tmp_path, [incomplete])

    collector = MeridianbetFileCollector(tmp_path)
    result = collector.collect().records

    assert not any(r.market.market_type == MarketType.THREE_WAY for r in result)


def test_skips_placeholder_selections(tmp_path):
    incomplete = football_event(
        match_winner_selections=[
            selection("1", "2.10"),
            selection("X", "3.40", placeholder=True),
            selection("2", "3.20"),
        ],
    )
    drop_capture(tmp_path, [incomplete])

    collector = MeridianbetFileCollector(tmp_path)
    result = collector.collect().records

    assert not any(r.market.market_type == MarketType.THREE_WAY for r in result)


def test_skips_totals_at_a_line_other_than_2_5(tmp_path):
    drop_capture(tmp_path, [football_event(totals_line=1.5)])

    collector = MeridianbetFileCollector(tmp_path)
    result = collector.collect().records

    assert not any(r.market.market_type == MarketType.TOTALS for r in result)


def test_skips_incomplete_totals(tmp_path):
    incomplete = football_event(
        totals_selections=[selection("Manje", "2.01")],
    )
    drop_capture(tmp_path, [incomplete])

    collector = MeridianbetFileCollector(tmp_path)
    result = collector.collect().records

    assert not any(r.market.market_type == MarketType.TOTALS for r in result)


def test_skips_non_football_events(tmp_path):
    drop_capture(tmp_path, [football_event(sport_name="Košarka")])

    collector = MeridianbetFileCollector(tmp_path)
    assert collector.collect().records == []


def test_skips_events_without_exactly_two_rivals(tmp_path):
    event = football_event()
    event["header"]["rivals"] = ["Only One Team"]
    drop_capture(tmp_path, [event])

    collector = MeridianbetFileCollector(tmp_path)
    assert collector.collect().records == []


def test_custom_filename(tmp_path):
    drop_capture(tmp_path, [football_event()], filename="meridianbet_snapshot.json")

    collector = MeridianbetFileCollector(tmp_path, filename="meridianbet_snapshot.json")
    result = collector.collect().records

    assert len(result) == 2
    assert not (tmp_path / "meridianbet_snapshot.json").exists()
    assert list((tmp_path / "history").glob("meridianbet_snapshot_*.json"))


def test_source_identifies_the_capture_directory(tmp_path):
    collector = MeridianbetFileCollector(tmp_path)
    assert collector.source == f"meridianbet-file:{tmp_path.name}"


def test_provider_id_is_stable_across_capture_directories(tmp_path):
    other_dir = tmp_path / "other"
    other_dir.mkdir()

    assert MeridianbetFileCollector(tmp_path).provider_id == "meridianbet"
    assert MeridianbetFileCollector(other_dir).provider_id == "meridianbet"


def test_parser_version_defaults_to_1(tmp_path):
    assert MeridianbetFileCollector(tmp_path).parser_version == "1"
