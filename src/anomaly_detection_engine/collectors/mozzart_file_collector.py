import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from anomaly_detection_engine.collectors.base import OddsCollector
from anomaly_detection_engine.collectors.json_collector import DEFAULT_MARKET
from anomaly_detection_engine.models.raw_odds import RawEventOdds

logger = logging.getLogger(__name__)

FINAL_RESULT_GROUP_NAME = "Konačan ishod"
_SPORT_NAME_MAP = {"Fudbal": "football"}


class MozzartFileCollector(OddsCollector):
    """Reads a manually-captured Mozzart response from disk; fetches nothing itself.

    mozzartbet.com sits behind Cloudflare bot-management (cf_clearance /
    __cf_bm cookies observed on the captured request) -- an automated
    fetch here would mean scripting around that protection, which this
    project won't do. The capture step stays manual: in your own browser,
    open DevTools -> Network, find the matches request (e.g.
    /live/matches), save its response body as a .json file into
    `directory`. Each collect() call picks whichever file in that
    directory has the newest modification time and treats it as the
    current snapshot -- drop a new capture in periodically and repeated
    app runs will naturally build up history for the movement report.

    Maps the "Konačan ishod" (final result / 1X2) odds group; other
    markets (next goal, totals, ...) in the same response are ignored.
    Matches missing that group, with an incomplete or non-ACTIVE 1X2
    line, or outside the current football-only MVP scope are skipped.
    """

    def __init__(
        self,
        directory: Path,
        *,
        source_name: str = "Mozzart",
        pattern: str = "*.json",
    ) -> None:
        self.directory = directory
        self._source_name = source_name
        self._pattern = pattern

    @property
    def source(self) -> str:
        return f"mozzart-file:{self.directory.name}"

    def collect(self) -> list[RawEventOdds]:
        path = self._latest_file()
        observed_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

        data = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
        matches = data.get("items", [])

        result = [
            raw
            for raw in (self._map_match(match, observed_at) for match in matches)
            if raw is not None
        ]

        logger.info(
            "mozzart_file_collector.read",
            extra={
                "path": str(path),
                "matches_in_file": len(matches),
                "records_produced": len(result),
            },
        )

        return result

    def _latest_file(self) -> Path:
        candidates = sorted(
            self.directory.glob(self._pattern),
            key=lambda candidate: candidate.stat().st_mtime,
        )
        if not candidates:
            raise FileNotFoundError(
                f"No files matching {self._pattern!r} in {self.directory}"
            )
        return candidates[-1]

    def _map_match(self, match: dict, observed_at: datetime) -> RawEventOdds | None:
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
                source=self._source_name,
                sport=sport,
                league=match["competition"]["name"],
                home_team=match["home"]["name"],
                away_team=match["visitor"]["name"],
                start_time=datetime.fromtimestamp(
                    match["startTime"] / 1000, tz=timezone.utc
                ),
                observed_at=observed_at,
                market=DEFAULT_MARKET,
                odds=odds,
            )
        except (KeyError, TypeError):
            return None
