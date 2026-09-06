import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from anomaly_detection_engine.collectors.base import OddsCollector
from anomaly_detection_engine.collectors.json_collector import DEFAULT_MARKET
from anomaly_detection_engine.collectors.manual_capture_collector import ManualCaptureCollector
from anomaly_detection_engine.models.raw_odds import RawEventOdds

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.the-odds-api.com/v4"
API_KEY_ENV_VAR = "ODDS_API_KEY"
_DRAW_OUTCOME_NAMES = {"draw", "tie"}


class TheOddsApiError(RuntimeError):
    """Raised for any failure talking to the-odds-api.com (network, HTTP,
    auth), or for a response/capture whose top-level shape doesn't even
    look like one of their responses."""


def parse_the_odds_api_response(
    raw: str | bytes, observed_at: datetime, *, league_fallback: str = ""
) -> list[RawEventOdds]:
    """Maps a the-odds-api.com /v4/sports/{sport}/odds response (h2h,
    decimal odds) onto RawEventOdds, one per bookmaker per event.

    Shared by TheOddsApiCollector (auto, fetched over HTTP) and
    TheOddsApiManualCollector (a manually-captured copy of the identical
    response shape) so both go through exactly the same mapping.
    league_fallback is used when an event has no sport_title field.

    Raises TheOddsApiError if the top-level shape isn't even a
    the-odds-api.com response (their /odds endpoint always returns a
    JSON array; a dict here is most likely their own error body, e.g.
    {"message": "Invalid API key"}, or the wrong capture in the wrong
    place). Deliberately does not fall back to "treat it as zero events
    this cycle": that would look identical to a legitimate quiet moment
    in the logs/CollectorRun, silently hiding that something was wrong
    with the response itself.
    """
    events = json.loads(raw, parse_float=Decimal)

    if not isinstance(events, list):
        raise TheOddsApiError(
            "This response/capture doesn't look like a the-odds-api.com "
            "odds response (expected a JSON array of events) -- check the "
            "API key/response for an error body, or that the right capture "
            "was saved to this collector's drop file."
        )

    result: list[RawEventOdds] = []

    for event in events:
        home_team = event["home_team"]
        away_team = event["away_team"]
        start_time = datetime.fromisoformat(event["commence_time"])
        league = event.get("sport_title", league_fallback)

        for bookmaker in event.get("bookmakers", []):
            odds = _extract_1x2_odds(bookmaker, home_team, away_team)
            if odds is None:
                continue

            last_update = bookmaker.get("last_update")
            source_timestamp = (
                datetime.fromisoformat(last_update) if last_update else None
            )

            result.append(
                RawEventOdds(
                    source=bookmaker.get("title") or bookmaker.get("key", "unknown"),
                    sport="football",
                    league=league,
                    home_team=home_team,
                    away_team=away_team,
                    start_time=start_time,
                    observed_at=observed_at,
                    market=DEFAULT_MARKET,
                    odds=odds,
                    source_timestamp=source_timestamp,
                )
            )

    return result


def _extract_1x2_odds(
    bookmaker: dict, home_team: str, away_team: str
) -> dict[str, Decimal] | None:
    h2h_market = next(
        (m for m in bookmaker.get("markets", []) if m.get("key") == "h2h"),
        None,
    )
    if h2h_market is None:
        return None

    odds: dict[str, Decimal] = {}
    for outcome in h2h_market.get("outcomes", []):
        name = outcome.get("name", "")
        price = outcome.get("price")
        if price is None:
            continue

        if name == home_team:
            odds["1"] = Decimal(price)
        elif name == away_team:
            odds["2"] = Decimal(price)
        elif name.strip().lower() in _DRAW_OUTCOME_NAMES:
            odds["X"] = Decimal(price)

    # A bookmaker publishing an incomplete 1X2 line (e.g. draw missing)
    # isn't usable for this market's analysis; skip it rather than
    # producing a RawEventOdds with a hole in its odds dict.
    if set(odds) != {"1", "X", "2"}:
        return None

    return odds


class TheOddsApiCollector(OddsCollector):
    """Collector for https://the-odds-api.com h2h (1X2) football markets.

    Requires an API key: pass api_key= explicitly, or set the
    ODDS_API_KEY environment variable. Never hardcode a real key in
    source or commit it -- this class only reads it at runtime.

    `fetch` is injectable (a callable taking the request URL and
    returning the raw response body as bytes) so tests can supply a
    canned response instead of making a real network call. It defaults
    to a real HTTP GET via urllib.

    For when the API itself is temporarily unavailable (rate limit,
    outage, exhausted quota) but a response body can still be obtained
    by hand, see TheOddsApiManualCollector below -- same response
    schema, same parsing, different acquisition.
    """

    def __init__(
        self,
        sport_key: str,
        api_key: str | None = None,
        regions: str = "eu",
        base_url: str = DEFAULT_BASE_URL,
        fetch: Callable[[str], bytes] | None = None,
    ) -> None:
        resolved_key = api_key or os.environ.get(API_KEY_ENV_VAR)
        if not resolved_key:
            raise TheOddsApiError(
                f"No API key provided. Pass api_key= or set the "
                f"{API_KEY_ENV_VAR} environment variable."
            )

        self._sport_key = sport_key
        self._api_key = resolved_key
        self._regions = regions
        self._base_url = base_url.rstrip("/")
        self._fetch = fetch or self._http_get

    @property
    def source(self) -> str:
        return f"the-odds-api:{self._sport_key}"

    def collect(self) -> list[RawEventOdds]:
        url = (
            f"{self._base_url}/sports/{self._sport_key}/odds/"
            f"?apiKey={self._api_key}&regions={self._regions}"
            f"&markets=h2h&oddsFormat=decimal&dateFormat=iso"
        )
        logger.info(
            "the_odds_api.request",
            extra={"sport_key": self._sport_key, "regions": self._regions},
        )

        raw = self._fetch(url)
        observed_at = datetime.now(timezone.utc)
        result = parse_the_odds_api_response(
            raw, observed_at, league_fallback=self._sport_key
        )

        logger.info(
            "the_odds_api.response",
            extra={"sport_key": self._sport_key, "raw_records_produced": len(result)},
        )

        return result

    @staticmethod
    def _http_get(url: str) -> bytes:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise TheOddsApiError(
                f"The Odds API request failed with HTTP {exc.code}: {body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise TheOddsApiError(f"The Odds API request failed: {exc.reason}") from exc


class TheOddsApiManualCollector(ManualCaptureCollector):
    """Manual-capture fallback for TheOddsApiCollector's exact response shape.

    For when the API itself can't be reached automatically right now
    (rate limit, outage, exhausted quota) but you can still obtain one
    response body by hand and want it ingested through the identical
    parsing path as the live collector -- same 1X2 mapping, same
    RawEventOdds output, just acquired differently. Save the response
    body to `capture_dir/filename` (default "capture.json"); the rest
    (archiving, no-file-yet handling, parse-failure handling) is the
    same drop-file mechanism MozzartFileCollector uses.
    """

    def __init__(
        self,
        capture_dir: Path,
        *,
        sport_key: str,
        filename: str = "capture.json",
        history_dirname: str = "history",
    ) -> None:
        super().__init__(
            capture_dir,
            parse=lambda raw_text, observed_at: parse_the_odds_api_response(
                raw_text, observed_at, league_fallback=sport_key
            ),
            source_label=f"the-odds-api-manual:{sport_key}",
            filename=filename,
            history_dirname=history_dirname,
        )
