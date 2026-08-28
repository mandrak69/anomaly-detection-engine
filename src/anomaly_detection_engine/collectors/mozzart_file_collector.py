import json
import logging
import uuid
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
    """Watches a fixed drop-file for a manually-captured Mozzart response.

    mozzartbet.com sits behind Cloudflare bot-management (cf_clearance /
    __cf_bm cookies observed on the captured request) -- an automated
    fetch here would mean scripting around that protection, which this
    project won't do. The capture step stays manual: in your own browser,
    open DevTools -> Network, find the matches request (e.g.
    /live/matches), and save its response body to `capture_dir /
    filename` (default "live.json"), overwriting the same file each time
    you capture a new reading.

    Each collect() call:
      - returns [] if the drop file isn't there yet (nothing new to
        report this cycle -- not an error);
      - otherwise parses it, and moves it into `capture_dir/history/`
        under a timestamped name so the drop slot is free for the next
        capture and every raw reading is kept for traceability/replay.
        A parse failure leaves the file in place (not archived) so it
        stays visible for you to inspect instead of silently vanishing
        into history.

    Maps the "Konačan ishod" (final result / 1X2) odds group; other
    markets (next goal, totals, ...) in the same response are ignored.
    Matches missing that group, with an incomplete or non-ACTIVE 1X2
    line, or outside the current football-only MVP scope are skipped.
    """

    def __init__(
        self,
        capture_dir: Path,
        *,
        filename: str = "live.json",
        history_dirname: str = "history",
        source_name: str = "Mozzart",
    ) -> None:
        self.capture_dir = capture_dir
        self._filename = filename
        self._history_dir = capture_dir / history_dirname
        self._source_name = source_name

    @property
    def source(self) -> str:
        return f"mozzart-file:{self.capture_dir.name}"

    def collect(self) -> list[RawEventOdds]:
        drop_path = self.capture_dir / self._filename
        if not drop_path.exists():
            logger.info(
                "mozzart_file_collector.no_new_capture",
                extra={"path": str(drop_path)},
            )
            return []

        observed_at = datetime.fromtimestamp(drop_path.stat().st_mtime, tz=timezone.utc)

        data = json.loads(drop_path.read_text(encoding="utf-8"), parse_float=Decimal)
        matches = data.get("items", [])

        result = [
            raw
            for raw in (self._map_match(match, observed_at) for match in matches)
            if raw is not None
        ]

        archived_path = self._archive(drop_path, observed_at)

        logger.info(
            "mozzart_file_collector.read",
            extra={
                "path": str(drop_path),
                "archived_to": str(archived_path),
                "matches_in_file": len(matches),
                "records_produced": len(result),
            },
        )

        return result

    def _archive(self, path: Path, observed_at: datetime) -> Path:
        # Timestamp alone isn't a reliable uniqueness guarantee -- captures
        # dropped in rapid succession can land within the same filesystem
        # mtime tick, which would make a second archive silently overwrite
        # the first via Path.replace(). The short random suffix guarantees
        # no collision regardless of clock resolution.
        self._history_dir.mkdir(parents=True, exist_ok=True)
        stamp = observed_at.strftime("%Y%m%dT%H%M%SZ")
        unique = uuid.uuid4().hex[:8]
        archived_path = self._history_dir / f"{path.stem}_{stamp}_{unique}{path.suffix}"
        path.replace(archived_path)
        return archived_path

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
