from datetime import datetime
from decimal import Decimal

import pytest

from anomaly_detection_engine.collectors.manual_capture_collector import ManualCaptureCollector
from anomaly_detection_engine.models.market import DEFAULT_MARKET
from anomaly_detection_engine.models.raw_odds import RawEventOdds


def dummy_parse(raw_text: str, observed_at: datetime) -> list[RawEventOdds]:
    """A trivial parse function: raw_text is "home,away,odds1,oddsX,odds2"."""
    home, away, o1, ox, o2 = raw_text.strip().split(",")
    return [
        RawEventOdds(
            source="dummy",
            sport="football",
            league="L",
            home_team=home,
            away_team=away,
            start_time=observed_at,
            observed_at=observed_at,
            market=DEFAULT_MARKET,
            odds={"1": Decimal(o1), "X": Decimal(ox), "2": Decimal(o2)},
        )
    ]


def test_returns_empty_when_no_capture_is_waiting(tmp_path):
    collector = ManualCaptureCollector(tmp_path, parse=dummy_parse, source_label="dummy:test")
    assert collector.collect() == []


def test_reads_and_archives_a_capture(tmp_path):
    drop_path = tmp_path / "capture.json"
    drop_path.write_text("Partizan,Crvena Zvezda,2.10,3.40,3.20", encoding="utf-8")

    collector = ManualCaptureCollector(tmp_path, parse=dummy_parse, source_label="dummy:test")
    result = collector.collect()

    assert len(result) == 1
    assert result[0].home_team == "Partizan"
    assert not drop_path.exists()
    assert list((tmp_path / "history").glob("capture_*.json"))


def test_second_capture_is_archived_separately_from_the_first(tmp_path):
    collector = ManualCaptureCollector(tmp_path, parse=dummy_parse, source_label="dummy:test")

    (tmp_path / "capture.json").write_text("A,B,2.00,3.00,4.00", encoding="utf-8")
    collector.collect()

    (tmp_path / "capture.json").write_text("C,D,2.10,3.10,4.10", encoding="utf-8")
    collector.collect()

    assert len(list((tmp_path / "history").glob("capture_*.json"))) == 2


def test_parse_failure_leaves_the_file_in_place(tmp_path):
    drop_path = tmp_path / "capture.json"
    drop_path.write_text("not,enough,fields", encoding="utf-8")

    collector = ManualCaptureCollector(tmp_path, parse=dummy_parse, source_label="dummy:test")

    with pytest.raises(ValueError):
        collector.collect()

    assert drop_path.exists()
    assert not (tmp_path / "history").exists()


def test_custom_filename_and_source_label(tmp_path):
    drop_path = tmp_path / "snapshot.txt"
    drop_path.write_text("A,B,2.00,3.00,4.00", encoding="utf-8")

    collector = ManualCaptureCollector(
        tmp_path,
        parse=dummy_parse,
        source_label="dummy:custom",
        filename="snapshot.txt",
    )

    assert collector.source == "dummy:custom"
    result = collector.collect()

    assert len(result) == 1
    assert list((tmp_path / "history").glob("snapshot_*.txt"))


def test_observed_at_reflects_the_file_modification_time(tmp_path):
    drop_path = tmp_path / "capture.json"
    drop_path.write_text("A,B,2.00,3.00,4.00", encoding="utf-8")

    import os
    fixed_mtime = datetime.fromisoformat("2026-08-27T10:00:00+00:00").timestamp()
    os.utime(drop_path, (fixed_mtime, fixed_mtime))

    collector = ManualCaptureCollector(tmp_path, parse=dummy_parse, source_label="dummy:test")
    result = collector.collect()

    assert result[0].observed_at == datetime.fromisoformat("2026-08-27T10:00:00+00:00")
