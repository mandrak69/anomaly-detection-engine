#!/usr/bin/env python3
r"""Builds a single Mozzart manual-capture drop file from a HAR export.

MozzartFileCollector (see collectors/mozzart_file_collector.py) expects
ONE JSON response at MOZZART_CAPTURE_DIR/live.json (or
MOZZART_PREMATCH_CAPTURE_DIR/live.json), shaped like a single {"items":
[...]} matches listing. But mozzartbet.com's listing is a POST request
paginated by a `currentPage`/`pageSize` request body, not a query string
-- a full football listing is commonly 30-40 separate page requests. A
HAR export (DevTools Network tab -> "Save all as HAR with content")
already contains every one of those page responses in one file, so this
script merges them into the single combined {"items": [...]} envelope
the existing collector already knows how to parse -- no change to
parse_mozzart_response needed.

This intentionally does NOT parse odds itself (no RawEventOdds mapping
here) -- that stays exactly where it already lives, in
parse_mozzart_response. This script's only job is acquisition: turn a
HAR export into the same shape a single manual capture would have
produced, so it can be dropped in place of one.

Usage:
    python scripts/import_mozzart_har.py --har mozzart.har ^
        --capture-dir "C:\mozzart-prematch"

    # Or point at MOZZART_CAPTURE_DIR/MOZZART_PREMATCH_CAPTURE_DIR
    # directly via --capture-dir-env:
    python scripts/import_mozzart_har.py --har mozzart.har ^
        --capture-dir-env MOZZART_PREMATCH_CAPTURE_DIR

Only POST responses to mozzartbet.com's own matches-listing endpoint are
considered, matched by URL alone (not method -- HAR entries for this
endpoint are always POST; a GET landing at the same URL by coincidence
would be a different, unrelated endpoint, so this deliberately does not
filter on method the way the Meridianbet importer filters out its own
OPTIONS preflight -- Mozzart's own OPTIONS preflight, if captured, still
has an empty response body and is skipped by the same "no/invalid
response text" check either way).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_URL_PATTERN = "*mozzartbet.com/betting/matches*"


class HarImportError(ValueError):
    """Raised when the HAR doesn't contain anything usable for this source."""


def _wildcard_to_matcher(pattern: str) -> Callable[[str], bool]:
    compiled = re.compile(fnmatch.translate(pattern))
    return lambda url: compiled.match(url) is not None


def extract_mozzart_pages(har: dict[str, Any], url_pattern: str) -> list[dict[str, Any]]:
    """Returns the parsed JSON payload of every entry in `har` whose URL
    matches `url_pattern` and whose response body actually parses as
    JSON. Skips any entry with no/invalid response text (defensive -- a
    captured 4xx/5xx or a truncated save shouldn't crash the whole
    import, just be excluded, since the caller will already fail loudly
    via HarImportError if the end result has zero usable pages)."""
    matches_url = _wildcard_to_matcher(url_pattern)
    pages: list[dict[str, Any]] = []

    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        if not matches_url(request.get("url", "")):
            continue

        text = entry.get("response", {}).get("content", {}).get("text")
        if not text:
            continue

        try:
            pages.append(json.loads(text))
        except json.JSONDecodeError:
            continue

    return pages


def merge_mozzart_pages(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Merges multiple per-page Mozzart matches-listing responses into
    one envelope with a combined "items"[] -- the exact shape
    parse_mozzart_response already accepts -- deduplicated by each
    match's own "id" in case pagination overlaps.

    Raises HarImportError if `pages` is empty, or if not a single page
    has a recognizable {"items": [...]} shape at all -- same "stop the
    run rather than silently ingest nothing" precedent
    parse_mozzart_response itself already sets for a wrong capture, and
    import_meridianbet_har.py's own merge function sets for that source.
    """
    if not pages:
        raise HarImportError(
            "No matching responses found in this HAR -- check that the "
            "right requests were captured (see docs/manual-capture-sources.md) "
            "and that --url-pattern actually matches them."
        )

    items_by_id: dict[Any, dict[str, Any]] = {}
    template: dict[str, Any] | None = None
    saw_items_key = False

    for page in pages:
        if not isinstance(page, dict) or "items" not in page:
            continue
        saw_items_key = True
        if template is None:
            template = page
        for item in page["items"]:
            key = item.get("id", id(item))
            items_by_id[key] = item

    if not saw_items_key:
        raise HarImportError(
            "None of the matched HAR entries look like a Mozzart matches "
            "listing response (expected an 'items' key) -- check that the "
            "right requests were captured, not e.g. a live-scoreboard or "
            "market-detail endpoint (see docs/manual-capture-sources.md)."
        )

    assert template is not None  # saw_items_key True guarantees this
    merged = dict(template)
    merged["items"] = list(items_by_id.values())
    return merged


def import_har(har_path: Path, capture_dir: Path, *, url_pattern: str, filename: str) -> Path:
    """Reads `har_path`, merges every matching page, and writes the result
    to `capture_dir/filename` -- the exact drop-file location
    ManualCaptureCollector.collect() watches. Overwrites any existing
    drop file, same as a human manually re-saving a capture would."""
    with open(har_path, encoding="utf-8") as f:
        har = json.load(f)

    pages = extract_mozzart_pages(har, url_pattern)
    merged = merge_mozzart_pages(pages)

    capture_dir.mkdir(parents=True, exist_ok=True)
    out_path = capture_dir / filename
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
        f.write("\n")

    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--har", required=True, type=Path, help="Path to the .har file")
    parser.add_argument(
        "--capture-dir",
        type=Path,
        default=None,
        help="Drop-file directory (default: $MOZZART_CAPTURE_DIR, or --capture-dir-env)",
    )
    parser.add_argument(
        "--capture-dir-env",
        default="MOZZART_CAPTURE_DIR",
        help=(
            "Env var to read --capture-dir from when it's not given directly "
            "(default: MOZZART_CAPTURE_DIR; pass MOZZART_PREMATCH_CAPTURE_DIR "
            "to import into the pre-match capture dir instead)"
        ),
    )
    parser.add_argument(
        "--filename",
        default="live.json",
        help="Drop filename (default matches MozzartFileCollector's own default filename)",
    )
    parser.add_argument(
        "--url-pattern",
        default=DEFAULT_URL_PATTERN,
        help=f"Wildcard pattern for request URLs to include (default: {DEFAULT_URL_PATTERN})",
    )
    args = parser.parse_args()

    capture_dir = args.capture_dir
    if capture_dir is None:
        env_dir = os.environ.get(args.capture_dir_env)
        if not env_dir:
            print(
                f"error: --capture-dir not given and {args.capture_dir_env} is not set",
                file=sys.stderr,
            )
            raise SystemExit(2)
        capture_dir = Path(env_dir)

    try:
        out_path = import_har(
            args.har, capture_dir, url_pattern=args.url_pattern, filename=args.filename
        )
    except HarImportError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    print(f"Wrote merged capture to {out_path}")


if __name__ == "__main__":
    main()
