import json
from decimal import Decimal

import pytest

from anomaly_detection_engine.collectors.mozzart_http_collector import (
    MozzartHttpCollector,
    MozzartHttpError,
)

MATCHES_URL = "https://www.mozzartbet.com/betting/matches"

NOT_STARTED_STATUS = {"id": 0, "name": "Nije počeo"}


def football_match(
    match_id: int, home: str, away: str, competition: str = "Super liga Srbije"
) -> dict:
    return {
        "id": match_id,
        "sport": {"name": "Fudbal"},
        "competition": {"name": competition},
        "home": {"name": home},
        "visitor": {"name": away},
        "startTime": 1787904000000,
        "oddsGroup": [
            {
                "groupName": "Konačan ishod",
                "odds": [
                    {"subgame": {"shortName": "1"}, "value": "2.10", "oddStatus": "ACTIVE"},
                    {"subgame": {"shortName": "X"}, "value": "3.40", "oddStatus": "ACTIVE"},
                    {"subgame": {"shortName": "2"}, "value": "3.20", "oddStatus": "ACTIVE"},
                ],
            }
        ],
        "status": NOT_STARTED_STATUS,
        "betStatus": "STARTED",
    }


def matches_response(matches: list[dict]) -> bytes:
    return json.dumps({"items": matches}).encode("utf-8")


def paginated_fetch_stub(pages: dict[int, list[dict]]):
    """Routes POST bodies by their currentPage field to pages[N]'s
    matches (or an empty page past the last configured page, ending
    pagination)."""

    def fetch(url: str, body: str) -> bytes:
        assert url == MATCHES_URL
        payload = json.loads(body)
        assert payload["sportId"] == 1
        assert payload["date"] == "all_days"
        page_num = payload["currentPage"]
        return matches_response(pages.get(page_num, []))

    return fetch


def test_maps_a_single_page_into_raw_event_odds():
    matches = [football_match(1, "Partizan", "Crvena Zvezda")]
    collector = MozzartHttpCollector(fetch=paginated_fetch_stub({0: matches}))

    result = collector.collect().records

    assert len(result) == 1
    raw = result[0]
    assert raw.home_team == "Partizan"
    assert raw.away_team == "Crvena Zvezda"
    assert raw.league == "Super liga Srbije"
    assert raw.source_event_id == "1"
    assert raw.odds == {"1": Decimal("2.10"), "X": Decimal("3.40"), "2": Decimal("3.20")}


def test_paginates_starting_at_page_zero_until_an_empty_page():
    pages = {
        0: [football_match(1, "Team A1", "Team B1")],
        1: [football_match(2, "Team A2", "Team B2")],
        2: [football_match(3, "Team A3", "Team B3")],
        # page 3 implicitly empty -> stop
    }
    collector = MozzartHttpCollector(fetch=paginated_fetch_stub(pages))

    result = collector.collect().records

    assert {r.source_event_id for r in result} == {"1", "2", "3"}


def test_source_payload_contains_every_fetched_page():
    pages = {
        0: [football_match(1, "Team A1", "Team B1")],
        1: [football_match(2, "Team A2", "Team B2")],
    }
    collector = MozzartHttpCollector(fetch=paginated_fetch_stub(pages))

    collection = collector.collect()

    payload = json.loads(collection.source_payload)
    assert len(payload) == 2


def test_a_zero_match_first_page_raises_rather_than_silently_collecting_nothing():
    collector = MozzartHttpCollector(fetch=paginated_fetch_stub({}))

    with pytest.raises(MozzartHttpError, match="zero pages"):
        collector.collect()


def test_source_and_provider_id():
    collector = MozzartHttpCollector()

    assert collector.source == "mozzart-http"
    assert collector.provider_id == "mozzart"
