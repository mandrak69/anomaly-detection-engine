import json
from datetime import datetime
from decimal import Decimal

import pytest

from anomaly_detection_engine.collectors.api_football_collector import (
    API_KEY_ENV_VAR,
    ApiFootballCollector,
    ApiFootballError,
    parse_api_football_response,
)
from anomaly_detection_engine.models.market import MarketPhase, MarketType

# Trimmed but structurally real: taken from a genuine api-football.com
# /odds?date=... response (round-trip verified against the full response
# before trimming), just cut down to one fixture / two bookmakers / the
# handful of bet types relevant here.
SAMPLE_ODDS_RESPONSE = {
    "get": "odds",
    "parameters": {"date": "2026-09-08"},
    "errors": [],
    "results": 1,
    "paging": {"current": 1, "total": 1},
    "response": [
        {
            "league": {
                "id": 128,
                "name": "Liga Profesional Argentina",
                "country": "Argentina",
                "season": 2026,
            },
            "fixture": {
                "id": 1493120,
                "timezone": "UTC",
                "date": "2026-09-08T00:15:00+00:00",
                "timestamp": 1788826500,
            },
            "update": "2026-09-07T12:01:17+00:00",
            "bookmakers": [
                {
                    "id": 8,
                    "name": "Bet365",
                    "bets": [
                        {
                            "id": 1,
                            "name": "Match Winner",
                            "values": [
                                {"value": "Home", "odd": "2.80"},
                                {"value": "Draw", "odd": "2.90"},
                                {"value": "Away", "odd": "2.80"},
                            ],
                        },
                        {
                            # Real api-football.com shape: every line the
                            # bookmaker offers bundled into one values
                            # list -- only "... 2.5" must be extracted
                            # (see models.market.TOTALS_2_5_MARKET).
                            "id": 5,
                            "name": "Goals Over/Under",
                            "values": [
                                {"value": "Over 1.5", "odd": "1.50"},
                                {"value": "Under 1.5", "odd": "2.55"},
                                {"value": "Over 2.5", "odd": "2.55"},
                                {"value": "Under 2.5", "odd": "1.50"},
                                {"value": "Over 3.5", "odd": "5.00"},
                                {"value": "Under 3.5", "odd": "1.17"},
                            ],
                        },
                        {
                            # Same bundling pattern, this time with an
                            # explicit sign on the line itself -- only
                            # "... -1" must be extracted (see
                            # models.market.HANDICAP_MINUS_1_MARKET).
                            "id": 9,
                            "name": "Handicap Result",
                            "values": [
                                {"value": "Home -1", "odd": "6.00"},
                                {"value": "Draw -1", "odd": "3.95"},
                                {"value": "Away -1", "odd": "1.42"},
                                {"value": "Home +1", "odd": "1.42"},
                                {"value": "Draw +1", "odd": "4.00"},
                                {"value": "Away +1", "odd": "6.00"},
                            ],
                        },
                    ],
                },
                {
                    # incomplete_book has no "Match Winner" bet at all,
                    # an incomplete 2.5 Over/Under line (no "Under"), and
                    # no Handicap Result bet -- none of the three markets
                    # should produce a record for it.
                    "id": 99,
                    "name": "IncompleteBook",
                    "bets": [
                        {
                            "id": 5,
                            "name": "Goals Over/Under",
                            "values": [{"value": "Over 2.5", "odd": "2.10"}],
                        },
                    ],
                },
            ],
        }
    ],
}

SAMPLE_FIXTURES_RESPONSE = {
    "get": "fixtures",
    "parameters": {"date": "2026-09-08"},
    "errors": [],
    "results": 1,
    "paging": {"current": 1, "total": 1},
    "response": [
        {
            "fixture": {
                "id": 1493120,
                "date": "2026-09-08T00:15:00+00:00",
                "timezone": "UTC",
            },
            "league": {
                "id": 128,
                "name": "Liga Profesional Argentina",
                "country": "Argentina",
                "season": 2026,
            },
            "teams": {
                "home": {"id": 435, "name": "River Plate"},
                "away": {"id": 451, "name": "Boca Juniors"},
            },
        }
    ],
}


def fetch_stub(fixtures_body, odds_body):
    def fetch(url: str) -> bytes:
        if "/fixtures" in url:
            return json.dumps(fixtures_body).encode("utf-8")
        return json.dumps(odds_body).encode("utf-8")

    return fetch


def test_maps_response_into_raw_event_odds_per_complete_bookmaker():
    collector = ApiFootballCollector(
        api_key="test-key",
        date="2026-09-08",
        fetch=fetch_stub(SAMPLE_FIXTURES_RESPONSE, SAMPLE_ODDS_RESPONSE),
    )

    collection = collector.collect()
    result = collection.records

    # Bet365 -> one THREE_WAY + one TOTALS + one HANDICAP record;
    # IncompleteBook has no Match Winner bet, an incomplete 2.5
    # Over/Under line, and no Handicap Result bet at all, so it produces
    # none of the three.
    assert len(result) == 3

    three_way = next(r for r in result if r.market.market_type == MarketType.THREE_WAY)
    assert three_way.source == "Bet365"
    assert three_way.source_id == "8"
    assert three_way.sport == "football"
    assert three_way.league == "Liga Profesional Argentina"
    assert three_way.home_team == "River Plate"
    assert three_way.away_team == "Boca Juniors"
    assert three_way.odds == {
        "1": Decimal("2.80"),
        "X": Decimal("2.90"),
        "2": Decimal("2.80"),
    }
    assert three_way.start_time.tzinfo is not None
    assert three_way.observed_at.tzinfo is not None
    assert three_way.market.phase == MarketPhase.PRE_MATCH
    assert collection.source_payload is not None


def test_extracts_only_the_2_5_line_from_the_bundled_goals_over_under_bet():
    collector = ApiFootballCollector(
        api_key="test-key",
        date="2026-09-08",
        fetch=fetch_stub(SAMPLE_FIXTURES_RESPONSE, SAMPLE_ODDS_RESPONSE),
    )

    result = collector.collect().records

    totals = [r for r in result if r.market.market_type == MarketType.TOTALS]
    assert len(totals) == 1
    raw = totals[0]
    assert raw.source == "Bet365"
    assert raw.market.line == Decimal("2.5")
    assert raw.odds == {"OVER": Decimal("2.55"), "UNDER": Decimal("1.50")}


def test_incomplete_over_under_line_is_skipped():
    # IncompleteBook only has "Over 2.5", no "Under 2.5" -- must not
    # produce a TOTALS record with a hole in its odds dict.
    collector = ApiFootballCollector(
        api_key="test-key",
        date="2026-09-08",
        fetch=fetch_stub(SAMPLE_FIXTURES_RESPONSE, SAMPLE_ODDS_RESPONSE),
    )

    result = collector.collect().records
    assert all(r.source != "IncompleteBook" for r in result)


def test_extracts_only_the_minus_1_line_from_the_bundled_handicap_result_bet():
    collector = ApiFootballCollector(
        api_key="test-key",
        date="2026-09-08",
        fetch=fetch_stub(SAMPLE_FIXTURES_RESPONSE, SAMPLE_ODDS_RESPONSE),
    )

    result = collector.collect().records

    handicap = [r for r in result if r.market.market_type == MarketType.HANDICAP]
    assert len(handicap) == 1
    raw = handicap[0]
    assert raw.source == "Bet365"
    assert raw.market.line == Decimal("-1")
    assert raw.odds == {
        "1": Decimal("6.00"),
        "X": Decimal("3.95"),
        "2": Decimal("1.42"),
    }


def test_source_identifies_the_provider():
    collector = ApiFootballCollector(api_key="test-key", fetch=fetch_stub({}, {}))
    assert collector.source == "api-football"
    assert collector.provider_id == "api-football"
    assert collector.parser_version == "1"


def test_raises_when_no_api_key_available(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)

    with pytest.raises(ApiFootballError):
        ApiFootballCollector(fetch=fetch_stub({}, {}))


def test_uses_api_key_from_environment_variable(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV_VAR, "from-env")

    empty_envelope = {"get": "x", "parameters": {}, "errors": [], "response": []}
    collector = ApiFootballCollector(
        date="2026-09-08", fetch=fetch_stub(empty_envelope, empty_envelope)
    )

    assert collector.collect().records == []


def test_a_fixture_with_odds_but_no_matching_fixtures_entry_is_skipped():
    # The odds response references a fixture.id the /fixtures response
    # has no team names for -- must be skipped, not crash the whole day.
    empty_fixtures = {"get": "fixtures", "parameters": {}, "errors": [], "response": []}
    collector = ApiFootballCollector(
        api_key="test-key",
        date="2026-09-08",
        fetch=fetch_stub(empty_fixtures, SAMPLE_ODDS_RESPONSE),
    )

    assert collector.collect().records == []


def test_errors_field_raises_instead_of_silently_returning_empty():
    # api-football.com returns the same envelope shape for a bad request
    # (e.g. invalid key), just with "errors" populated -- must not be
    # mistaken for "zero fixtures today".
    error_envelope = {
        "get": "odds",
        "parameters": {},
        "errors": {"token": "Invalid API key"},
        "response": [],
    }
    collector = ApiFootballCollector(
        api_key="bad-key",
        date="2026-09-08",
        fetch=fetch_stub(SAMPLE_FIXTURES_RESPONSE, error_envelope),
    )

    with pytest.raises(ApiFootballError):
        collector.collect()


def test_wrong_shaped_response_raises_instead_of_silently_returning_empty():
    not_the_right_shape = {"someOtherKey": []}
    collector = ApiFootballCollector(
        api_key="test-key",
        date="2026-09-08",
        fetch=fetch_stub(SAMPLE_FIXTURES_RESPONSE, not_the_right_shape),
    )

    with pytest.raises(ApiFootballError):
        collector.collect()


def test_collect_never_logs_the_api_key(caplog):
    secret_key = "super-secret-key-value"
    collector = ApiFootballCollector(
        api_key=secret_key,
        date="2026-09-08",
        fetch=fetch_stub(SAMPLE_FIXTURES_RESPONSE, SAMPLE_ODDS_RESPONSE),
    )

    with caplog.at_level("DEBUG"):
        collector.collect()

    assert secret_key not in caplog.text


TWO_FIXTURES_RESPONSE = {
    "get": "fixtures",
    "parameters": {"date": "2026-09-08"},
    "errors": [],
    "results": 2,
    "paging": {"current": 1, "total": 1},
    "response": [
        {
            "fixture": {"id": 2001, "date": "2026-09-08T00:15:00+00:00", "timezone": "UTC"},
            "league": {"id": 128, "name": "League A", "country": "X", "season": 2026},
            "teams": {
                "home": {"id": 1, "name": "Team A1"},
                "away": {"id": 2, "name": "Team A2"},
            },
        },
        {
            "fixture": {"id": 2002, "date": "2026-09-08T02:00:00+00:00", "timezone": "UTC"},
            "league": {"id": 129, "name": "League B", "country": "Y", "season": 2026},
            "teams": {
                "home": {"id": 3, "name": "Team B1"},
                "away": {"id": 4, "name": "Team B2"},
            },
        },
    ],
}


def _match_winner_odds_response(fixture_id: int, *, current: int, total: int) -> dict:
    return {
        "get": "odds",
        "parameters": {"date": "2026-09-08"},
        "errors": [],
        "results": 1,
        "paging": {"current": current, "total": total},
        "response": [
            {
                "league": {"id": 128, "name": "League A", "country": "X", "season": 2026},
                "fixture": {
                    "id": fixture_id,
                    "timezone": "UTC",
                    "date": "2026-09-08T00:15:00+00:00",
                    "timestamp": 1788826500,
                },
                "update": "2026-09-07T12:01:17+00:00",
                "bookmakers": [
                    {
                        "id": 8,
                        "name": "Bet365",
                        "bets": [
                            {
                                "id": 1,
                                "name": "Match Winner",
                                "values": [
                                    {"value": "Home", "odd": "2.00"},
                                    {"value": "Draw", "odd": "3.00"},
                                    {"value": "Away", "odd": "4.00"},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


def _paged_fetch_stub(fixtures_pages: dict[int, dict], odds_pages: dict[int, dict]):
    """Like fetch_stub, but the response for each endpoint can differ by
    the requested `page=` query param -- page 1 is requested with no
    page param at all (see _fetch_all_pages), so it is looked up under
    key 1 the same as every other page.
    """
    requested_urls: list[str] = []

    def fetch(url: str) -> bytes:
        requested_urls.append(url)
        page = int(url.rsplit("page=", 1)[1]) if "page=" in url else 1
        pages = fixtures_pages if "/fixtures" in url else odds_pages
        return json.dumps(pages[page]).encode("utf-8")

    fetch.requested_urls = requested_urls
    return fetch


def test_collect_fetches_and_merges_every_page_of_a_paginated_odds_response():
    odds_pages = {
        1: _match_winner_odds_response(2001, current=1, total=2),
        2: _match_winner_odds_response(2002, current=2, total=2),
    }
    fetch = _paged_fetch_stub({1: TWO_FIXTURES_RESPONSE}, odds_pages)
    collector = ApiFootballCollector(api_key="test-key", date="2026-09-08", fetch=fetch)

    result = collector.collect().records

    # One THREE_WAY record per fixture, one fixture per odds page --
    # both pages must have been fetched and merged, not just page 1.
    assert {r.home_team for r in result} == {"Team A1", "Team B1"}
    assert any(url.endswith("&page=2") for url in fetch.requested_urls)


def test_collect_merges_a_paginated_fixtures_response():
    fixtures_pages = {
        1: {**TWO_FIXTURES_RESPONSE, "paging": {"current": 1, "total": 2},
            "response": [TWO_FIXTURES_RESPONSE["response"][0]]},
        2: {**TWO_FIXTURES_RESPONSE, "paging": {"current": 2, "total": 2},
            "response": [TWO_FIXTURES_RESPONSE["response"][1]]},
    }
    odds_pages = {
        1: {
            **_match_winner_odds_response(2001, current=1, total=1),
            "response": [
                _match_winner_odds_response(2001, current=1, total=1)["response"][0],
                _match_winner_odds_response(2002, current=1, total=1)["response"][0],
            ],
        }
    }
    fetch = _paged_fetch_stub(fixtures_pages, odds_pages)
    collector = ApiFootballCollector(api_key="test-key", date="2026-09-08", fetch=fetch)

    result = collector.collect().records

    # Team A2/Team B2 only resolve if both fixtures pages' team names
    # were actually merged before the odds/fixtures join happened.
    assert {r.home_team for r in result} == {"Team A1", "Team B1"}


def test_collect_source_payload_contains_every_fetched_page():
    odds_pages = {
        1: _match_winner_odds_response(2001, current=1, total=2),
        2: _match_winner_odds_response(2002, current=2, total=2),
    }
    fetch = _paged_fetch_stub({1: TWO_FIXTURES_RESPONSE}, odds_pages)
    collector = ApiFootballCollector(api_key="test-key", date="2026-09-08", fetch=fetch)

    collection = collector.collect()

    payload = json.loads(collection.source_payload)
    assert len(payload["odds"]["response"]) == 2


def test_collect_raises_if_pagination_never_reports_completion():
    # current never catches up to total -- must abort loudly rather than
    # fetch pages forever inside an unattended poller.
    def fetch(url: str) -> bytes:
        if "/fixtures" in url:
            return json.dumps(TWO_FIXTURES_RESPONSE).encode("utf-8")
        return json.dumps(_match_winner_odds_response(2001, current=1, total=999)).encode(
            "utf-8"
        )

    collector = ApiFootballCollector(api_key="test-key", date="2026-09-08", fetch=fetch)

    with pytest.raises(ApiFootballError, match="did not finish paginating"):
        collector.collect()


def test_parse_api_football_response_directly():
    result = parse_api_football_response(
        json.dumps(SAMPLE_FIXTURES_RESPONSE),
        json.dumps(SAMPLE_ODDS_RESPONSE),
        observed_at=datetime.fromisoformat("2026-09-07T12:30:00+00:00"),
    )
    assert len(result) == 3
    assert all(r.home_team == "River Plate" for r in result)
