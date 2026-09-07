import json
from decimal import Decimal

import pytest

from anomaly_detection_engine.collectors.mozzart_file_collector import (
    MozzartFileCollector,
    MozzartResponseError,
)
from anomaly_detection_engine.models.market import MarketPhase


def odds_group(name: str, outcomes: dict[str, tuple[str, str, str]]) -> dict:
    """outcomes: {shortName: (value, oddStatus, name)}"""
    return {
        "groupName": name,
        "odds": [
            {
                "subgame": {"shortName": code, "name": entry[2]},
                "value": entry[0],
                "oddStatus": entry[1],
            }
            for code, entry in outcomes.items()
        ],
    }


def football_match(
    match_id=1,
    home="Partizan",
    away="Crvena Zvezda",
    competition="Super liga Srbije",
    start_time_ms=1787904000000,
    outcomes=None,
    sport_name="Fudbal",
    include_final_result=True,
):
    outcomes = outcomes or {
        "1": (2.10, "ACTIVE", home),
        "X": (3.40, "ACTIVE", "nerešeno"),
        "2": (3.20, "ACTIVE", away),
    }
    groups = []
    if include_final_result:
        groups.append(odds_group("Konačan ishod", outcomes))
    groups.append(odds_group("Sledeći gol", {"1": ("1.90", "ACTIVE", home)}))

    return {
        "id": match_id,
        "sport": {"name": sport_name},
        "competition": {"name": competition},
        "home": {"name": home},
        "visitor": {"name": away},
        "startTime": start_time_ms,
        "oddsGroup": groups,
    }


def drop_capture(capture_dir, matches, filename="live.json"):
    capture_dir.mkdir(parents=True, exist_ok=True)
    path = capture_dir / filename
    path.write_text(json.dumps({"items": matches}), encoding="utf-8")
    return path


def test_maps_a_clean_match_into_raw_event_odds(tmp_path):
    drop_capture(tmp_path, [football_match()])

    collector = MozzartFileCollector(tmp_path)
    collection = collector.collect()
    result = collection.records

    assert collection.source_payload is not None
    assert len(result) == 1
    raw = result[0]
    assert raw.source == "Mozzart"
    assert raw.sport == "football"
    assert raw.league == "Super liga Srbije"
    assert raw.home_team == "Partizan"
    assert raw.away_team == "Crvena Zvezda"
    assert raw.odds == {"1": Decimal("2.10"), "X": Decimal("3.40"), "2": Decimal("3.20")}
    assert raw.start_time.tzinfo is not None
    assert raw.observed_at.tzinfo is not None
    # mozzartbet.com's /live/matches is exactly that -- live, in-play
    # odds -- never the pre-match phase (see models.market.MarketPhase).
    assert raw.market.phase == MarketPhase.LIVE


def test_returns_empty_when_no_capture_is_waiting(tmp_path):
    collector = MozzartFileCollector(tmp_path)
    collection = collector.collect()
    assert collection.records == []
    assert collection.source_payload is None


def test_archives_the_capture_after_reading_it(tmp_path):
    drop_path = drop_capture(tmp_path, [football_match()])

    collector = MozzartFileCollector(tmp_path)
    collector.collect()

    assert not drop_path.exists()

    history_dir = tmp_path / "history"
    archived = list(history_dir.glob("live_*.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text())["items"][0]["home"]["name"] == "Partizan"


def test_second_capture_after_first_is_archived_separately(tmp_path):
    collector = MozzartFileCollector(tmp_path)

    drop_capture(tmp_path, [football_match(home="First")])
    first = collector.collect().records

    drop_capture(tmp_path, [football_match(home="Second")])
    second = collector.collect().records

    assert first[0].home_team == "First"
    assert second[0].home_team == "Second"

    history_dir = tmp_path / "history"
    assert len(list(history_dir.glob("live_*.json"))) == 2


def test_corrupt_capture_is_left_in_place_not_archived(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    drop_path = tmp_path / "live.json"
    drop_path.write_text("{not valid json", encoding="utf-8")

    collector = MozzartFileCollector(tmp_path)

    with pytest.raises(json.JSONDecodeError):
        collector.collect()

    assert drop_path.exists()
    assert not (tmp_path / "history").exists()


def test_wrong_shaped_json_raises_instead_of_silently_returning_empty(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    drop_path = tmp_path / "live.json"
    # Valid JSON, but not shaped like a Mozzart response at all -- e.g.
    # the wrong bookmaker's capture landed here by mistake. Must not be
    # mistaken for "zero matches this cycle" (which would look identical
    # to a legitimate quiet moment).
    drop_path.write_text(json.dumps({"someOtherKey": []}), encoding="utf-8")

    collector = MozzartFileCollector(tmp_path)

    with pytest.raises(MozzartResponseError):
        collector.collect()

    assert drop_path.exists()
    assert not (tmp_path / "history").exists()


def test_non_object_json_raises_instead_of_silently_returning_empty(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    drop_path = tmp_path / "live.json"
    drop_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    collector = MozzartFileCollector(tmp_path)

    with pytest.raises(MozzartResponseError):
        collector.collect()

    assert drop_path.exists()


def test_skips_matches_missing_the_final_result_group(tmp_path):
    drop_capture(tmp_path, [football_match(include_final_result=False)])

    collector = MozzartFileCollector(tmp_path)
    assert collector.collect().records == []


def test_skips_deactivated_or_incomplete_outcomes(tmp_path):
    incomplete = football_match(
        match_id=1,
        outcomes={
            "1": ("2.10", "ACTIVE", "Partizan"),
            "X": ("3.40", "DEACTIVATED", "nerešeno"),
            "2": ("3.20", "ACTIVE", "Crvena Zvezda"),
        },
    )
    drop_capture(tmp_path, [incomplete])

    collector = MozzartFileCollector(tmp_path)
    assert collector.collect().records == []


def test_skips_non_football_matches(tmp_path):
    drop_capture(tmp_path, [football_match(sport_name="Košarka")])

    collector = MozzartFileCollector(tmp_path)
    assert collector.collect().records == []


def test_custom_filename(tmp_path):
    drop_capture(tmp_path, [football_match()], filename="mozzart_snapshot.json")

    collector = MozzartFileCollector(tmp_path, filename="mozzart_snapshot.json")
    result = collector.collect().records

    assert len(result) == 1
    assert not (tmp_path / "mozzart_snapshot.json").exists()
    assert list((tmp_path / "history").glob("mozzart_snapshot_*.json"))


def test_source_identifies_the_capture_directory(tmp_path):
    collector = MozzartFileCollector(tmp_path)
    assert collector.source == f"mozzart-file:{tmp_path.name}"


def test_provider_id_is_stable_across_capture_directories(tmp_path):
    # Unlike source (which encodes the capture directory name), two
    # MozzartFileCollector instances pointed at different directories
    # are still the same real provider -- provider_id must not vary
    # with tmp_path.
    other_dir = tmp_path / "other"
    other_dir.mkdir()

    assert MozzartFileCollector(tmp_path).provider_id == "mozzart"
    assert MozzartFileCollector(other_dir).provider_id == "mozzart"


def test_parser_version_defaults_to_1(tmp_path):
    assert MozzartFileCollector(tmp_path).parser_version == "1"
