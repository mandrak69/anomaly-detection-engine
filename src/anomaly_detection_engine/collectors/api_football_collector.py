import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from anomaly_detection_engine.collectors.base import CollectionResult, OddsCollector
from anomaly_detection_engine.models.market import (
    DEFAULT_MARKET,
    HANDICAP_MINUS_1_MARKET,
    TOTALS_2_5_MARKET,
)
from anomaly_detection_engine.models.raw_odds import RawEventOdds

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://v3.football.api-sports.io"
API_KEY_ENV_VAR = "API_FOOTBALL_KEY"
_MATCH_WINNER_BET_NAME = "Match Winner"
_OUTCOME_CODES = {"Home": "1", "Draw": "X", "Away": "2"}
_OVER_UNDER_BET_NAME = "Goals Over/Under"
_TOTALS_2_5_LINE = "2.5"
_HANDICAP_RESULT_BET_NAME = "Handicap Result"
_HANDICAP_MINUS_1_LINE = "-1"
# Generous but bounded -- a real day's fixtures should never come close
# to this many pages. Exists so a bug (an API that never reports
# current catching up to total) turns into a loud, immediate
# ApiFootballError instead of an infinite fetch loop inside an
# unattended, long-running poller (see poller.py).
_MAX_PAGES = 50


class ApiFootballError(RuntimeError):
    """Raised for any failure talking to api-football.com (network, HTTP,
    auth), or for a response whose top-level shape doesn't even look like
    one of theirs."""


def parse_api_football_response(
    fixtures_raw: str | bytes, odds_raw: str | bytes, observed_at: datetime
) -> list[RawEventOdds]:
    """Maps one day's api-football.com /fixtures + /odds responses onto
    RawEventOdds -- up to three records per bookmaker per fixture: the
    "Match Winner" (1X2, DEFAULT_MARKET) bet, the "Goals Over/Under"
    bet's 2.5 line (TOTALS_2_5_MARKET), and the "Handicap Result" bet's
    -1 line (HANDICAP_MINUS_1_MARKET) -- each only produced if that
    bookmaker actually has a complete line for it.

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

    Takes one already-complete response per endpoint -- collect() is
    responsible for fetching every page of a paginated day and merging
    them into one such response first (see _fetch_all_pages), so this
    function never needs to know pagination happened at all.
    """
    fixtures_data = _load_envelope(fixtures_raw, what="fixtures")
    odds_data = _load_envelope(odds_raw, what="odds")
    return _parse_envelopes(fixtures_data, odds_data, observed_at)


def _parse_envelopes(
    fixtures_data: dict, odds_data: dict, observed_at: datetime
) -> list[RawEventOdds]:
    """The actual per-fixture/per-bookmaker extraction behind
    parse_api_football_response, operating on already-loaded (and, for
    collect()'s own use, already page-merged) envelope dicts rather than
    raw JSON text -- split out so collect() can hand it merged,
    multi-page envelopes directly instead of re-serializing them back to
    JSON text first (which would also have to smuggle _load_envelope's
    Decimal-parsed odds back through a plain json.dumps, which cannot
    serialize Decimal at all).
    """
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

        # Per-fixture (not per-bookmaker -- api-football.com only reports
        # one "update" timestamp for the whole odds entry, unlike
        # the-odds-api's per-bookmaker last_update), so every bookmaker
        # under this fixture shares the same source_timestamp. Same
        # freshness reasoning as the-odds-api's last_update -> see
        # RawEventOdds.quote_time and OddsSnapshot.quote_time: without
        # this, source_timestamp stays None and quote_time silently falls
        # back to observed_at (when *we* polled), hiding how stale the
        # underlying quote actually was.
        raw_update = item.get("update")
        source_timestamp = (
            datetime.fromisoformat(raw_update) if raw_update else None
        )

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
                "source_timestamp": source_timestamp,
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

            handicap_odds = _extract_handicap_minus_1_odds(bookmaker)
            if handicap_odds is not None:
                result.append(
                    RawEventOdds(**common, market=HANDICAP_MINUS_1_MARKET, odds=handicap_odds)
                )

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


def _fetch_all_pages(
    fetch: Callable[[str], bytes], url_base: str, *, what: str
) -> tuple[dict, dict]:
    """Fetches every page of one api-football.com list endpoint
    (paging.current/paging.total), merging each page's "response" array
    into one envelope dict shaped like a single, complete response --
    every caller (parse_api_football_response's business logic,
    collect()'s source_payload) sees one complete envelope and never
    needs to know more than one HTTP call was involved.

    Returns (decimal_envelope, plain_envelope): the same bytes parsed
    twice, once with parse_float=Decimal (via _load_envelope, for exact
    odds precision feeding the actual extraction) and once as plain
    JSON (for source_payload -- a faithful record of what was actually
    received, not run back through the Decimal-parsed structure, which
    plain json.dumps cannot serialize at all).

    url_base must not already include a `page` query param; `&page=N`
    is appended for every page after the first (page 1 is requested
    exactly as api-football.com's own default, with no page param at
    all). Raises ApiFootballError (via the same _MAX_PAGES guard as
    everything else that must fail loud rather than loop silently) if a
    response never reports paging.current catching up to paging.total
    within a sane number of pages -- protects an unattended,
    long-running poller (see poller.py) from an infinite fetch loop
    should the API ever misbehave.
    """
    page = 1
    merged_decimal: dict | None = None
    merged_plain: dict | None = None

    while True:
        if page > _MAX_PAGES:
            raise ApiFootballError(
                f"api-football.com {what} response did not finish paginating "
                f"within {_MAX_PAGES} pages -- aborting rather than fetching "
                f"forever."
            )

        url = url_base if page == 1 else f"{url_base}&page={page}"
        raw = fetch(url)
        decimal_data = _load_envelope(raw, what=what)
        plain_data = json.loads(raw)

        if merged_decimal is None:
            merged_decimal, merged_plain = decimal_data, plain_data
        else:
            merged_decimal["response"].extend(decimal_data["response"])
            merged_plain["response"].extend(plain_data["response"])

        paging = decimal_data.get("paging") or {}
        total = paging.get("total") or 1
        current = paging.get("current") or 1
        if current >= total:
            break
        page += 1

    if page > 1:
        logger.info("api_football.paginated", extra={"what": what, "pages_fetched": page})

    return merged_decimal, merged_plain


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


def _extract_handicap_minus_1_odds(bookmaker: dict) -> dict[str, Decimal] | None:
    """api-football.com's "Handicap Result" bet bundles many handicap
    lines ("Home -1"/"Draw -1"/"Away -1", "Home -2"/..., "Home +1"/...)
    into one values list -- this project only extracts the -1 line (see
    models.market.HANDICAP_MINUS_1_MARKET), the 3-way flavor where
    Home/Draw/Away are all still possible (unlike 2-way Asian Handicap,
    deliberately not modeled here -- see that constant's own docstring).
    Reuses _OUTCOME_CODES: "Home -1" maps the same way "Home" does for
    Match Winner, just filtered to this one specific line first.
    """
    handicap_result = next(
        (bet for bet in bookmaker.get("bets", []) if bet.get("name") == _HANDICAP_RESULT_BET_NAME),
        None,
    )
    if handicap_result is None:
        return None

    odds: dict[str, Decimal] = {}
    for value in handicap_result.get("values", []):
        odd = value.get("odd")
        raw_value = value.get("value")
        if odd is None or not isinstance(raw_value, str):
            continue

        direction, _, line = raw_value.partition(" ")
        if line != _HANDICAP_MINUS_1_LINE:
            continue

        code = _OUTCOME_CODES.get(direction)
        if code is not None:
            odds[code] = Decimal(odd)

    if set(odds) != {"1", "X", "2"}:
        return None

    return odds


class ApiFootballCollector(OddsCollector):
    """Collector for https://www.api-football.com (api-sports.io)
    pre-match odds: 1X2 ("Match Winner"), the 2.5 line of Goals
    Over/Under, and the -1 line of Handicap Result (see
    parse_api_football_response for all three).

    Requires an API key: pass api_key= explicitly, or set the
    API_FOOTBALL_KEY environment variable. Never hardcode a real key in
    source or commit it -- this class only reads it at runtime. Auth is
    a request header (x-apisports-key), not a URL query param the way
    the-odds-api.com's is.

    `fetch` is injectable (a callable taking a request URL and returning
    the raw response body as bytes) so tests can supply canned responses
    instead of making real network calls -- collect() calls it at least
    twice, once each for /fixtures and /odds for the same date, and once
    more per additional page if either response is paginated (see
    _fetch_all_pages: every page is fetched and merged before parsing,
    not just page 1). It defaults to a real HTTP GET via urllib.

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

        fixtures_decimal, fixtures_plain = _fetch_all_pages(
            self._fetch, f"{self._base_url}/fixtures?date={date_str}", what="fixtures"
        )
        odds_decimal, odds_plain = _fetch_all_pages(
            self._fetch, f"{self._base_url}/odds?date={date_str}", what="odds"
        )

        observed_at = datetime.now(UTC)
        result = _parse_envelopes(fixtures_decimal, odds_decimal, observed_at)

        logger.info(
            "api_football.response",
            extra={"date": date_str, "raw_records_produced": len(result)},
        )

        # Both merged responses, not just the odds one -- a parser bug
        # fixed later needs the fixtures response too to reprocess (team
        # names aren't recoverable from the odds response alone). Built
        # from the plain (non-Decimal) parse of every page already
        # fetched above -- not re-fetched, and not the Decimal-parsed
        # dicts, which plain json.dumps cannot serialize.
        source_payload = json.dumps({"fixtures": fixtures_plain, "odds": odds_plain})
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
