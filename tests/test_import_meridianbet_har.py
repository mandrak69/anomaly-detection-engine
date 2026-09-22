import json

import pytest

from import_meridianbet_har import (
    HarImportError,
    extract_meridianbet_pages,
    import_har,
    merge_meridianbet_pages,
)

MATCHING_URL = "https://online.meridianbet.com/betshop/api/v1/offer/sport/58/leagues?page=0&time=ALL"
OTHER_URL = "https://online.meridianbet.com/betshop/api/v1/offer/sport/58/leagues?page=1&time=ALL"
UNRELATED_URL = "https://online.meridianbet.com/betshop/api/v1/offer/sport/58/some-other-endpoint"


def har_entry(url: str, *, method: str = "GET", text: str | None = "{}") -> dict:
    content: dict = {}
    if text is not None:
        content["text"] = text
    return {
        "request": {"method": method, "url": url},
        "response": {"content": content},
    }


def har(*entries: dict) -> dict:
    return {"log": {"entries": list(entries)}}


def league(league_id, *events) -> dict:
    return {"leagueId": league_id, "leagueName": f"League {league_id}", "events": list(events)}


def event(event_id, name="Match") -> dict:
    return {"header": {"eventId": event_id, "name": name}, "positions": []}


def envelope(**payload) -> str:
    return json.dumps({"errorCode": None, "payload": payload})


class TestExtractMeridianbetPages:
    def test_keeps_only_get_requests_matching_the_pattern(self):
        data = har(
            har_entry(MATCHING_URL, text=envelope(leagues=[league(1)])),
            har_entry(MATCHING_URL, method="OPTIONS", text=""),  # CORS preflight, same URL
            har_entry(UNRELATED_URL, text=envelope(leagues=[league(2)])),
        )

        pages = extract_meridianbet_pages(data, "*meridianbet.com*leagues?page=*")

        assert len(pages) == 1
        assert pages[0]["payload"]["leagues"][0]["leagueId"] == 1

    def test_skips_entries_with_no_body_or_invalid_json(self):
        data = har(
            har_entry(MATCHING_URL, text=None),
            har_entry(MATCHING_URL, text="not json"),
            har_entry(MATCHING_URL, text=envelope(leagues=[league(1)])),
        )

        pages = extract_meridianbet_pages(data, "*leagues?page=*")

        assert len(pages) == 1


class TestMergeMeridianbetPages:
    def test_raises_when_no_pages_at_all(self):
        with pytest.raises(HarImportError):
            merge_meridianbet_pages([])

    def test_raises_when_no_page_has_a_recognizable_shape(self):
        with pytest.raises(HarImportError):
            merge_meridianbet_pages([{"payload": {"positions": []}}])

    def test_raises_when_pages_mix_leagues_and_events_shapes(self):
        pages = [
            {"payload": {"leagues": [league(1)]}},
            {"payload": {"events": [event(1)]}},
        ]
        with pytest.raises(HarImportError):
            merge_meridianbet_pages(pages)

    def test_combines_leagues_from_separate_pages(self):
        pages = [
            {"payload": {"leagues": [league(1, event(101))]}},
            {"payload": {"leagues": [league(2, event(201))]}},
        ]

        merged = merge_meridianbet_pages(pages)

        league_ids = {lg["leagueId"] for lg in merged["payload"]["leagues"]}
        assert league_ids == {1, 2}

    def test_merges_and_deduplicates_events_when_the_same_league_spans_pages(self):
        # Same leagueId appearing on two pages (a pagination overlap) --
        # one shared event (101, should not be duplicated) plus one event
        # unique to each page.
        pages = [
            {"payload": {"leagues": [league(1, event(101), event(102))]}},
            {"payload": {"leagues": [league(1, event(101), event(103))]}},
        ]

        merged = merge_meridianbet_pages(pages)

        assert len(merged["payload"]["leagues"]) == 1
        event_ids = {e["header"]["eventId"] for e in merged["payload"]["leagues"][0]["events"]}
        assert event_ids == {101, 102, 103}

    def test_supports_the_flat_events_shape(self):
        pages = [
            {"payload": {"events": [event(1)]}},
            {"payload": {"events": [event(2)]}},
        ]

        merged = merge_meridianbet_pages(pages)

        event_ids = {e["header"]["eventId"] for e in merged["payload"]["events"]}
        assert event_ids == {1, 2}

    def test_preserves_top_level_envelope_fields_from_the_first_page(self):
        pages = [
            {"errorCode": None, "payload": {"usedTimeFilter": "ALL", "leagues": [league(1)]}},
        ]

        merged = merge_meridianbet_pages(pages)

        assert merged["errorCode"] is None
        assert merged["payload"]["usedTimeFilter"] == "ALL"


class TestImportHar:
    def test_writes_the_merged_drop_file(self, tmp_path):
        har_path = tmp_path / "capture.har"
        har_path.write_text(
            json.dumps(
                har(
                    har_entry(MATCHING_URL, text=envelope(leagues=[league(1, event(101))])),
                    har_entry(OTHER_URL, text=envelope(leagues=[league(2, event(201))])),
                )
            ),
            encoding="utf-8",
        )

        capture_dir = tmp_path / "meridianbet"
        out_path = import_har(
            har_path,
            capture_dir,
            url_pattern="*leagues?page=*",
            filename="meridianbet.json",
        )

        assert out_path == capture_dir / "meridianbet.json"
        written = json.loads(out_path.read_text(encoding="utf-8"))
        league_ids = {lg["leagueId"] for lg in written["payload"]["leagues"]}
        assert league_ids == {1, 2}

    def test_raises_and_writes_nothing_when_the_har_has_no_matching_pages(self, tmp_path):
        har_path = tmp_path / "capture.har"
        har_path.write_text(json.dumps(har(har_entry(UNRELATED_URL))), encoding="utf-8")
        capture_dir = tmp_path / "meridianbet"

        with pytest.raises(HarImportError):
            import_har(
                har_path, capture_dir, url_pattern="*leagues?page=*", filename="meridianbet.json"
            )

        assert not (capture_dir / "meridianbet.json").exists()
