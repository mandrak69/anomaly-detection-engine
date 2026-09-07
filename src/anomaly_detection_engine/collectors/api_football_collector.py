import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from anomaly_detection_engine.collectors.base import CollectionResult, OddsCollector
from anomaly_detection_engine.models.market import DEFAULT_MARKET, TOTALS_2_5_MARKET
from anomaly_detection_engine.models.raw_odds import RawEventOdds

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://v3.football.api-sports.io"
API_KEY_ENV_VAR = "API_FOOTBALL_KEY"
_MATCH_WINNER_BET_NAME = "Match Winner"
_OUTCOME_CODES = {"Home": "1", "Draw": "X", "Away": "2"}
_OVER_UNDER_BET_NAME = "Goals Over/Under"
_TOTALS_2_5_LINE = "2.5"


class ApiFootballError(RuntimeError):
    """Raised for any failure talking to api-football.com (network, HTTP,
    auth), or for a response whose top-level shape doesn't even look like
    one of theirs."""


def parse_api_football_response(
    fixtures_raw: str | bytes, odds_raw: str | bytes, observed_at: datetime
) -> list[RawEventOdds]:
    """Maps one day's api-football.com /fixtures + /odds responses onto
    RawEventOdds -- up to two records per bookmaker per fixture, one for
    the "Match Winner" (1X2, DEFAULT_MARKET) bet and one for the "Goals
    Over/Under" bet's 2.5 line (TOTALS_2_5_MARKET), each only produced
    if that bookmaker actually has a complete line for it.

    Two real differences from every other collector in this project,
    both consequences of api-football.com's actual response shape (not
    a design choice made here): the /odds endpoint is date-scoped
    (?date=YYYY-MM-DD), not sport/competition-scoped, and it identifies
    each entry only by fixture.id -- team names live on the separate
    /fixtures endpoint for the same date. So this function takes both
    responses and joins them locally by fixture.id, instead of parsing
    one self-contained response the way parse_the_odds_api_response/
    parse_mozzart_response do. league name and start_time, by contrast,
    *are* already present directly on the odds response itself, so only
    team names need the join.

    Raises ApiFootballError if either response doesn't have the shape
    api-football.com's envelope always has ({"response": [...],
    "errors": [...], ...}), or if "errors" is non-empty -- their API
    returns this same envelope shape even for a bad request (e.g. an
    invalid key), so a dict without "response" or a populated "errors"
    list is treated the same way an unexpected shape is in every other
    collector here: raised, not silently treated as "zero fixtures
    today".

    A fixture present in the odds response but missing from the
    fixtures response (team names unknown) is skipped -- there is no
    home_team/away_team to build a RawEventOdds from, and skipping one
    fixture must not abort every other one in the same day's response.
    """
    fixtures_data = _load_envelope(fixtures_raw, what="fixtures")
    odds_data = _load_envelope(odds_raw, what="odds")

    team_names = _team_names_by_fixture_id(fixtures_data)

    result: list[RawEventOdds] = []

    for item in odds_data["response"]:
        fixture_id = item.get("fixture", {}).get("id")
        teams = team_names.get(fixture_id)
        if teams is None:
            continue
        home_team, away_team = teams

        league_name = item.get("league", {}).get("name")
        raw_date = item.get("fixture", {}).get("date")
        if not league_name or not raw_date:
            continue
        start_time = datetime.fromisoformat(raw_date)

        for bookmaker in item.get("bookmakers", []):
            bookmaker_id = bookmaker.get("id")
            source_id = str(bookmaker_id) if bookmaker_id is not None else None
            common = {
                "source": bookmaker.get("name") or "unknown",
                "sport": "football",
                "league": league_name,
                "home_team": home_team,
                "away_team": away_team,
                "start_time": start_time,
                "observed_at": observed_at,
                # api-football.com's own stable per-bookmaker id (e.g. 8
                # for "Bet365") -- see RawEventOdds.source_id for why this
                # must not be derived from the display name.
                "source_id": source_id,
            }

            match_winner_odds = _extract_match_winner_odds(bookmaker)
            if match_winner_odds is not None:
                result.append(RawEventOdds(**common, market=DEFAULT_MARKET, odds=match_winner_odds))

            totals_odds = _extract_totals_2_5_odds(bookmaker)
            if totals_odds is not None:
                result.append(RawEventOdds(**common, market=TOTALS_2_5_MARKET, odds=totals_odds))

    return result


def _load_envelope(raw: str | bytes, *, what: str) -> dict:
    data = json.loads(raw, parse_float=Decimal)

    if not isinstance(data, dict) or "response" not in data:
        raise ApiFootballError(
            f"This {what} response doesn't look like an api-football.com "
            f"response (expected an object with a 'response' key) -- check "
            f"the API key/request."
        )

    errors = data.get("errors")
    if errors:
        raise ApiFootballError(f"api-football.com {what} request returned errors: {errors}")

    return data


def _team_names_by_fixture_id(fixtures_data: dict) -> dict[int, tuple[str, str]]:
    result: dict[int, tuple[str, str]] = {}
    for item in fixtures_data["response"]:
        try:
            fixture_id = item["fixture"]["id"]
            home = item["teams"]["home"]["name"]
            away = item["teams"]["away"]["name"]
        except (KeyError, TypeError):
            continue
        result[fixture_id] = (home, away)
    return result


def _extract_match_winner_odds(bookmaker: dict) -> dict[str, Decimal] | None:
    match_winner = next(
        (bet for bet in bookmaker.get("bets", []) if bet.get("name") == _MATCH_WINNER_BET_NAME),
        None,
    )
    if match_winner is None:
        return None

    odds: dict[str, Decimal] = {}
    for value in match_winner.get("values", []):
        code = _OUTCOME_CODES.get(value.get("value"))
        odd = value.get("odd")
        if code is None or odd is None:
            continue
        odds[code] = Decimal(odd)

    # A bookmaker publishing an incomplete 1X2 line isn't usable for this
    # market's analysis; skip it rather than producing a RawEventOdds with
    # a hole in its odds dict.
    if set(odds) != {"1", "X", "2"}:
        return None

    return odds


def _extract_totals_2_5_odds(bookmaker: dict) -> dict[str, Decimal] | None:
    """api-football.com's "Goals Over/Under" bet bundles every line the
    bookmaker offers (0.5, 1.5, 2.5, 3.5, ...) into one values list, each
    entry shaped "Over 2.5"/"Under 2.5" -- this project only extracts the
    2.5 line (see models.market.TOTALS_2_5_MARKET), so every value whose
    line isn't "2.5" is ignored, not just every non-Over/Under value.
    """
    over_under = next(
        (bet for bet in bookmaker.get("bets", []) if bet.get("name") == _OVER_UNDER_BET_NAME),
        None,
    )
    if over_under is None:
        return None

    odds: dict[str, Decimal] = {}
    for value in over_under.get("values", []):
        odd = value.get("odd")
        raw_value = value.get("value")
        if odd is None or not isinstance(raw_value, str):
            continue

        direction, _, line = raw_value.partition(" ")
        if line != _TOTALS_2_5_LINE:
            continue

        if direction == "Over":
            odds["OVER"] = Decimal(odd)
        elif direction == "Under":
            odds["UNDER"] = Decimal(odd)

    if set(odds) != {"OVER", "UNDER"}:
        return None

    return odds


class ApiFootballCollector(OddsCollector):
    """Collector for https://www.api-football.com (api-sports.io)
    pre-match odds: 1X2 ("Match Winner") and the 2.5 line of Goals
    Over/Under (see parse_api_football_response for both).

    Requires an API key: pass api_key= explicitly, or set the
    API_FOOTBALL_KEY environment variable. Never hardcode a real key in
    source or commit it -- this class only reads it at runtime. Auth is
    a request header (x-apisports-key), not a URL query param the way
    the-odds-api.com's is.

    `fetch` is injectable (a callable taking a request URL and returning
    the raw response body as bytes) so tests can supply canned responses
    instead of making real network calls -- collect() calls it twice,
    once for /fixtures and once for /odds, both for the same date. It
    defaults to a real HTTP GET via urllib.

    date defaults to today's UTC date, recomputed on every collect()
    call, so each poll cycle naturally fetches that day's fixtures --
    pass an explicit date (YYYY-MM-DD) to pin it, mainly useful for
    tests.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        date: str | None = None,
        fetch: Callable[[str], bytes] | None = None,
    ) -> None:
        resolved_key = api_key or os.environ.get(API_KEY_ENV_VAR)
        if not resolved_key:
            raise ApiFootballError(
                f"No API key provided. Pass api_key= or set the "
                f"{API_KEY_ENV_VAR} environment variable."
            )

        self._api_key = resolved_key
        self._base_url = base_url.rstrip("/")
        self._date = date
        self._fetch = fetch or self._http_get

    @property
    def source(self) -> str:
        return "api-football"

    @property
    def provider_id(self) -> str:
        return "api-football"

    @property
    def parser_version(self) -> str:
        return "1"

    def collect(self) -> CollectionResult:
        date_str = self._date or datetime.now(UTC).date().isoformat()

        logger.info("api_football.request", extra={"date": date_str})

        fixtures_raw = self._fetch(f"{self._base_url}/fixtures?date={date_str}")
        odds_raw = self._fetch(f"{self._base_url}/odds?date={date_str}")

        observed_at = datetime.now(UTC)
        result = parse_api_football_response(fixtures_raw, odds_raw, observed_at)

        logger.info(
            "api_football.response",
            extra={"date": date_str, "raw_records_produced": len(result)},
        )

        # Both raw responses, not just the odds one -- a parser bug fixed
        # later needs the fixtures response too to reprocess (team names
        # aren't recoverable from the odds response alone).
        source_payload = json.dumps(
            {
                "fixtures": json.loads(fixtures_raw),
                "odds": json.loads(odds_raw),
            }
        )
        return CollectionResult(source_payload=source_payload, records=result)

    def _http_get(self, url: str) -> bytes:
        request = urllib.request.Request(
            url, headers={"x-apisports-key": self._api_key, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ApiFootballError(
                f"API-Football request failed with HTTP {exc.code}: {body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ApiFootballError(f"API-Football request failed: {exc.reason}") from exc
