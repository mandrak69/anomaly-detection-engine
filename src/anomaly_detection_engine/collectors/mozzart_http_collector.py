import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from anomaly_detection_engine.collectors.base import CollectionResult, OddsCollector
from anomaly_detection_engine.collectors.http_retry import http_get_with_retry
from anomaly_detection_engine.collectors.mozzart_file_collector import parse_mozzart_response
from anomaly_detection_engine.models.raw_odds import RawEventOdds

logger = logging.getLogger(__name__)

DEFAULT_MATCHES_URL = "https://www.mozzartbet.com/betting/matches"
FOOTBALL_SPORT_ID = 1
DEFAULT_PAGE_SIZE = 15

# meridianbet.rs's own anonymous-visitor endpoint (see
# meridianbet_http_collector.py) turned out to only need a realistic
# User-Agent; mozzartbet.com's /betting/matches turned out to need one
# specific thing more -- a `medium` request header identifying the
# calling client (its own frontend sends "PREMATCH_WEB" on this exact
# listing page; confirmed live by inspecting the real page's own
# outgoing XHR via a patched XMLHttpRequest, not guessed). Without it,
# the endpoint returns its own 403 ({"status":"ERROR","message":
# "Pogresan medium","mediumFromHeader":"DEFAULT"}) -- an application-
# level check, not Cloudflare: the same plain request with this one
# header added succeeds cleanly, with no cookies, no session, no token,
# no browser involved at all. See MozzartFileCollector's own docstring
# for why mozzartbet.com was believed to need bot-detection bypassing
# entirely -- that belief predates this finding and no longer holds for
# this specific endpoint.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_MEDIUM_HEADER_VALUE = "PREMATCH_WEB"

# Generous but bounded, same "fail loud rather than loop forever"
# reasoning as api_football_collector._MAX_PAGES -- a real run has gone
# past page 82 (~1200+ football matches for date=all_days) with no sign
# of a bug.
_MAX_PAGES = 300


class MozzartHttpError(RuntimeError):
    """Raised for any failure talking to mozzartbet.com (network, HTTP),
    or for a response whose shape doesn't look like theirs."""


class MozzartHttpCollector(OddsCollector):
    """Automatic HTTP collector for mozzartbet.com's own pre-match listing
    API (`POST /betting/matches`) -- no browser, no manual capture.

    Paginates `currentPage=0, 1, 2, ...` (mozzartbet.com's own frontend
    starts at 0, confirmed live -- page 0 and page 1 both return real,
    distinct matches, not empty/placeholder pages) until an empty
    `items` page, merging every page's records. `date="all_days"` is
    what makes this worth automating over a single day's listing --
    confirmed live: 83 pages, 1219 unique matches in one run, close to
    3x the largest manual HAR capture this project had taken by hand
    (451 matches, see docs/manual-capture-sources.md).

    Reuses parse_mozzart_response unchanged: this endpoint's response
    shape (a JSON object with an "items" key, each with oddsGroup[]) is
    identical to MozzartFileCollector's manually-captured drop file, so
    no new parsing logic exists here -- only acquisition.

    `fetch` is injectable (a callable taking the request URL and the
    JSON request body as a str, returning the raw response body as
    bytes) so tests can supply canned responses instead of making real
    network calls -- headers are fixed and not parameterized here, same
    as every other collector's own default _http_get/_http_post. It
    defaults to a real HTTP POST via urllib.
    """

    def __init__(
        self,
        *,
        sport_id: int = FOOTBALL_SPORT_ID,
        date: str = "all_days",
        page_size: int = DEFAULT_PAGE_SIZE,
        matches_url: str = DEFAULT_MATCHES_URL,
        fetch: Callable[[str, str], bytes] | None = None,
    ) -> None:
        self._sport_id = sport_id
        self._date = date
        self._page_size = page_size
        self._matches_url = matches_url
        self._fetch = fetch or self._http_post

    @property
    def source(self) -> str:
        return "mozzart-http"

    @property
    def provider_id(self) -> str:
        # Same provider_id as MozzartFileCollector -- see
        # OddsCollector.provider_id's docstring: the two acquisition
        # methods for the same real-world provider must share one
        # FixtureCatalog team/competition mapping cache, not build two
        # redundant ones.
        return "mozzart"

    @property
    def parser_version(self) -> str:
        return "1"

    def collect(self) -> CollectionResult:
        pages = self._fetch_all_pages()

        observed_at = datetime.now(UTC)
        result: list[RawEventOdds] = []
        for page_text in pages:
            result.extend(
                parse_mozzart_response(page_text, observed_at, source_name="Mozzart")
            )

        logger.info(
            "mozzart_http.response",
            extra={"pages_fetched": len(pages), "raw_records_produced": len(result)},
        )

        # A JSON array of every page's own raw response text -- a
        # faithful record of what was actually received, each element
        # individually reprocessable through parse_mozzart_response
        # unchanged, exactly as collect() itself does above.
        source_payload = json.dumps([json.loads(p) for p in pages])
        return CollectionResult(source_payload=source_payload, records=result)

    def _fetch_all_pages(self) -> list[str]:
        pages: list[str] = []
        page = 0
        while True:
            if page > _MAX_PAGES:
                raise MozzartHttpError(
                    f"mozzartbet.com listing did not finish paginating within "
                    f"{_MAX_PAGES} pages -- aborting rather than fetching forever."
                )

            body = json.dumps(
                {
                    "date": self._date,
                    "sort": "bycompetition",
                    "currentPage": page,
                    "pageSize": self._page_size,
                    "sportId": self._sport_id,
                    "competitionIds": [],
                    "search": "",
                    "matchTypeId": 0,
                }
            )
            text = self._fetch(self._matches_url, body).decode("utf-8", errors="replace")

            data = json.loads(text)
            items = data.get("items") if isinstance(data, dict) else None
            if not items:
                break

            pages.append(text)
            page += 1

        if not pages:
            raise MozzartHttpError(
                "mozzartbet.com listing returned zero pages for sport_id="
                f"{self._sport_id} date={self._date!r} -- expected at least "
                "page 0 to have matches."
            )

        logger.info("mozzart_http.paginated", extra={"pages_fetched": len(pages)})
        return pages

    @staticmethod
    def _http_post(url: str, body: str) -> bytes:
        return http_get_with_retry(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "medium": _MEDIUM_HEADER_VALUE,
            },
            timeout=10,
            error_cls=MozzartHttpError,
            provider_label="Mozzart",
            data=body.encode("utf-8"),
        )
