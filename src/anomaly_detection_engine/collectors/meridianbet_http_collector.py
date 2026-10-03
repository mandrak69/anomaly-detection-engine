import html
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime

from anomaly_detection_engine.collectors.base import CollectionResult, OddsCollector
from anomaly_detection_engine.collectors.http_retry import http_get_with_retry
from anomaly_detection_engine.collectors.meridianbet_file_collector import (
    parse_meridianbet_response,
)
from anomaly_detection_engine.models.raw_odds import RawEventOdds

logger = logging.getLogger(__name__)

DEFAULT_SSR_PAGE_URL = "https://meridianbet.rs/sr/kladjenje/fudbal"
DEFAULT_API_BASE_URL = "https://online.meridianbet.rs/betshop/api/v1/offer/sport"
FOOTBALL_SPORT_ID = 58

# meridianbet.rs sits behind Cloudflare, which rejects urllib's default
# "Python-urllib/x.y" User-Agent outright (confirmed live: HTTP 403,
# Cloudflare error 1010 "banned ... based on your browser's signature") --
# not a challenge to solve, just a request identifying itself honestly as
# a browser the way any ordinary HTTP client reasonably does. A real
# browser's own request (same SSR page, same anonymous token, same
# listing endpoint -- see class docstring) succeeds with this header
# alone, no cookies/session/JS execution needed.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_NG_STATE_PATTERN = re.compile(
    r'<script id="ng-state" type="application/json">(.*?)</script>', re.DOTALL
)
# Generous but bounded, same "fail loud rather than loop forever" reasoning
# as api_football_collector._MAX_PAGES -- a real capture has run past page
# 77 (~1900 football events across the ALL time filter) with no sign of a
# bug, so this leaves plenty of headroom without being unbounded.
_MAX_PAGES = 200


class MeridianbetHttpError(RuntimeError):
    """Raised for any failure talking to meridianbet.rs (network, HTTP,
    auth), or for a response whose shape doesn't look like theirs."""


class MeridianbetHttpCollector(OddsCollector):
    """Automatic HTTP collector for meridianbet.rs's own pre-match football
    listing API -- no browser, no Playwright, no manual capture.

    meridianbet.rs's frontend is server-side-rendered (Angular Universal):
    its plain HTML response already embeds the full match/odds state for
    "today" in a `<script id="ng-state">` JSON blob, and that same blob
    carries a short-lived anonymous session token (`NEW_TOKEN.access_token`)
    that meridianbet.rs's own server issues to *any* visitor, logged in or
    not -- the identical token a real browser's first page load gets. This
    collector fetches that SSR page once per collect() call purely to
    obtain a fresh token (tokens are short-lived JWTs, so not cached across
    calls), then uses it as a Bearer token against the real backend listing
    endpoint (`.../betshop/api/v1/offer/sport/{sport_id}/leagues?page=N&
    time=ALL`) -- the same endpoint meridianbet.rs's own frontend calls
    internally, just driven directly instead of through a browser. No
    credential is forged or extracted from a logged-in session; this is
    the exact anonymous-visitor contract the site already offers.

    `time=ALL` (not meridianbet.rs's default "today" filter) is what
    actually makes this worth automating: a single page=0 request without
    it only covers the next ~24h, but paginating with `time=ALL` returned
    the full upcoming football schedule (confirmed live: 78 pages, 1901
    unique events) in one run -- wider coverage than any one manual HAR
    capture this project has taken so far.

    `Accept-Language` must also be sent on every API call -- its absence
    produces a 400 INVALID_LANGUAGE *before* any other validation runs
    (confirmed live), independent of and in addition to the auth token.

    Reuses parse_meridianbet_response unchanged: this endpoint's response
    shape (payload.leagues[].events[].header/positions) is identical to
    MeridianbetFileCollector's manually-captured drop file, so no new
    parsing logic exists here -- only acquisition.

    `fetch` is injectable (a callable taking a URL and a headers dict,
    returning the raw response body as bytes) so tests can supply canned
    responses instead of making real network calls -- collect() calls it
    once for the SSR token page, then once per result page of the listing
    endpoint. It defaults to a real HTTP GET via urllib.
    """

    def __init__(
        self,
        *,
        sport_id: int = FOOTBALL_SPORT_ID,
        ssr_page_url: str = DEFAULT_SSR_PAGE_URL,
        api_base_url: str = DEFAULT_API_BASE_URL,
        fetch: Callable[[str, dict[str, str]], bytes] | None = None,
    ) -> None:
        self._sport_id = sport_id
        self._ssr_page_url = ssr_page_url
        self._api_base_url = api_base_url.rstrip("/")
        self._fetch = fetch or self._http_get

    @property
    def source(self) -> str:
        return "meridianbet-http"

    @property
    def provider_id(self) -> str:
        # Same provider_id as MeridianbetFileCollector -- see
        # OddsCollector.provider_id's docstring: the two acquisition
        # methods for the same real-world provider must share one
        # FixtureCatalog team/competition mapping cache, not build two
        # redundant ones.
        return "meridianbet"

    @property
    def parser_version(self) -> str:
        return "1"

    def collect(self) -> CollectionResult:
        token = self._fetch_token()
        pages = self._fetch_all_pages(token)

        observed_at = datetime.now(UTC)
        result: list[RawEventOdds] = []
        for page_text in pages:
            result.extend(
                parse_meridianbet_response(page_text, observed_at, source_name="Meridianbet")
            )

        logger.info(
            "meridianbet_http.response",
            extra={"pages_fetched": len(pages), "raw_records_produced": len(result)},
        )

        # A JSON array of every page's own raw response text, not a
        # reshaped/merged envelope -- a faithful record of what was
        # actually received (see CollectionResult.source_payload), and
        # each element individually reprocessable through
        # parse_meridianbet_response unchanged, exactly as collect()
        # itself does above.
        source_payload = json.dumps([json.loads(p) for p in pages])
        return CollectionResult(source_payload=source_payload, records=result)

    def _fetch_token(self) -> str:
        """GETs the plain SSR listing page and extracts the anonymous
        session token meridianbet.rs's own server embeds in it for every
        visitor (see class docstring). Raises MeridianbetHttpError if the
        page doesn't have the expected ng-state script or token shape --
        same "fail loud on an unexpected shape" precedent every parser in
        this project follows, since a silently-empty token would otherwise
        surface as a confusing 400/401 from the listing endpoint instead
        of a clear error here.
        """
        body = self._fetch(
            self._ssr_page_url, {"Accept": "text/html", "User-Agent": _USER_AGENT}
        ).decode("utf-8", errors="replace")

        match = _NG_STATE_PATTERN.search(body)
        if match is None:
            raise MeridianbetHttpError(
                "meridianbet.rs's SSR page didn't contain the expected "
                "ng-state script -- the page shape may have changed."
            )

        try:
            state = json.loads(html.unescape(match.group(1)))
            raw_token = state["NEW_TOKEN"]
            token_obj = json.loads(raw_token) if isinstance(raw_token, str) else raw_token
            access_token = token_obj["access_token"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise MeridianbetHttpError(
                "meridianbet.rs's SSR page ng-state didn't contain the "
                "expected NEW_TOKEN.access_token -- the page shape may "
                "have changed."
            ) from exc

        if not isinstance(access_token, str) or not access_token:
            raise MeridianbetHttpError(
                "meridianbet.rs's SSR page returned an empty access token."
            )
        return access_token

    def _fetch_all_pages(self, token: str) -> list[str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            # Required -- its absence produces 400 INVALID_LANGUAGE before
            # any other validation on this endpoint (see class docstring).
            "Accept-Language": "sr",
            "User-Agent": _USER_AGENT,
        }

        pages: list[str] = []
        page = 0
        while True:
            if page > _MAX_PAGES:
                raise MeridianbetHttpError(
                    f"meridianbet.rs listing did not finish paginating within "
                    f"{_MAX_PAGES} pages -- aborting rather than fetching forever."
                )

            url = f"{self._api_base_url}/{self._sport_id}/leagues?page={page}&time=ALL"
            text = self._fetch(url, headers).decode("utf-8", errors="replace")

            data = json.loads(text)
            leagues = (data.get("payload") or {}).get("leagues") if isinstance(data, dict) else None
            if not leagues:
                break

            pages.append(text)
            page += 1

        if not pages:
            raise MeridianbetHttpError(
                "meridianbet.rs listing returned zero pages for sport_id="
                f"{self._sport_id} -- expected at least page 0 to have events."
            )

        logger.info("meridianbet_http.paginated", extra={"pages_fetched": len(pages)})
        return pages

    @staticmethod
    def _http_get(url: str, headers: dict[str, str]) -> bytes:
        return http_get_with_retry(
            url,
            headers=headers,
            timeout=10,
            error_cls=MeridianbetHttpError,
            provider_label="Meridianbet",
        )
