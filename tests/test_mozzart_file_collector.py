import json
from decimal import Decimal

import pytest

from anomaly_detection_engine.collectors.mozzart_file_collector import (
    MozzartFileCollector,
    MozzartResponseError,
)
from anomaly_detection_engine.models.market import EventLifecycle, MarketPhase


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


# A genuine live capture: status.isLive=True (see
# _resolve_market_and_lifecycle). This is football_match()'s default so
# every pre-existing test in this file keeps exercising the live path
# unchanged.
LIVE_STATUS = {"id": 1, "name": "Prvo poluvreme", "isLive": True}

# A genuine pre-match capture: status.name == "Nije počeo" ("not
# started"), no isLive key at all -- confirmed against a real Mozzart
# pre-match response (see mozzart_file_collector._resolve_market_and_lifecycle
# docstring). betStatus="STARTED" is included deliberately: it's present
# in real pre-match captures too and must NOT be mistaken for "match
# started".
NOT_STARTED_STATUS = {"id": 0, "name": "Nije počeo"}


def football_match(
    match_id=1,
    home="Partizan",
    away="Crvena Zvezda",
    home_id=None,
    away_id=None,
    competition="Super liga Srbije",
    competition_id=None,
    start_time_ms=1787904000000,
    outcomes=None,
    sport_name="Fudbal",
    include_final_result=True,
    status=None,
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

    home_obj = {"name": home}
    if home_id is not None:
        home_obj["id"] = home_id
    away_obj = {"name": away}
    if away_id is not None:
        away_obj["id"] = away_id
    competition_obj = {"name": competition}
    if competition_id is not None:
        competition_obj["id"] = competition_id

    return {
        "id": match_id,
        "sport": {"name": sport_name},
        "competition": competition_obj,
        "home": home_obj,
        "visitor": away_obj,
        "startTime": start_time_ms,
        "oddsGroup": groups,
        "status": LIVE_STATUS if status is None else status,
        "betStatus": "STARTED",
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
    # Resolved from this match's own status.isLive=True, not assumed from
    # which endpoint the capture came from (see
    # _resolve_market_and_lifecycle).
    assert raw.market.phase == MarketPhase.LIVE
    assert raw.lifecycle == EventLifecycle.LIVE
    assert raw.source_event_id == "1"


def test_raw_event_odds_carries_the_provider_team_and_competition_ids(tmp_path):
    drop_capture(tmp_path, [football_match(home_id=94299, away_id=94296, competition_id=4187)])

    collector = MozzartFileCollector(tmp_path)
    raw = collector.collect().records[0]

    assert raw.source_home_team_id == "94299"
    assert raw.source_away_team_id == "94296"
    assert raw.source_competition_id == "4187"
    # No country/region field is confirmed present in a real Mozzart
    # capture (only an opaque, unconfirmed `originId`) -- left unset
    # rather than guessed.
    assert raw.country is None


def test_missing_provider_ids_leave_the_new_fields_unset(tmp_path):
    drop_capture(tmp_path, [football_match()])

    collector = MozzartFileCollector(tmp_path)
    raw = collector.collect().records[0]

    assert raw.source_home_team_id is None
    assert raw.source_away_team_id is None
    assert raw.source_competition_id is None


def test_maps_a_pre_match_match_into_raw_event_odds(tmp_path):
    drop_capture(tmp_path, [football_match(status=NOT_STARTED_STATUS)])

    collector = MozzartFileCollector(tmp_path)
    result = collector.collect().records

    assert len(result) == 1
    raw = result[0]
    # status.name == "Nije počeo" ("not started"), despite this match's
    # betStatus reading "STARTED" the same as a live match's would --
    # betStatus means "this market accepts bets", not "kickoff happened"
    # (see _resolve_market_and_lifecycle).
    assert raw.market.phase == MarketPhase.PRE_MATCH
    assert raw.lifecycle == EventLifecycle.SCHEDULED


def test_skips_matches_with_an_unrecognized_status(tmp_path):
    drop_capture(tmp_path, [football_match(status={"id": 99, "name": "Prekinut"})])

    collector = MozzartFileCollector(tmp_path)
    # No silent guess for a status this project hasn't confirmed the
    # meaning of yet -- see _resolve_market_and_lifecycle.
    assert collector.collect().records == []


def test_skips_matches_with_no_status_at_all(tmp_path):
    drop_capture(tmp_path, [football_match(status={})])

    collector = MozzartFileCollector(tmp_path)
    assert collector.collect().records == []


def test_single_capture_mixing_live_and_pre_match_tags_each_correctly(tmp_path):
    drop_capture(
        tmp_path,
        [
            football_match(match_id=1, home="LiveHome", status=LIVE_STATUS),
            football_match(match_id=2, home="PreMatchHome", status=NOT_STARTED_STATUS),
        ],
    )

    collector = MozzartFileCollector(tmp_path)
    result = collector.collect().records

    assert len(result) == 2
    by_home = {r.home_team: r for r in result}
    assert by_home["LiveHome"].market.phase == MarketPhase.LIVE
    assert by_home["LiveHome"].lifecycle == EventLifecycle.LIVE
    assert by_home["PreMatchHome"].market.phase == MarketPhase.PRE_MATCH
    assert by_home["PreMatchHome"].lifecycle == EventLifecycle.SCHEDULED


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
