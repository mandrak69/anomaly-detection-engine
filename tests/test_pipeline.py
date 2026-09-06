import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

import anomaly_detection_engine.config as config
import anomaly_detection_engine.pipeline as pipeline
from anomaly_detection_engine.analysis.freshness import FreshnessPolicy
from anomaly_detection_engine.collectors.json_collector import JsonOddsCollector
from anomaly_detection_engine.collectors.mozzart_file_collector import MozzartFileCollector
from anomaly_detection_engine.collectors.the_odds_api_collector import TheOddsApiManualCollector
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.market import DEFAULT_MARKET
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.storage.database import configure_connection, initialize_database
from anomaly_detection_engine.storage.movement_repository import MovementRepository
from anomaly_detection_engine.storage.odds_repository import OddsRepository
from anomaly_detection_engine.storage.signal_repository import SignalRepository


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

    collectors = pipeline.build_collectors(config.load_config())

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

    monkeypatch.setattr(pipeline, "TheOddsApiCollector", FakeLiveCollector)

    collectors = pipeline.build_collectors(config.load_config())

    assert len(collectors) == 1
    assert collectors[0].source == "the-odds-api:soccer_epl"


def test_invalid_odds_source_raises(monkeypatch):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_SOURCE", "the-odds-ap1")

    with pytest.raises(ValueError, match="the-odds-ap1"):
        config.load_config()


def test_odds_api_mode_manual_uses_manual_collector(monkeypatch, tmp_path):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_SOURCE", "the-odds-api")
    monkeypatch.setenv("ODDS_SPORT_KEY", "soccer_epl")
    monkeypatch.setenv("ODDS_API_MODE", "manual")
    monkeypatch.setenv("ODDS_API_CAPTURE_DIR", str(tmp_path))

    collectors = pipeline.build_collectors(config.load_config())

    assert len(collectors) == 1
    assert isinstance(collectors[0], TheOddsApiManualCollector)
    assert collectors[0].source == "the-odds-api-manual:soccer_epl"


def test_odds_api_mode_manual_without_capture_dir_raises(monkeypatch):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_SOURCE", "the-odds-api")
    monkeypatch.setenv("ODDS_API_MODE", "manual")

    with pytest.raises(ValueError, match="ODDS_API_CAPTURE_DIR"):
        pipeline.build_collectors(config.load_config())


def test_odds_api_mode_invalid_value_raises(monkeypatch):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_SOURCE", "the-odds-api")
    monkeypatch.setenv("ODDS_API_MODE", "telepathic")

    with pytest.raises(ValueError, match="telepathic"):
        pipeline.build_collectors(config.load_config())


def test_mozzart_capture_dir_adds_a_supplemental_collector(monkeypatch, tmp_path):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("MOZZART_CAPTURE_DIR", str(tmp_path))

    collectors = pipeline.build_collectors(config.load_config())

    assert len(collectors) == 3
    assert isinstance(collectors[2], MozzartFileCollector)
    assert collectors[2].capture_dir == tmp_path


def test_mozzart_mode_defaults_to_manual_explicitly(monkeypatch, tmp_path):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("MOZZART_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setenv("MOZZART_MODE", "manual")

    collectors = pipeline.build_collectors(config.load_config())

    assert isinstance(collectors[2], MozzartFileCollector)


def test_mozzart_mode_auto_is_rejected(monkeypatch, tmp_path):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("MOZZART_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setenv("MOZZART_MODE", "auto")

    with pytest.raises(ValueError, match="MOZZART_MODE"):
        pipeline.build_collectors(config.load_config())


def test_no_mozzart_capture_dir_means_no_supplemental_collector(monkeypatch):
    _clear_source_env(monkeypatch)

    collectors = pipeline.build_collectors(config.load_config())

    assert len(collectors) == 2


def _repositories():
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    return (
        OddsRepository(connection),
        SignalRepository(connection),
        MovementRepository(connection),
    )


def test_persist_detected_signals_creates_then_resolves_a_surebet():
    odds_repository, signal_repository, movement_repository = _repositories()
    now = datetime.fromisoformat("2026-08-27T10:00:00+00:00")
    event = Event(
        id="e1",
        sport="football",
        league="L",
        competition_id="competition-1",
        home_team=Team("h", "A"),
        away_team=Team("a", "B"),
        start_time=now,
    )
    policy = FreshnessPolicy(
        max_snapshot_age=timedelta(hours=1), max_observation_spread=timedelta(hours=1)
    )

    def save(outcome, odds, observed_at):
        odds_repository.save(
            OddsSnapshot(
                event_id="e1",
                bookmaker=Bookmaker("bet1", "Bet1"),
                market=DEFAULT_MARKET,
                outcome=outcome,
                odds=Decimal(odds),
                observed_at=observed_at,
            )
        )

    save("1", "2.50", now)
    save("X", "4.00", now)
    save("2", "4.00", now)

    first_sweep = pipeline.persist_detected_signals(
        [event],
        odds_repository,
        signal_repository,
        movement_repository,
        market=DEFAULT_MARKET,
        freshness_policy=policy,
        analysis_time=now,
    )
    assert first_sweep["active_surebets"] == 1
    active = signal_repository.find_active("SUREBET")
    assert len(active) == 1
    assert active[0].details["legs"][0]["bookmaker"] == "Bet1"

    # Odds move enough to kill the arbitrage (margin now > 1) -- the
    # signal should resolve, and the price change itself should be
    # recorded as a movement.
    save("1", "2.00", now + timedelta(minutes=5))

    second_sweep = pipeline.persist_detected_signals(
        [event],
        odds_repository,
        signal_repository,
        movement_repository,
        market=DEFAULT_MARKET,
        freshness_policy=policy,
        analysis_time=now + timedelta(minutes=5),
    )
    assert second_sweep["active_surebets"] == 0
    assert second_sweep["movements_recorded"] == 1
    assert signal_repository.find_active("SUREBET") == []
