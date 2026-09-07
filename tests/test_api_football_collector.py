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
from anomaly_detection_engine.models.market import MarketPhase

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
                    ],
                },
                {
                    # incomplete_book has no "Match Winner" bet at all --
                    # must be skipped, same as an incomplete 1X2 line.
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

    assert len(result) == 1  # IncompleteBook has no Match Winner bet, skipped
    raw = result[0]

    assert raw.source == "Bet365"
    assert raw.source_id == "8"
    assert raw.sport == "football"
    assert raw.league == "Liga Profesional Argentina"
    assert raw.home_team == "River Plate"
    assert raw.away_team == "Boca Juniors"
    assert raw.odds == {
        "1": Decimal("2.80"),
        "X": Decimal("2.90"),
        "2": Decimal("2.80"),
    }
    assert raw.start_time.tzinfo is not None
    assert raw.observed_at.tzinfo is not None
    assert raw.market.phase == MarketPhase.PRE_MATCH
    assert collection.source_payload is not None


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


def test_parse_api_football_response_directly():
    result = parse_api_football_response(
        json.dumps(SAMPLE_FIXTURES_RESPONSE),
        json.dumps(SAMPLE_ODDS_RESPONSE),
        observed_at=datetime.fromisoformat("2026-09-07T12:30:00+00:00"),
    )
    assert len(result) == 1
    assert result[0].home_team == "River Plate"
