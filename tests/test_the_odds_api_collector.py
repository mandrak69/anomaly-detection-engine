import json
import urllib.error
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from anomaly_detection_engine.collectors.the_odds_api_collector import (
    API_KEY_ENV_VAR,
    TheOddsApiCollector,
    TheOddsApiError,
)

SAMPLE_RESPONSE = [
    {
        "id": "abc123",
        "sport_key": "soccer_epl",
        "sport_title": "EPL",
        "commence_time": "2026-09-01T20:00:00Z",
        "home_team": "Manchester United",
        "away_team": "Liverpool",
        "bookmakers": [
            {
                "key": "bet365",
                "title": "Bet365",
                "last_update": "2026-08-27T10:00:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Manchester United", "price": 2.15},
                            {"name": "Liverpool", "price": 3.20},
                            {"name": "Draw", "price": 3.45},
                        ],
                    }
                ],
            },
            {
                "key": "incomplete_book",
                "title": "IncompleteBook",
                "last_update": "2026-08-27T10:00:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Manchester United", "price": 2.10},
                            {"name": "Liverpool", "price": 3.10},
                        ],
                    }
                ],
            },
        ],
    }
]


def fetch_stub(response_events):
    def fetch(url: str) -> bytes:
        return json.dumps(response_events).encode("utf-8")

    return fetch


def test_maps_response_into_raw_event_odds_per_complete_bookmaker():
    collector = TheOddsApiCollector(
        sport_key="soccer_epl",
        api_key="test-key",
        fetch=fetch_stub(SAMPLE_RESPONSE),
    )

    result = collector.collect()

    assert len(result) == 1  # incomplete_book has no Draw outcome, skipped
    raw = result[0]

    assert raw.source == "Bet365"
    assert raw.sport == "football"
    assert raw.league == "EPL"
    assert raw.home_team == "Manchester United"
    assert raw.away_team == "Liverpool"
    assert raw.odds == {
        "1": Decimal("2.15"),
        "2": Decimal("3.20"),
        "X": Decimal("3.45"),
    }
    assert raw.start_time.tzinfo is not None
    assert raw.observed_at.tzinfo is not None
    assert raw.source_timestamp.tzinfo is not None


def test_source_identifies_the_sport_key():
    collector = TheOddsApiCollector(
        sport_key="soccer_epl", api_key="test-key", fetch=fetch_stub([])
    )
    assert collector.source == "the-odds-api:soccer_epl"


def test_raises_when_no_api_key_available(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)

    with pytest.raises(TheOddsApiError):
        TheOddsApiCollector(sport_key="soccer_epl", fetch=fetch_stub([]))


def test_uses_api_key_from_environment_variable(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV_VAR, "from-env")

    collector = TheOddsApiCollector(sport_key="soccer_epl", fetch=fetch_stub([]))

    assert collector.collect() == []


def test_collect_never_logs_the_api_key(caplog):
    secret_key = "super-secret-key-value"
    collector = TheOddsApiCollector(
        sport_key="soccer_epl",
        api_key=secret_key,
        fetch=fetch_stub(SAMPLE_RESPONSE),
    )

    with caplog.at_level("DEBUG"):
        collector.collect()

    assert secret_key not in caplog.text


def test_http_error_is_wrapped_in_the_odds_api_error():
    collector = TheOddsApiCollector(
        sport_key="soccer_epl", api_key="bad-key"
    )

    http_error = urllib.error.HTTPError(
        url="https://api.the-odds-api.com/v4/sports/soccer_epl/odds/",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=Mock(read=lambda: b'{"message": "Invalid API key"}'),
    )

    with patch("urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(TheOddsApiError, match="401"):
            collector.collect()
