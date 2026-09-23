#!/usr/bin/env python3
r"""Builds a single Meridianbet manual-capture drop file from a HAR export.

MeridianbetFileCollector (see collectors/meridianbet_file_collector.py)
expects ONE JSON response at MERIDIANBET_CAPTURE_DIR/meridianbet.json,
shaped like a single pre-match listing response (payload.leagues[] or
payload.events[]). But meridianbet.com paginates that listing across many
requests (?page=0, ?page=1, ...) -- a full football listing is commonly
40-50 separate page responses, each covering a different subset of
leagues. A HAR export (DevTools Network tab -> "Save all as HAR with
content") already contains every one of those page responses in one file,
so this script merges them into the single combined envelope the existing
collector already knows how to parse -- no change to
parse_meridianbet_response needed.

This intentionally does NOT parse odds itself (no RawEventOdds mapping
here) -- that stays exactly where it already lives, in
parse_meridianbet_response. This script's only job is acquisition: turn a
HAR export into the same shape a single manual capture would have
produced, so it can be dropped in place of one.

Usage:
    python scripts/import_meridianbet_har.py --har meridianbet.har ^
        --capture-dir "C:\meridianbet"

    # Or point at MERIDIANBET_CAPTURE_DIR directly:
    python scripts/import_meridianbet_har.py --har meridianbet.har

If --capture-dir is omitted, MERIDIANBET_CAPTURE_DIR from the environment
is used (matching how the app itself is configured -- see .env.example).

Only GET responses are considered, so the CORS preflight (OPTIONS) HAR
also records for the same URL -- with an empty body -- is skipped
automatically without needing special-case handling.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_URL_PATTERN = "*meridianbet.com/betshop/api/v1/*/sport/*/*?page=*"


class HarImportError(ValueError):
    """Raised when the HAR doesn't contain anything usable for this source."""


def _wildcard_to_matcher(pattern: str):
    compiled = re.compile(fnmatch.translate(pattern))
    return lambda url: compiled.match(url) is not None


def extract_meridianbet_pages(har: dict[str, Any], url_pattern: str) -> list[dict[str, Any]]:
    """Returns the parsed JSON payload of every GET entry in `har` whose
    URL matches `url_pattern`. Skips non-GET entries (the OPTIONS
    preflight HAR also records for the same URL, with no body) and any
    entry with no/invalid response text (defensive -- a captured 4xx/5xx
    or a truncated save shouldn't crash the whole import, just be
    excluded, since the caller will already fail loudly via
    HarImportError if the end result has zero usable pages)."""
    matches_url = _wildcard_to_matcher(url_pattern)
    pages: list[dict[str, Any]] = []

    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        if request.get("method", "").upper() != "GET":
            continue
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


def merge_meridianbet_pages(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Merges multiple per-page Meridianbet listing responses into one
    envelope with a combined payload.leagues[] (deduplicated by
    leagueId, with each league's own events deduplicated by
    header.eventId in case a league's events ever end up split across
    two page boundaries) -- the exact shape
    parse_meridianbet_response's payload.leagues[] branch already
    accepts, so nothing downstream needs to change.

    Pages using the flatter payload.events[] shape are supported too,
    merged into one deduplicated (by header.eventId) events list -- but
    a HAR mixing both shapes across its pages raises HarImportError,
    since that would mean two genuinely different endpoints got captured
    together by mistake (see docs/manual-capture-sources.md's own
    "wrong shapes captured before" warnings for why this is worth
    stopping on rather than silently merging something incoherent).

    Raises HarImportError if `pages` is empty, or if not a single page
    has a recognizable payload.leagues/payload.events shape at all --
    same "stop the run rather than silently ingest nothing" precedent
    parse_meridianbet_response itself already sets for a wrong capture.
    """
    if not pages:
        raise HarImportError(
            "No matching GET responses found in this HAR -- check that the "
            "right requests were captured (see docs/manual-capture-sources.md) "
            "and that --url-pattern actually matches them."
        )

    leagues_by_id: dict[Any, dict[str, Any]] = {}
    flat_events_by_id: dict[Any, dict[str, Any]] = {}
    shapes_seen: set[str] = set()
    template: dict[str, Any] | None = None

    for page in pages:
        payload = page.get("payload") if isinstance(page, dict) else None
        if not isinstance(payload, dict):
            continue

        if template is None:
            template = page

        if "leagues" in payload:
            shapes_seen.add("leagues")
            for league in payload["leagues"]:
                key = league.get("leagueId", id(league))
                existing = leagues_by_id.get(key)
                if existing is None:
                    leagues_by_id[key] = dict(league)
                    continue
                # Same league seen on another page (pagination overlap) --
                # merge its events rather than keeping only one page's slice.
                merged_events = {
                    event.get("header", {}).get("eventId", id(event)): event
                    for event in existing.get("events", [])
                }
                for event in league.get("events", []):
                    merged_events[event.get("header", {}).get("eventId", id(event))] = event
                existing["events"] = list(merged_events.values())
        elif "events" in payload:
            shapes_seen.add("events")
            for event in payload["events"]:
                key = event.get("header", {}).get("eventId", id(event))
                flat_events_by_id[key] = event

    if not shapes_seen:
        raise HarImportError(
            "None of the matched HAR entries look like a Meridianbet listing "
            "response (expected payload.leagues or payload.events on at "
            "least one) -- check that the right requests were captured, not "
            "e.g. a market-column-config or video-stream endpoint (see "
            "docs/manual-capture-sources.md)."
        )
    if len(shapes_seen) > 1:
        raise HarImportError(
            f"HAR mixes both Meridianbet response shapes ({sorted(shapes_seen)}) "
            "across its pages -- this usually means two different endpoints "
            "got captured together by mistake. Re-capture with a single, "
            "consistent request pattern."
        )

    assert template is not None  # shapes_seen non-empty guarantees this
    merged = dict(template)
    merged_payload = dict(template["payload"])
    if "leagues" in shapes_seen:
        merged_payload["leagues"] = list(leagues_by_id.values())
    else:
        merged_payload["events"] = list(flat_events_by_id.values())
    merged["payload"] = merged_payload
    return merged


def import_har(har_path: Path, capture_dir: Path, *, url_pattern: str, filename: str) -> Path:
    """Reads `har_path`, merges every matching page, and writes the result
    to `capture_dir/filename` -- the exact drop-file location
    ManualCaptureCollector.collect() watches. Overwrites any existing
    drop file, same as a human manually re-saving a capture would."""
    with open(har_path, encoding="utf-8") as f:
        har = json.load(f)

    pages = extract_meridianbet_pages(har, url_pattern)
    merged = merge_meridianbet_pages(pages)

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
        help="Drop-file directory (default: $MERIDIANBET_CAPTURE_DIR)",
    )
    parser.add_argument(
        "--filename",
        default="meridianbet.json",
        help="Drop filename (default matches MeridianbetFileCollector's own default filename)",
    )
    parser.add_argument(
        "--url-pattern",
        default=DEFAULT_URL_PATTERN,
        help=f"Wildcard pattern for request URLs to include (default: {DEFAULT_URL_PATTERN})",
    )
    args = parser.parse_args()

    capture_dir = args.capture_dir
    if capture_dir is None:
        env_dir = os.environ.get("MERIDIANBET_CAPTURE_DIR")
        if not env_dir:
            print(
                "error: --capture-dir not given and MERIDIANBET_CAPTURE_DIR is not set",
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
