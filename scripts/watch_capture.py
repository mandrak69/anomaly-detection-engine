#!/usr/bin/env python3
"""Watches a manual-capture drop directory and runs the app when a new
capture file appears -- so you don't have to manually re-run
`python -m anomaly_detection_engine.app` every time you save a new
capture (Mozzart, or any future manual-capture source).

Source-agnostic on purpose: it doesn't know or care which bookmaker's
capture it's watching, just a directory/filename to watch and which
environment variable that directory should be exposed as (matching
whatever the app's collector for that source expects, e.g.
MOZZART_CAPTURE_DIR or ODDS_API_CAPTURE_DIR).

Polls rather than using a filesystem-events library (watchdog, etc.):
a human dropping a file every few minutes at most doesn't need
sub-second reaction time, and polling avoids a new dependency for what
is otherwise a zero-dependency project.

Usage:
    python scripts/watch_capture.py --dir ./mozzart --env MOZZART_CAPTURE_DIR
    python scripts/watch_capture.py --dir ./mozzart --filename live.json \
        --env MOZZART_CAPTURE_DIR --poll-seconds 10

The watched directory is passed through as-is via --dir; any other env
vars the app needs (ODDS_SOURCE, thresholds, ...) should already be set
in the shell this script runs in, same as running the app directly.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def _wait_until_stable(path: Path, poll_seconds: float) -> None:
    """Waits until `path`'s size stops changing between two polls.

    A quick guard against triggering on a file that's still being
    written (e.g. a slow "Save As" over a network drive) -- for a
    same-machine browser save this almost never actually loops, but the
    cost of checking is negligible next to the cost of ingesting a
    half-written capture.
    """
    previous_size = -1
    while True:
        current_size = path.stat().st_size
        if current_size == previous_size:
            return
        previous_size = current_size
        time.sleep(poll_seconds)


def check_and_run_once(
    watch_path: Path,
    env: dict,
    poll_seconds: float,
    *,
    runner=subprocess.run,
) -> bool:
    """Checks once for a waiting capture and runs the app if there is one.

    Split out from main()'s loop so it's unit-testable without an actual
    infinite loop or real subprocess -- `runner` is injectable for that.
    Returns True if a run was triggered.
    """
    if not watch_path.exists():
        return False

    _wait_until_stable(watch_path, poll_seconds)
    timestamp = time.strftime("%H:%M:%S")
    print(f"[{timestamp}] Capture found at {watch_path} -- running app.py")
    runner(
        [sys.executable, "-m", "anomaly_detection_engine.app"],
        env=env,
        check=False,
    )
    print(f"[{time.strftime('%H:%M:%S')}] Done. Waiting for the next capture...")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir", required=True, type=Path, help="Capture directory to watch"
    )
    parser.add_argument(
        "--filename", default="live.json", help="Drop filename to watch for"
    )
    parser.add_argument(
        "--env",
        required=True,
        help="Environment variable to set to --dir's path when running the app "
        "(e.g. MOZZART_CAPTURE_DIR)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="How often to check for a new capture (default: 5s)",
    )
    args = parser.parse_args()

    watch_path = args.dir / args.filename
    print(f"Watching {watch_path} (polling every {args.poll_seconds}s). Ctrl+C to stop.")

    env = os.environ.copy()
    env[args.env] = str(args.dir)

    try:
        while True:
            check_and_run_once(watch_path, env, args.poll_seconds)
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
