import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from anomaly_detection_engine.collectors.base import OddsCollector
from anomaly_detection_engine.models.raw_odds import RawEventOdds

logger = logging.getLogger(__name__)

ParseFn = Callable[[str, datetime], list[RawEventOdds]]


class ManualCaptureCollector(OddsCollector):
    """Generic drop-file + archive acquisition for any manually-captured source.

    Fetches nothing itself -- this is only the acquisition half of a
    manual-capture source: some human process (a browser session, an
    export, whatever) saves a raw response to `capture_dir/filename`,
    and collect() picks it up, hands it to `parse` (the source's own
    schema-specific mapping, injected rather than owned here), and
    archives it.

    Splitting acquisition (this class) from parsing (the `parse`
    callable) is what lets *any* source support "capture manually
    instead of fetching automatically" as a mode -- not just a source
    that has no automatic option at all (mozzartbet.com's Cloudflare
    bot-management, see MozzartFileCollector), but also one that
    normally fetches fine (TheOddsApiCollector) and might still need a
    manual fallback sometimes: a rate limit, an outage, an exhausted
    quota. Both wrap this same class with their own `parse` function
    rather than duplicating the drop-file/archive mechanics.

    Each collect() call:
      - returns [] if the drop file isn't there yet (nothing new this
        cycle -- not an error);
      - otherwise reads it, calls `parse(raw_text, observed_at)` where
        observed_at is the file's own modification time (when the
        capture actually happened, not whenever this happens to run),
        then archives the file into `capture_dir/history/` under a
        timestamped + unique-suffixed name so the drop slot is free for
        the next capture;
      - a parse failure leaves the file in place (not archived) so it
        stays visible to inspect instead of silently vanishing into
        history.
    """

    def __init__(
        self,
        capture_dir: Path,
        *,
        parse: ParseFn,
        source_label: str,
        filename: str = "capture.json",
        history_dirname: str = "history",
    ) -> None:
        self.capture_dir = capture_dir
        self._parse = parse
        self._source_label = source_label
        self._filename = filename
        self._history_dir = capture_dir / history_dirname

    @property
    def source(self) -> str:
        return self._source_label

    def collect(self) -> list[RawEventOdds]:
        drop_path = self.capture_dir / self._filename
        if not drop_path.exists():
            logger.info(
                "manual_capture_collector.no_new_capture",
                extra={"path": str(drop_path), "source": self._source_label},
            )
            return []

        observed_at = datetime.fromtimestamp(drop_path.stat().st_mtime, tz=UTC)
        raw_text = drop_path.read_text(encoding="utf-8")

        result = self._parse(raw_text, observed_at)

        archived_path = self._archive(drop_path, observed_at)

        logger.info(
            "manual_capture_collector.read",
            extra={
                "path": str(drop_path),
                "archived_to": str(archived_path),
                "source": self._source_label,
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
