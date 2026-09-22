import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from anomaly_detection_engine.collectors.manual_capture_collector import ManualCaptureCollector
from anomaly_detection_engine.models.market import (
    DEFAULT_MARKET,
    LIVE_MARKET,
    EventLifecycle,
    MarketIdentity,
)
from anomaly_detection_engine.models.raw_odds import RawEventOdds

FINAL_RESULT_GROUP_NAME = "Konačan ishod"
_SPORT_NAME_MAP = {"Fudbal": "football"}
# The exact status.name string Mozzart uses for a match that hasn't
# kicked off yet -- confirmed live against a real pre-match capture (see
# _resolve_market_and_lifecycle).
_NOT_STARTED_STATUS_NAME = "Nije počeo"


class MozzartResponseError(ValueError):
    """Raised when a capture doesn't look like a Mozzart response at all."""


def parse_mozzart_response(
    raw_text: str, observed_at: datetime, *, source_name: str = "Mozzart"
) -> list[RawEventOdds]:
    """Maps a Mozzart matches-shaped JSON response onto RawEventOdds.
    mozzartbet.com's live and pre-match listing endpoints return this
    exact same envelope shape -- LIVE_MARKET vs DEFAULT_MARKET (and
    EventLifecycle) is resolved per match from its own status.isLive/
    status.name fields (see _resolve_market_and_lifecycle), not assumed
    from which endpoint was captured. A single drop file can safely mix
    matches from both phases; each is tagged correctly on its own.

    Only the "Konačan ishod" (final result / 1X2) odds group is used;
    other markets (next goal, totals, ...) in the same response are
    ignored. Matches missing that group, with an incomplete or
    non-ACTIVE 1X2 line, with an unrecognized status, or outside the
    current football-only MVP scope are skipped.

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


def _resolve_market_and_lifecycle(
    match: dict[str, Any],
) -> tuple[MarketIdentity, EventLifecycle] | None:
    """Determines whether one match's odds are LIVE or PRE_MATCH, and the
    event's real-world lifecycle, from Mozzart's own status.isLive/
    status.name fields -- not from which endpoint the capture came from.
    betStatus is deliberately NOT used for this: it means "this market
    currently accepts bets" (true in both phases), not "the match has
    started" -- confirmed live against a real pre-match capture where
    every match had betStatus="STARTED" despite status.name="Nije počeo"
    ("not started") and no isLive/result/matchTime fields at all.

    Only the two statuses actually observed are mapped; anything else
    (e.g. a finished or interrupted match, status names not yet seen)
    returns None -- the caller skips that record rather than guessing,
    same "no silent default" policy applied everywhere else a provider's
    status gets mapped in this project (see
    api_football_collector._map_fixture_status).
    """
    status = match.get("status") or {}
    if status.get("isLive") is True:
        return LIVE_MARKET, EventLifecycle.LIVE
    if status.get("name") == _NOT_STARTED_STATUS_NAME:
        return DEFAULT_MARKET, EventLifecycle.SCHEDULED
    return None


def _map_match(
    match: dict[str, Any], observed_at: datetime, source_name: str
) -> RawEventOdds | None:
    sport = _SPORT_NAME_MAP.get(match.get("sport", {}).get("name", ""))
    if sport is None:
        return None

    resolved = _resolve_market_and_lifecycle(match)
    if resolved is None:
        return None
    market, lifecycle = resolved

    group = next(
        (g for g in match.get("oddsGroup", []) if g.get("groupName") == FINAL_RESULT_GROUP_NAME),
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
            start_time=datetime.fromtimestamp(match["startTime"] / 1000, tz=UTC),
            observed_at=observed_at,
            # market/lifecycle resolved per match above (see
            # _resolve_market_and_lifecycle) -- a pre-match price and a
            # live price for the same event are never simultaneously
            # valid (see models.market.MarketPhase), so getting this
            # right per record, not assumed for the whole file, is what
            # makes a single capture safe to mix both phases in.
            market=market,
            lifecycle=lifecycle,
            odds=odds,
            # mozzartbet.com's own stable match id.
            source_event_id=str(match["id"]),
        )
    except (KeyError, TypeError):
        return None


class MozzartFileCollector(ManualCaptureCollector):
    """Watches a fixed drop-file for a manually-captured Mozzart response --
    either the live or the pre-match listing endpoint, or a capture
    containing a mix of both (see parse_mozzart_response/
    _resolve_market_and_lifecycle for how each match's own status
    decides its phase; the two endpoints share this exact response
    shape).

    mozzartbet.com sits behind Cloudflare bot-management (cf_clearance /
    __cf_bm cookies observed on the captured request) -- an automated
    fetch here would mean scripting around that protection, which this
    project won't do. The capture step stays manual: in your own browser,
    open DevTools -> Network, find the matches request (e.g.
    /live/matches for live, or the equivalent pre-match listing), and
    save its response body to `capture_dir / filename` (default
    "live.json" -- the name is just a default, not a constraint on what
    the file may actually contain), overwriting the same file each time
    you capture a new reading. Point a second MozzartFileCollector
    instance at a different filename/capture_dir if you want live and
    pre-match captures kept in separate drop files instead of relying on
    per-match auto-detection in one.

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
            provider_id="mozzart",
            filename=filename,
            history_dirname=history_dirname,
        )
