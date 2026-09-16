import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from anomaly_detection_engine.collectors.manual_capture_collector import ManualCaptureCollector
from anomaly_detection_engine.models.market import DEFAULT_MARKET, TOTALS_2_5_MARKET
from anomaly_detection_engine.models.raw_odds import RawEventOdds

FINAL_RESULT_GROUP_NAME = "Konačan Ishod"
TOTALS_GROUP_NAME = "Ukupno golova"
_SPORT_NAME_MAP = {"Fudbal": "football"}
_TOTALS_OUTCOME_MAP = {"Manje": "UNDER", "Više": "OVER"}
_TOTALS_LINE = Decimal("2.5")


class MeridianbetResponseError(ValueError):
    """Raised when a capture doesn't look like a Meridianbet pre-match
    listing response at all."""


def parse_meridianbet_response(
    raw_text: str, observed_at: datetime, *, source_name: str = "Meridianbet"
) -> list[RawEventOdds]:
    """Maps a meridianbet.com pre-match listing response
    (payload.leagues[].events[]) onto RawEventOdds.

    Only two markets are extracted -- "Konačan Ishod" (1X2, DEFAULT_MARKET)
    and "Ukupno golova" at exactly the 2.5 line (TOTALS_2_5_MARKET); no
    handicap-shaped market has been observed in this response shape at
    all (unlike api-football's odds response), so none is extracted here
    -- same "only extract what's actually reliably present" precedent
    already set by MozzartFileCollector only extracting 1X2.

    Raises MeridianbetResponseError if the top-level shape doesn't even
    look like this response (no "payload"/"leagues") -- e.g. the wrong
    request was captured (meridianbet.com's own frontend also exposes a
    "market column config" endpoint shaped very differently, with no
    odds/team data in it at all, that has been captured by mistake here
    before). Same "better to stop the run than ingest nothing while
    believing everything is fine" reasoning as MozzartResponseError.
    """
    data = json.loads(raw_text, parse_float=Decimal)

    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(payload, dict) or "leagues" not in payload:
        raise MeridianbetResponseError(
            "This capture doesn't look like a Meridianbet pre-match listing "
            "response (expected an object with payload.leagues) -- check "
            "that the right request was saved to this collector's drop file."
        )

    result: list[RawEventOdds] = []
    for league in payload["leagues"]:
        for event in league.get("events", []):
            result.extend(_map_event(event, observed_at, source_name))
    return result


def _map_event(
    event: dict[str, Any], observed_at: datetime, source_name: str
) -> list[RawEventOdds]:
    header = event.get("header", {})
    sport = _SPORT_NAME_MAP.get(header.get("sport", {}).get("name", ""))
    if sport is None:
        return []

    rivals = header.get("rivals", [])
    if len(rivals) != 2:
        return []

    try:
        common = {
            "source": source_name,
            "sport": sport,
            "league": header["league"]["name"],
            "home_team": rivals[0],
            "away_team": rivals[1],
            "start_time": datetime.fromtimestamp(header["startTime"] / 1000, tz=UTC),
            "observed_at": observed_at,
            # meridianbet.com's own stable event id -- see
            # RawEventOdds.source_event_id.
            "source_event_id": str(header["eventId"]),
        }
    except (KeyError, TypeError):
        return []

    records: list[RawEventOdds] = []

    match_winner_odds = _extract_match_winner_odds(event)
    if match_winner_odds is not None:
        records.append(RawEventOdds(**common, market=DEFAULT_MARKET, odds=match_winner_odds))

    totals_odds = _extract_totals_2_5_odds(event)
    if totals_odds is not None:
        records.append(RawEventOdds(**common, market=TOTALS_2_5_MARKET, odds=totals_odds))

    return records


def _find_group(event: dict[str, Any], *, group_name: str) -> dict[str, Any] | None:
    for position in event.get("positions", []):
        group: dict[str, Any]
        for group in position.get("groups", []):
            if group.get("name") == group_name:
                return group
    return None


def _active_selection_prices(group: dict[str, Any]) -> dict[str, Decimal]:
    prices: dict[str, Decimal] = {}
    for selection in group.get("selections", []):
        if selection.get("placeholder") or selection.get("state") != "ACTIVE":
            continue
        name = selection.get("name")
        price = selection.get("price")
        if name is not None and price is not None:
            prices[name] = Decimal(price)
    return prices


def _extract_match_winner_odds(event: dict[str, Any]) -> dict[str, Decimal] | None:
    group = _find_group(event, group_name=FINAL_RESULT_GROUP_NAME)
    if group is None:
        return None

    prices = _active_selection_prices(group)
    odds = {code: prices[code] for code in ("1", "X", "2") if code in prices}

    # A bookmaker publishing an incomplete 1X2 line isn't usable for this
    # market -- same "all-or-nothing" rule api_football_collector's own
    # _extract_match_winner_odds already applies.
    if set(odds) != {"1", "X", "2"}:
        return None
    return odds


def _extract_totals_2_5_odds(event: dict[str, Any]) -> dict[str, Decimal] | None:
    for position in event.get("positions", []):
        for group in position.get("groups", []):
            if group.get("name") != TOTALS_GROUP_NAME:
                continue
            over_under = group.get("overUnder")
            if over_under is None or Decimal(over_under) != _TOTALS_LINE:
                continue

            prices = _active_selection_prices(group)
            odds: dict[str, Decimal] = {}
            for raw_name, code in _TOTALS_OUTCOME_MAP.items():
                if raw_name in prices:
                    odds[code] = prices[raw_name]

            if set(odds) != {"OVER", "UNDER"}:
                return None
            return odds
    return None


class MeridianbetFileCollector(ManualCaptureCollector):
    """Watches a fixed drop-file for a manually-captured Meridianbet
    pre-match listing response.

    Manual capture, the same acquisition mode MozzartFileCollector uses:
    in your own browser, open DevTools -> Network, find the pre-match
    listing request for football (its response has a top-level
    payload.leagues shape -- not the "market column config" request,
    which is shaped very differently and has no odds in it at all), and
    save its response body to `capture_dir / filename` (default
    "meridianbet.json"), overwriting the same file each time you capture
    a new reading.

    A thin ManualCaptureCollector wrapper around
    parse_meridianbet_response -- see manual_capture_collector.py for the
    acquisition/archive mechanics this reuses.
    """

    def __init__(
        self,
        capture_dir: Path,
        *,
        filename: str = "meridianbet.json",
        history_dirname: str = "history",
        source_name: str = "Meridianbet",
    ) -> None:
        super().__init__(
            capture_dir,
            parse=lambda raw_text, observed_at: parse_meridianbet_response(
                raw_text, observed_at, source_name=source_name
            ),
            source_label=f"meridianbet-file:{capture_dir.name}",
            provider_id="meridianbet",
            filename=filename,
            history_dirname=history_dirname,
        )
