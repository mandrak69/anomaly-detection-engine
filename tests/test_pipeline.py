import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

import anomaly_detection_engine.config as config
import anomaly_detection_engine.pipeline as pipeline
from anomaly_detection_engine.analysis.freshness import FreshnessPolicy
from anomaly_detection_engine.collectors.api_football_collector import ApiFootballCollector
from anomaly_detection_engine.collectors.json_collector import JsonOddsCollector
from anomaly_detection_engine.collectors.mozzart_file_collector import MozzartFileCollector
from anomaly_detection_engine.collectors.the_odds_api_collector import TheOddsApiManualCollector
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.market import DEFAULT_MARKET, TOTALS_2_5_MARKET, MarketType
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.runtime import build_runtime
from anomaly_detection_engine.storage.database import configure_connection, initialize_database
from anomaly_detection_engine.storage.fixture_catalog import FixtureCatalog
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
        "API_FOOTBALL_KEY",
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


def test_api_football_key_adds_a_supplemental_collector(monkeypatch):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")

    collectors = pipeline.build_collectors(config.load_config())

    assert len(collectors) == 3
    assert isinstance(collectors[2], ApiFootballCollector)
    assert collectors[2].provider_id == "api-football"


def test_no_api_football_key_means_no_supplemental_collector(monkeypatch):
    _clear_source_env(monkeypatch)

    collectors = pipeline.build_collectors(config.load_config())

    assert not any(isinstance(c, ApiFootballCollector) for c in collectors)


def test_mozzart_and_api_football_can_both_be_active_at_once(monkeypatch, tmp_path):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("MOZZART_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")

    collectors = pipeline.build_collectors(config.load_config())

    assert len(collectors) == 4
    assert isinstance(collectors[2], MozzartFileCollector)
    assert isinstance(collectors[3], ApiFootballCollector)


def test_run_ingestion_returns_only_events_touched_this_cycle(monkeypatch):
    # run_ingestion() must not return catalog.list_events() (every event
    # the shared FixtureCatalog has ever created) -- an event from a
    # long-past run that today's collectors never mention again would
    # otherwise be handed to run_detection forever, permanently
    # evaluated and permanently "stale". Only what this cycle's polls
    # actually touched should come back.
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("DB_PATH", ":memory:")
    cfg = config.load_config()
    runtime = build_runtime(cfg)

    stale_match = FixtureCatalog(runtime.connection, provider_id="old-source").match(
        sport="football",
        league="Old League",
        home_team_raw="Old Home",
        away_team_raw="Old Away",
        start_time=datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
    )
    stale_event_id = stale_match.event.id

    events = pipeline.run_ingestion(runtime, cfg)

    event_ids = {event.id for event in events}
    assert stale_event_id not in event_ids
    # Both demo JSON polls report the same two real matches under
    # several spellings -- aliasing/fuzzy matching collapses them to
    # exactly two canonical events, both of which this cycle did touch.
    assert len(events) == 2


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


def _save_surebet_snapshots(odds_repository, event_id, observed_at):
    for outcome, odds in [("1", "2.50"), ("X", "4.00"), ("2", "4.00")]:
        odds_repository.save(
            OddsSnapshot(
                event_id=event_id,
                bookmaker=Bookmaker("bet1", "Bet1"),
                market=DEFAULT_MARKET,
                outcome=outcome,
                odds=Decimal(odds),
                observed_at=observed_at,
            )
        )


def test_run_detection_expires_a_signal_once_its_event_stops_being_touched(monkeypatch):
    # A signal for an event that fell out of touched_events (see
    # run_ingestion) can never be resolved by reconcile() -- it's simply
    # never evaluated again. run_detection's separate expire_active_signals
    # step is what eventually clears it once the event's own lifecycle
    # (start_time + signal_ttl) has run out.
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("DB_PATH", ":memory:")
    cfg = config.load_config()
    runtime = build_runtime(cfg)

    ancient_start = datetime.fromisoformat("2000-01-01T00:00:00+00:00")
    match = FixtureCatalog(runtime.connection, provider_id="test").match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=ancient_start,
    )
    event = match.event
    _save_surebet_snapshots(runtime.odds_repository, event.id, observed_at=ancient_start)

    # First cycle: the event is touched and the surebet is genuinely
    # detected -- must not be expired in this same call (it was just
    # reconfirmed ACTIVE), even though its start_time is long past.
    first = pipeline.run_detection(runtime, [event], cfg)
    assert first["active_surebets"] == 1
    assert first["signals_expired"] == 0
    assert len(runtime.signal_repository.find_active("SUREBET")) == 1

    # Second cycle: nothing reports on this event anymore (empty events,
    # simulating run_ingestion's touched_events no longer including it).
    # reconcile() can't resolve it (never evaluated), but enough real
    # time has passed since the first call's last_seen_at that expiry now
    # applies.
    second = pipeline.run_detection(runtime, [], cfg)
    assert second["signals_expired"] == 1
    assert runtime.signal_repository.find_active("SUREBET") == []


def test_run_detection_does_not_expire_a_signal_still_being_touched(monkeypatch):
    # Same ancient start_time as above, but the event keeps being
    # reported every cycle -- it must stay ACTIVE indefinitely, since a
    # provider can legitimately keep quoting an event long after its
    # nominal start_time (delayed kickoff, postponed match, bad
    # scheduling data, ...).
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("DB_PATH", ":memory:")
    cfg = config.load_config()
    runtime = build_runtime(cfg)

    ancient_start = datetime.fromisoformat("2000-01-01T00:00:00+00:00")
    match = FixtureCatalog(runtime.connection, provider_id="test").match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=ancient_start,
    )
    event = match.event
    _save_surebet_snapshots(runtime.odds_repository, event.id, observed_at=ancient_start)

    pipeline.run_detection(runtime, [event], cfg)
    second = pipeline.run_detection(runtime, [event], cfg)

    assert second["signals_expired"] == 0
    assert len(runtime.signal_repository.find_active("SUREBET")) == 1


def _save_totals_surebet_snapshots(odds_repository, event_id, observed_at):
    # Shaped like what ApiFootballCollector actually extracts from a real
    # "Goals Over/Under" bet's 2.5 line (see
    # collectors.api_football_collector._extract_totals_2_5_odds) --
    # OVER/UNDER only, no 1/X/2 -- proving run_detection's DETECTED_MARKETS
    # loop reaches TOTALS_2_5_MARKET, not just DEFAULT_MARKET.
    for outcome, odds in [("OVER", "2.10"), ("UNDER", "2.10")]:
        odds_repository.save(
            OddsSnapshot(
                event_id=event_id,
                bookmaker=Bookmaker("bet1", "Bet1"),
                market=TOTALS_2_5_MARKET,
                outcome=outcome,
                odds=Decimal(odds),
                observed_at=observed_at,
            )
        )


def test_run_detection_detects_a_totals_surebet_not_just_default_market(monkeypatch):
    # Before DETECTED_MARKETS (see pipeline.py), run_detection() only
    # ever called persist_detected_signals with market=DEFAULT_MARKET --
    # a TOTALS-shaped snapshot like ApiFootballCollector now ingests
    # could never produce a signal, no matter how good the arbitrage,
    # because nothing ever asked detect_surebet_candidates to look at
    # TOTALS_2_5_MARKET. This is the "API-Football TOTALS snapshot ->
    # run_detection() -> TOTALS signal can be created" proof.
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("DB_PATH", ":memory:")
    cfg = config.load_config()
    runtime = build_runtime(cfg)

    start_time = datetime.fromisoformat("2026-09-08T00:15:00+00:00")
    match = FixtureCatalog(runtime.connection, provider_id="api-football").match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=start_time,
    )
    event = match.event
    observed_at = datetime.fromisoformat("2026-09-07T12:01:17+00:00")
    _save_totals_surebet_snapshots(runtime.odds_repository, event.id, observed_at=observed_at)

    summary = pipeline.run_detection(runtime, [event], cfg)

    assert summary["active_surebets"] == 1
    active = runtime.signal_repository.find_active("SUREBET")
    assert len(active) == 1
    assert active[0].market.market_type == MarketType.TOTALS
    assert active[0].market.line == Decimal("2.5")
