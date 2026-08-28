import json
import os
from decimal import Decimal

import pytest

from anomaly_detection_engine.collectors.mozzart_file_collector import MozzartFileCollector


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


def write_capture(directory, filename, matches, mtime=None):
    path = directory / filename
    path.write_text(json.dumps({"items": matches}), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_maps_a_clean_match_into_raw_event_odds(tmp_path):
    write_capture(tmp_path, "capture1.json", [football_match()])

    collector = MozzartFileCollector(tmp_path)
    result = collector.collect()

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


def test_uses_the_most_recently_modified_file(tmp_path):
    write_capture(
        tmp_path, "older.json", [football_match(match_id=1, home="Old")], mtime=1000
    )
    write_capture(
        tmp_path, "newer.json", [football_match(match_id=2, home="New")], mtime=2000
    )

    collector = MozzartFileCollector(tmp_path)
    result = collector.collect()

    assert len(result) == 1
    assert result[0].home_team == "New"


def test_skips_matches_missing_the_final_result_group(tmp_path):
    write_capture(
        tmp_path,
        "capture.json",
        [football_match(include_final_result=False)],
    )

    collector = MozzartFileCollector(tmp_path)
    assert collector.collect() == []


def test_skips_deactivated_or_incomplete_outcomes(tmp_path):
    incomplete = football_match(
        match_id=1,
        outcomes={
            "1": ("2.10", "ACTIVE", "Partizan"),
            "X": ("3.40", "DEACTIVATED", "nerešeno"),
            "2": ("3.20", "ACTIVE", "Crvena Zvezda"),
        },
    )
    write_capture(tmp_path, "capture.json", [incomplete])

    collector = MozzartFileCollector(tmp_path)
    assert collector.collect() == []


def test_skips_non_football_matches(tmp_path):
    write_capture(
        tmp_path,
        "capture.json",
        [football_match(sport_name="Košarka")],
    )

    collector = MozzartFileCollector(tmp_path)
    assert collector.collect() == []


def test_raises_when_directory_has_no_matching_files(tmp_path):
    collector = MozzartFileCollector(tmp_path)

    with pytest.raises(FileNotFoundError):
        collector.collect()


def test_source_identifies_the_capture_directory(tmp_path):
    collector = MozzartFileCollector(tmp_path)
    assert collector.source == f"mozzart-file:{tmp_path.name}"
