"""One-off diagnostic: what fraction of currently-tracked odds lines
actually get a fresh source_timestamp within a short gap?

Answers a question the production database alone can't: historical
odds_snapshots analysis showed a median ~4h gap between distinct
source_timestamps per (event, bookmaker, market, outcome), but the
*minimum* observed gap sat right at poller.py's own ~90min poll
interval across thousands of samples -- a classic sampling-floor
artifact, not evidence that nothing updates faster than 90 min. This
script measures the real floor directly instead of guessing: two live
ApiFootballCollector.collect() calls a short gap_seconds apart, diffed
in memory.

Deliberately calls the collector directly rather than going through
OddsIngestionService/Runtime -- this never touches the production
database, so it can't interfere with (or be interfered by) poller.py
running alongside it, and needs no DB/matcher/repository setup at all.

Cost: exactly 2x ApiFootballCollector.collect()'s request cost (~5
requests each, ~10 total -- 1 fixtures + up to 4 odds/pagination per
call). Check the day's remaining free-tier budget before running this;
it is not on any schedule and does not repeat itself.
"""

import argparse
import time
from collections import Counter

from anomaly_detection_engine.collectors.api_football_collector import ApiFootballCollector
from anomaly_detection_engine.config import load_dotenv
from anomaly_detection_engine.models.raw_odds import RawEventOdds

_LineKey = tuple[str, str, str | None, object, str]


def _line_keys(records: list[RawEventOdds]) -> dict[_LineKey, object]:
    lines: dict[_LineKey, object] = {}
    for raw in records:
        for outcome in raw.odds:
            key = (raw.home_team, raw.away_team, raw.source_id, raw.market, outcome)
            lines[key] = raw.source_timestamp
    return lines


def _snapshot(label: str) -> dict[_LineKey, object]:
    collector = ApiFootballCollector()
    result = collector.collect()
    lines = _line_keys(result.records)
    print(f"[{label}] fetched {len(result.records)} raw records -> {len(lines)} odds lines")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gap-seconds",
        type=int,
        default=300,
        help="Seconds between the two collect() calls (default: 300 = 5 min).",
    )
    args = parser.parse_args()

    load_dotenv()

    snap1 = _snapshot("t=0")
    print(f"Sleeping {args.gap_seconds}s before the second fetch...")
    time.sleep(args.gap_seconds)
    snap2 = _snapshot(f"t=+{args.gap_seconds}s")

    common = set(snap1) & set(snap2)
    if not common:
        print("No odds lines present in both snapshots -- can't compare (day rolled over?).")
        return

    changed = [key for key in common if snap1[key] != snap2[key]]

    print()
    print(f"Lines tracked in both snapshots: {len(common)}")
    print(
        f"  changed source_timestamp within {args.gap_seconds}s: "
        f"{len(changed)} ({len(changed) / len(common) * 100:.1f}%)"
    )
    print(
        f"  unchanged (no real update yet):           "
        f"{len(common) - len(changed)} ({(len(common) - len(changed)) / len(common) * 100:.1f}%)"
    )

    if changed:
        by_bookmaker = Counter(key[2] for key in changed)
        print()
        print("Changed lines by bookmaker (source_id), top 10:")
        for source_id, count in by_bookmaker.most_common(10):
            print(f"  {source_id}: {count}")


if __name__ == "__main__":
    main()
