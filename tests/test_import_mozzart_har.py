import json

import pytest

from import_mozzart_har import (
    HarImportError,
    extract_mozzart_pages,
    import_har,
    merge_mozzart_pages,
)

MATCHING_URL = "https://www.mozzartbet.com/betting/matches"
UNRELATED_URL = "https://www.mozzartbet.com/betting/live-scoreboard"


def har_entry(url: str, *, method: str = "POST", text: str | None = "{}") -> dict:
    content: dict = {}
    if text is not None:
        content["text"] = text
    return {
        "request": {"method": method, "url": url},
        "response": {"content": content},
    }


def har(*entries: dict) -> dict:
    return {"log": {"entries": list(entries)}}


def match(match_id, home="Home") -> dict:
    return {"id": match_id, "home": {"name": home}}


def envelope(**body) -> str:
    return json.dumps({"nextStepPath": "", "matchCount": 0, **body})


class TestExtractMozzartPages:
    def test_keeps_only_requests_matching_the_pattern(self):
        data = har(
            har_entry(MATCHING_URL, text=envelope(items=[match(1)])),
            har_entry(UNRELATED_URL, text=envelope(items=[match(2)])),
        )

        pages = extract_mozzart_pages(data, "*mozzartbet.com/betting/matches*")

        assert len(pages) == 1
        assert pages[0]["items"][0]["id"] == 1

    def test_skips_entries_with_no_body_or_invalid_json(self):
        data = har(
            har_entry(MATCHING_URL, text=None),
            har_entry(MATCHING_URL, text="not json"),
            har_entry(MATCHING_URL, text=envelope(items=[match(1)])),
        )

        pages = extract_mozzart_pages(data, "*mozzartbet.com/betting/matches*")

        assert len(pages) == 1


class TestMergeMozzartPages:
    def test_raises_when_no_pages_at_all(self):
        with pytest.raises(HarImportError):
            merge_mozzart_pages([])

    def test_raises_when_no_page_has_a_recognizable_shape(self):
        with pytest.raises(HarImportError):
            merge_mozzart_pages([{"positions": [], "configuredPositions": True}])

    def test_combines_items_from_separate_pages(self):
        pages = [
            {"items": [match(1)]},
            {"items": [match(2)]},
        ]

        merged = merge_mozzart_pages(pages)

        match_ids = {m["id"] for m in merged["items"]}
        assert match_ids == {1, 2}

    def test_deduplicates_items_when_the_same_match_spans_pages(self):
        # Same match id appearing on two pages (a pagination overlap) --
        # must not be duplicated in the merged output.
        pages = [
            {"items": [match(1), match(2)]},
            {"items": [match(2), match(3)]},
        ]

        merged = merge_mozzart_pages(pages)

        match_ids = {m["id"] for m in merged["items"]}
        assert match_ids == {1, 2, 3}
        assert len(merged["items"]) == 3

    def test_preserves_top_level_envelope_fields_from_the_first_page(self):
        pages = [{"nextStepPath": "/some/path", "matchCount": 15, "items": [match(1)]}]

        merged = merge_mozzart_pages(pages)

        assert merged["nextStepPath"] == "/some/path"


class TestImportHar:
    def test_writes_the_merged_drop_file(self, tmp_path):
        har_path = tmp_path / "capture.har"
        har_path.write_text(
            json.dumps(
                har(
                    har_entry(MATCHING_URL, text=envelope(items=[match(1)])),
                    har_entry(MATCHING_URL, text=envelope(items=[match(2)])),
                )
            ),
            encoding="utf-8",
        )

        capture_dir = tmp_path / "mozzart-prematch"
        out_path = import_har(
            har_path,
            capture_dir,
            url_pattern="*mozzartbet.com/betting/matches*",
            filename="live.json",
        )

        assert out_path == capture_dir / "live.json"
        written = json.loads(out_path.read_text(encoding="utf-8"))
        match_ids = {m["id"] for m in written["items"]}
        assert match_ids == {1, 2}

    def test_raises_and_writes_nothing_when_the_har_has_no_matching_pages(self, tmp_path):
        har_path = tmp_path / "capture.har"
        har_path.write_text(json.dumps(har(har_entry(UNRELATED_URL))), encoding="utf-8")
        capture_dir = tmp_path / "mozzart-prematch"

        with pytest.raises(HarImportError):
            import_har(
                har_path,
                capture_dir,
                url_pattern="*mozzartbet.com/betting/matches*",
                filename="live.json",
            )

        assert not (capture_dir / "live.json").exists()
