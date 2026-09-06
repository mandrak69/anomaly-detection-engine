import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from anomaly_detection_engine.collectors.manual_capture_collector import ManualCaptureCollector
from anomaly_detection_engine.models.market import DEFAULT_MARKET
from anomaly_detection_engine.models.raw_odds import RawEventOdds

FINAL_RESULT_GROUP_NAME = "Konačan ishod"
_SPORT_NAME_MAP = {"Fudbal": "football"}


class MozzartResponseError(ValueError):
    """Raised when a capture doesn't look like a Mozzart response at all."""


def parse_mozzart_response(
    raw_text: str, observed_at: datetime, *, source_name: str = "Mozzart"
) -> list[RawEventOdds]:
    """Maps a Mozzart /live/matches-shaped JSON response onto RawEventOdds.

    Only the "Konačan ishod" (final result / 1X2) odds group is used;
    other markets (next goal, totals, ...) in the same response are
    ignored. Matches missing that group, with an incomplete or
    non-ACTIVE 1X2 line, or outside the current football-only MVP scope
    are skipped.

    Raises MozzartResponseError if the top-level shape isn't even a
    Mozzart response (no "items" key) -- e.g. the wrong bookmaker's
    capture landed in this collector's drop directory by mistake.
    Deliberately does NOT fall back to "treat it as zero matches this
    cycle": that would look identical to a legitimate quiet moment (no
    live matches right now) in the logs/CollectorRun, silently hiding
    that the wrong file was captured. Better to stop the run than
    ingest nothing while believing everything is fine.
    """
    data = json.loads(raw_text, parse_float=Decimal)

    if not isinstance(data, dict) or "items" not in data:
        raise MozzartResponseError(
            "This capture doesn't look like a Mozzart response (expected a "
            "JSON object with an 'items' key) -- check that the right "
            "response was saved to this collector's drop file."
        )

    matches = data["items"]

    return [
        raw
        for raw in (_map_match(match, observed_at, source_name) for match in matches)
        if raw is not None
    ]


def _map_match(match: dict, observed_at: datetime, source_name: str) -> RawEventOdds | None:
    sport = _SPORT_NAME_MAP.get(match.get("sport", {}).get("name", ""))
    if sport is None:
        return None

    group = next(
        (
            g
            for g in match.get("oddsGroup", [])
            if g.get("groupName") == FINAL_RESULT_GROUP_NAME
        ),
        None,
    )
    if group is None:
        return None

    odds: dict[str, Decimal] = {}
    for odd in group.get("odds", []):
        if odd.get("oddStatus") != "ACTIVE":
            continue
        code = odd.get("subgame", {}).get("shortName")
        if code in ("1", "X", "2") and "value" in odd:
            odds[code] = Decimal(odd["value"])

    if set(odds) != {"1", "X", "2"}:
        return None

    try:
        return RawEventOdds(
            source=source_name,
            sport=sport,
            league=match["competition"]["name"],
            home_team=match["home"]["name"],
            away_team=match["visitor"]["name"],
            start_time=datetime.fromtimestamp(match["startTime"] / 1000, tz=timezone.utc),
            observed_at=observed_at,
            market=DEFAULT_MARKET,
            odds=odds,
        )
    except (KeyError, TypeError):
        return None


class MozzartFileCollector(ManualCaptureCollector):
    """Watches a fixed drop-file for a manually-captured Mozzart response.

    mozzartbet.com sits behind Cloudflare bot-management (cf_clearance /
    __cf_bm cookies observed on the captured request) -- an automated
    fetch here would mean scripting around that protection, which this
    project won't do. The capture step stays manual: in your own browser,
    open DevTools -> Network, find the matches request (e.g.
    /live/matches), and save its response body to `capture_dir /
    filename` (default "live.json"), overwriting the same file each time
    you capture a new reading.

    A thin ManualCaptureCollector wrapper around parse_mozzart_response --
    see manual_capture_collector.py for the acquisition/archive mechanics
    this reuses.
    """

    def __init__(
        self,
        capture_dir: Path,
        *,
        filename: str = "live.json",
        history_dirname: str = "history",
        source_name: str = "Mozzart",
    ) -> None:
        super().__init__(
            capture_dir,
            parse=lambda raw_text, observed_at: parse_mozzart_response(
                raw_text, observed_at, source_name=source_name
            ),
            source_label=f"mozzart-file:{capture_dir.name}",
            filename=filename,
            history_dirname=history_dirname,
        )
