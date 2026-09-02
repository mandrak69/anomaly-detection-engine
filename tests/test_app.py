import anomaly_detection_engine.app as app
from anomaly_detection_engine.collectors.json_collector import JsonOddsCollector
from anomaly_detection_engine.collectors.mozzart_file_collector import MozzartFileCollector


def test_default_source_uses_two_json_collector_polls(monkeypatch):
    monkeypatch.delenv("ODDS_SOURCE", raising=False)
    monkeypatch.delenv("MOZZART_CAPTURE_DIR", raising=False)

    collectors = app.build_collectors()

    assert len(collectors) == 2
    assert all(isinstance(c, JsonOddsCollector) for c in collectors)
    assert collectors[0].source == "json:odds_sample.json"
    assert collectors[1].source == "json:odds_sample_poll2.json"


def test_the_odds_api_source_used_when_configured(monkeypatch):
    monkeypatch.setenv("ODDS_SOURCE", "the-odds-api")
    monkeypatch.setenv("ODDS_SPORT_KEY", "soccer_epl")
    monkeypatch.delenv("MOZZART_CAPTURE_DIR", raising=False)

    class FakeLiveCollector:
        def __init__(self, sport_key):
            self._sport_key = sport_key

        @property
        def source(self):
            return f"the-odds-api:{self._sport_key}"

    monkeypatch.setattr(app, "TheOddsApiCollector", FakeLiveCollector)

    collectors = app.build_collectors()

    assert len(collectors) == 1
    assert collectors[0].source == "the-odds-api:soccer_epl"


def test_mozzart_capture_dir_adds_a_supplemental_collector(monkeypatch, tmp_path):
    monkeypatch.delenv("ODDS_SOURCE", raising=False)
    monkeypatch.setenv("MOZZART_CAPTURE_DIR", str(tmp_path))

    collectors = app.build_collectors()

    assert len(collectors) == 3
    assert isinstance(collectors[2], MozzartFileCollector)
    assert collectors[2].capture_dir == tmp_path


def test_no_mozzart_capture_dir_means_no_supplemental_collector(monkeypatch):
    monkeypatch.delenv("ODDS_SOURCE", raising=False)
    monkeypatch.delenv("MOZZART_CAPTURE_DIR", raising=False)

    collectors = app.build_collectors()

    assert len(collectors) == 2
