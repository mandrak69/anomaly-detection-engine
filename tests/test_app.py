import pytest

import anomaly_detection_engine.app as app
from anomaly_detection_engine.collectors.json_collector import JsonOddsCollector
from anomaly_detection_engine.collectors.mozzart_file_collector import MozzartFileCollector
from anomaly_detection_engine.collectors.the_odds_api_collector import TheOddsApiManualCollector


def _clear_source_env(monkeypatch):
    for var in (
        "ODDS_SOURCE",
        "ODDS_API_MODE",
        "ODDS_API_CAPTURE_DIR",
        "MOZZART_MODE",
        "MOZZART_CAPTURE_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


def test_default_source_uses_two_json_collector_polls(monkeypatch):
    _clear_source_env(monkeypatch)

    collectors = app.build_collectors()

    assert len(collectors) == 2
    assert all(isinstance(c, JsonOddsCollector) for c in collectors)
    assert collectors[0].source == "json:odds_sample.json"
    assert collectors[1].source == "json:odds_sample_poll2.json"


def test_the_odds_api_source_used_when_configured(monkeypatch):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_SOURCE", "the-odds-api")
    monkeypatch.setenv("ODDS_SPORT_KEY", "soccer_epl")

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


def test_odds_api_mode_manual_uses_manual_collector(monkeypatch, tmp_path):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_SOURCE", "the-odds-api")
    monkeypatch.setenv("ODDS_SPORT_KEY", "soccer_epl")
    monkeypatch.setenv("ODDS_API_MODE", "manual")
    monkeypatch.setenv("ODDS_API_CAPTURE_DIR", str(tmp_path))

    collectors = app.build_collectors()

    assert len(collectors) == 1
    assert isinstance(collectors[0], TheOddsApiManualCollector)
    assert collectors[0].source == "the-odds-api-manual:soccer_epl"


def test_odds_api_mode_manual_without_capture_dir_raises(monkeypatch):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_SOURCE", "the-odds-api")
    monkeypatch.setenv("ODDS_API_MODE", "manual")

    with pytest.raises(ValueError, match="ODDS_API_CAPTURE_DIR"):
        app.build_collectors()


def test_odds_api_mode_invalid_value_raises(monkeypatch):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_SOURCE", "the-odds-api")
    monkeypatch.setenv("ODDS_API_MODE", "telepathic")

    with pytest.raises(ValueError, match="telepathic"):
        app.build_collectors()


def test_mozzart_capture_dir_adds_a_supplemental_collector(monkeypatch, tmp_path):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("MOZZART_CAPTURE_DIR", str(tmp_path))

    collectors = app.build_collectors()

    assert len(collectors) == 3
    assert isinstance(collectors[2], MozzartFileCollector)
    assert collectors[2].capture_dir == tmp_path


def test_mozzart_mode_defaults_to_manual_explicitly(monkeypatch, tmp_path):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("MOZZART_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setenv("MOZZART_MODE", "manual")

    collectors = app.build_collectors()

    assert isinstance(collectors[2], MozzartFileCollector)


def test_mozzart_mode_auto_is_rejected(monkeypatch, tmp_path):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("MOZZART_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setenv("MOZZART_MODE", "auto")

    with pytest.raises(ValueError, match="MOZZART_MODE"):
        app.build_collectors()


def test_no_mozzart_capture_dir_means_no_supplemental_collector(monkeypatch):
    _clear_source_env(monkeypatch)

    collectors = app.build_collectors()

    assert len(collectors) == 2
