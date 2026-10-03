import json
from decimal import Decimal

import pytest

from anomaly_detection_engine.collectors.meridianbet_http_collector import (
    MeridianbetHttpCollector,
    MeridianbetHttpError,
)

SSR_URL = "https://meridianbet.rs/sr/kladjenje/fudbal"
API_BASE_URL = "https://online.meridianbet.rs/betshop/api/v1/offer/sport"
SPORT_ID = 58

VALID_TOKEN = "header.payload.signature"


def ssr_page_html(*, token: str | None = VALID_TOKEN, include_ng_state: bool = True) -> bytes:
    if not include_ng_state:
        return b"<html><body>no ng-state here</body></html>"

    state: dict = {"LANGUAGE_KEY": "sr"}
    if token is not None:
        # meridianbet.rs's own ng-state nests NEW_TOKEN as a JSON-encoded
        # *string*, not a nested object -- same double-encoding this
        # collector's _fetch_token must undo.
        state["NEW_TOKEN"] = json.dumps({"access_token": token})

    body = (
        '<html><body><script id="ng-state" type="application/json">'
        f"{json.dumps(state)}</script></body></html>"
    )
    return body.encode("utf-8")


def selection(name: str, price: str) -> dict:
    return {"name": name, "price": price, "state": "ACTIVE", "placeholder": False}


def football_event(event_id: int, home: str, away: str, league: str = "Premier Liga") -> dict:
    return {
        "header": {
            "eventId": event_id,
            "sport": {"name": "Fudbal"},
            "league": {"name": league},
            "region": {"name": "Engleska"},
            "startTime": 1787904000000,
            "rivals": [home, away],
        },
        "positions": [
            {
                "groups": [
                    {
                        "name": "Konačan Ishod",
                        "selections": [
                            selection("1", "2.10"),
                            selection("X", "3.40"),
                            selection("2", "3.20"),
                        ],
                    }
                ]
            }
        ],
    }


def api_page_response(events: list[dict]) -> bytes:
    body = {
        "errorCode": None,
        "parameters": None,
        "errorMessages": None,
        "payload": {
            "usedTimeFilter": "ALL",
            "leagues": (
                [{"leagueId": 1, "leagueName": "Premier Liga", "events": events}] if events else []
            ),
        },
    }
    return json.dumps(body).encode("utf-8")


def empty_page_response() -> bytes:
    return api_page_response([])


def paginated_fetch_stub(pages: dict[int, list[dict]], *, token: str | None = VALID_TOKEN):
    """Routes the SSR page URL to a token-bearing HTML page, and
    `.../leagues?page=N&time=ALL` to `pages[N]`'s events (or an empty
    page past the last configured page, ending pagination)."""

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        if url == SSR_URL:
            return ssr_page_html(token=token)

        assert url.startswith(f"{API_BASE_URL}/{SPORT_ID}/leagues?page=")
        assert "time=ALL" in url
        assert headers.get("Authorization") == f"Bearer {token}"
        assert headers.get("Accept-Language") == "sr"

        page_num = int(url.split("page=")[1].split("&")[0])
        events = pages.get(page_num, [])
        return api_page_response(events)

    return fetch


def test_maps_a_single_page_into_raw_event_odds():
    events = [football_event(1, "Manchester City", "Arsenal")]
    collector = MeridianbetHttpCollector(fetch=paginated_fetch_stub({0: events}))

    result = collector.collect().records

    assert len(result) == 1
    raw = result[0]
    assert raw.home_team == "Manchester City"
    assert raw.away_team == "Arsenal"
    assert raw.league == "Engleska - Premier Liga"
    assert raw.source_event_id == "1"
    assert raw.odds == {"1": Decimal("2.10"), "X": Decimal("3.40"), "2": Decimal("3.20")}


def test_paginates_until_an_empty_page():
    pages = {
        0: [football_event(1, "Team A1", "Team B1")],
        1: [football_event(2, "Team A2", "Team B2")],
        2: [football_event(3, "Team A3", "Team B3")],
        # page 3 implicitly empty -> stop
    }
    collector = MeridianbetHttpCollector(fetch=paginated_fetch_stub(pages))

    result = collector.collect().records

    assert {r.source_event_id for r in result} == {"1", "2", "3"}


def test_source_payload_contains_every_fetched_page():
    pages = {
        0: [football_event(1, "Team A1", "Team B1")],
        1: [football_event(2, "Team A2", "Team B2")],
    }
    collector = MeridianbetHttpCollector(fetch=paginated_fetch_stub(pages))

    collection = collector.collect()

    payload = json.loads(collection.source_payload)
    assert len(payload) == 2


def test_a_zero_event_first_page_raises_rather_than_silently_collecting_nothing():
    collector = MeridianbetHttpCollector(fetch=paginated_fetch_stub({}))

    with pytest.raises(MeridianbetHttpError, match="zero pages"):
        collector.collect()


def test_missing_ng_state_script_raises():
    def fetch(url: str, headers: dict[str, str]) -> bytes:
        return ssr_page_html(include_ng_state=False)

    collector = MeridianbetHttpCollector(fetch=fetch)

    with pytest.raises(MeridianbetHttpError, match="ng-state"):
        collector.collect()


def test_missing_token_in_ng_state_raises():
    def fetch(url: str, headers: dict[str, str]) -> bytes:
        return ssr_page_html(token=None)

    collector = MeridianbetHttpCollector(fetch=fetch)

    with pytest.raises(MeridianbetHttpError, match="NEW_TOKEN"):
        collector.collect()


def test_missing_accept_language_header_would_be_rejected_upstream():
    # Documents the real, observed failure mode this collector must avoid
    # (400 INVALID_LANGUAGE) by asserting the header this collector sends
    # is exactly what's required -- paginated_fetch_stub's own assertion
    # on headers.get("Accept-Language") already enforces this on every
    # other test; this test exists so that assertion's absence would be
    # noticed if it were ever accidentally removed from the stub.
    events = [football_event(1, "Team A", "Team B")]

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        if url == SSR_URL:
            return ssr_page_html()
        assert "Accept-Language" in headers
        return api_page_response(events if "page=0" in url else [])

    collector = MeridianbetHttpCollector(fetch=fetch)
    collector.collect()


def test_source_and_provider_id():
    collector = MeridianbetHttpCollector()

    assert collector.source == "meridianbet-http"
    assert collector.provider_id == "meridianbet"
