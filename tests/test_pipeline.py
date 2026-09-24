import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import anomaly_detection_engine.config as config
import anomaly_detection_engine.pipeline as pipeline
from anomaly_detection_engine.analysis.freshness import FreshnessPolicy
from anomaly_detection_engine.collectors.api_football_collector import (
    ApiFootballCollector,
    ApiFootballError,
)
from anomaly_detection_engine.collectors.json_collector import JsonOddsCollector
from anomaly_detection_engine.collectors.meridianbet_file_collector import (
    MeridianbetFileCollector,
)
from anomaly_detection_engine.collectors.mozzart_file_collector import MozzartFileCollector
from anomaly_detection_engine.collectors.the_odds_api_collector import (
    TheOddsApiCollector,
    TheOddsApiManualCollector,
)
from anomaly_detection_engine.models.collector_run import CollectorRun, CollectorRunStatus
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.market import (
    DEFAULT_MARKET,
    LIVE_MARKET,
    TOTALS_2_5_MARKET,
    EventLifecycle,
    MarketIdentity,
    MarketPeriod,
    MarketPhase,
    MarketType,
)
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.runtime import build_runtime
from anomaly_detection_engine.storage.collector_run_repository import CollectorRunRepository
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
        "MOZZART_PREMATCH_CAPTURE_DIR",
        "MERIDIANBET_MODE",
        "MERIDIANBET_CAPTURE_DIR",
        "API_FOOTBALL_KEY",
        "ODDS_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_national_team_acronym_alias_unifies_two_providers_same_match():
    # Regression test for a real cross-provider matching gap: Mozzart
    # reports "UAE M23" (league "Azijske igre M23"), Meridianbet reports
    # "United Arab Emirates U23" (league "Azijske Igre U23"), for the
    # exact same real Asian Games U23 match -- resolved to two separate
    # canonical events until pipeline.TOKEN_ALIASES ("UAE" -> "United Arab
    # Emirates") and pipeline.LEAGUE_ALIASES (an explicit "these are the
    # same league" entry -- a one-off tournament name isn't worth teaching
    # the matcher to handle algorithmically) were both added. Fuzzy
    # matching alone (token_sort_ratio) can never bridge an acronym (the
    # two strings share almost no characters), and pipeline.ALIASES can't
    # either, since it only ever matches a raw name that is *entirely* one
    # of its keys -- "UAE" as a key there never matches within the longer
    # string "UAE M23". Two separate FixtureCatalog instances sharing one
    # connection, mirroring exactly how run_ingestion wires one per
    # collector, using the exact real spellings both captures reported.
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    start_time = datetime.fromisoformat("2026-09-16T10:00:00+00:00")

    mozzart_catalog = FixtureCatalog(
        connection,
        provider_id="mozzart",
        aliases=pipeline.ALIASES,
        token_aliases=pipeline.TOKEN_ALIASES,
        league_aliases=pipeline.LEAGUE_ALIASES,
    )
    meridianbet_catalog = FixtureCatalog(
        connection,
        provider_id="meridianbet",
        aliases=pipeline.ALIASES,
        token_aliases=pipeline.TOKEN_ALIASES,
        league_aliases=pipeline.LEAGUE_ALIASES,
    )

    mozzart_result = mozzart_catalog.match(
        sport="football", league="Azijske igre M23",
        home_team_raw="UAE M23", away_team_raw="Iran M23",
        start_time=start_time,
    )
    meridianbet_result = meridianbet_catalog.match(
        sport="football", league="Azijske Igre U23",
        home_team_raw="United Arab Emirates U23", away_team_raw="Iran U23",
        start_time=start_time,
    )

    assert mozzart_result.event is not None
    assert meridianbet_result.event is not None
    assert mozzart_result.event.id == meridianbet_result.event.id


def test_country_qualified_epl_names_unify_all_three_providers():
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    start_time = datetime.fromisoformat("2026-09-27T15:00:00+00:00")

    def catalog(provider_id):
        return FixtureCatalog(
            connection,
            provider_id=provider_id,
            aliases=pipeline.ALIASES,
            token_aliases=pipeline.TOKEN_ALIASES,
            league_aliases=pipeline.LEAGUE_ALIASES,
        )

    api = catalog("api-football").match(
        sport="football", league="England - Premier League", country="England",
        home_team_raw="Arsenal", away_team_raw="Liverpool", start_time=start_time,
        source_event_id="af-1", home_team_source_id="42",
        away_team_source_id="40", competition_source_id="39",
    )
    odds_api = catalog("the-odds-api").match(
        sport="football", league="EPL",
        home_team_raw="Arsenal", away_team_raw="Liverpool", start_time=start_time,
        source_event_id="toa-1", competition_source_id="soccer_epl",
    )
    meridian = catalog("meridianbet").match(
        sport="football", league="Engleska - Premier Liga", country="Engleska",
        home_team_raw="Arsenal FC", away_team_raw="Liverpool", start_time=start_time,
        source_event_id="m-1", home_team_source_id="420355",
        away_team_source_id="263461", competition_source_id="2505",
    )

    assert {api.event.id, odds_api.event.id, meridian.event.id} == {api.event.id}
    assert api.event.league == "England - Premier League"
    assert connection.execute("SELECT COUNT(*) FROM competitions").fetchone()[0] == 1


def test_club_suffix_alias_unifies_two_providers_same_match():
    # Regression test for a real cross-provider matching gap found live:
    # Meridianbet reports "Arsenal FC" and "Lille OSC" for a Champions
    # League fixture the-odds-api/Mozzart report as bare "Arsenal" and
    # "Lille" -- token_sort_ratio scores the club-suffix difference below
    # fuzzy_threshold, so each side created its own separate canonical
    # team until pipeline.ALIASES got an explicit entry for each name
    # (see that dict's own comment).
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    start_time = datetime.fromisoformat("2026-10-13T19:00:00+00:00")

    meridianbet_catalog = FixtureCatalog(
        connection,
        provider_id="meridianbet",
        aliases=pipeline.ALIASES,
        token_aliases=pipeline.TOKEN_ALIASES,
        league_aliases=pipeline.LEAGUE_ALIASES,
    )
    mozzart_catalog = FixtureCatalog(
        connection,
        provider_id="mozzart",
        aliases=pipeline.ALIASES,
        token_aliases=pipeline.TOKEN_ALIASES,
        league_aliases=pipeline.LEAGUE_ALIASES,
    )

    meridianbet_result = meridianbet_catalog.match(
        sport="football", league="Liga Šampiona",
        home_team_raw="Arsenal FC", away_team_raw="Lille OSC",
        start_time=start_time,
    )
    mozzart_result = mozzart_catalog.match(
        sport="football", league="Liga Šampiona",
        home_team_raw="Arsenal", away_team_raw="Lille",
        start_time=start_time,
    )

    assert meridianbet_result.event is not None
    assert mozzart_result.event is not None
    assert meridianbet_result.event.id == mozzart_result.event.id


def test_transliteration_alias_unifies_two_providers_same_match():
    # A different category from the club-suffix cases above: the same
    # small Israeli town's club spelled via two unrelated
    # transliteration conventions (Arabic-native "Baqa Al-Gharbiyye" vs
    # Hebrew-route "Ironi Baka El Garbiya") -- token_sort_ratio can't
    # bridge this either, verified live via a shared opponent + identical
    # kickoff on both sides.
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    start_time = datetime.fromisoformat("2026-09-23T17:45:00+00:00")

    meridianbet_catalog = FixtureCatalog(
        connection,
        provider_id="meridianbet",
        aliases=pipeline.ALIASES,
        token_aliases=pipeline.TOKEN_ALIASES,
        league_aliases=pipeline.LEAGUE_ALIASES,
    )
    api_football_catalog = FixtureCatalog(
        connection,
        provider_id="api-football",
        aliases=pipeline.ALIASES,
        token_aliases=pipeline.TOKEN_ALIASES,
        league_aliases=pipeline.LEAGUE_ALIASES,
    )

    meridianbet_result = meridianbet_catalog.match(
        sport="football", league="Liga Alef",
        home_team_raw="Baqa Al-Gharbiyye", away_team_raw="Hapoel Tirat HaCarmel",
        start_time=start_time,
    )
    api_football_result = api_football_catalog.match(
        sport="football", league="Liga Alef",
        home_team_raw="Ironi Baka El Garbiya", away_team_raw="Hapoel Tirat HaCarmel",
        start_time=start_time,
    )

    assert meridianbet_result.event is not None
    assert api_football_result.event is not None
    assert meridianbet_result.event.id == api_football_result.event.id


def test_batch_club_name_aliases_have_the_expected_targets():
    # A plain content check for the rest of the batch added alongside the
    # two end-to-end tests above -- each was individually verified live
    # (same competition_id + identical kickoff + a shared opponent on the
    # unaliased side) before being added; this only guards against the
    # dict itself being accidentally edited later, not the underlying
    # matching mechanism (already covered end-to-end above and by
    # TeamNormalizer's own tests).
    expected = {
        "Galatasaray Istanbul": "Galatasaray",
        "Inter Milano": "Inter",
        "Viking FK": "Viking",
        "Fenerbahce Istanbul": "Fenerbahce",
        "PSG": "Paris Saint-Germain",
        "VfB Stuttgart": "Stuttgart",
        "CSD Xelaju MC": "Xelajú",
        "MS Tira": "Tira",
        "Independiente Santa Fe": "Santa Fe",
        "Aguilas Doradas Rionegro": "Águilas Doradas",
        "Turks&Caicos Islands": "Turks and Caicos Islands",
        "Saint Martin": "Saint-Martin",
        "Antigva & Barbuda": "Antigua and Barbuda",
        "Atletico Fenix": "CA Fenix Montevideo",
        "Colon FC": "Colon Montevideo",
        "CS Cerrito": "Cerrito",
        "MS Ashdod": "Ashdod",
        "MS Football Hapoel Kiryat Yam": "Kiryat Yam",
        "Guadalupe": "Guadeloupe",
    }
    for key, value in expected.items():
        assert pipeline.ALIASES[key] == value
    # Deliberately excluded: a live duplicate exists for "CA Cerro", but
    # its target ("Club Atletico Cerro") is itself contaminated with an
    # unrelated fixture, so no alias was added for it.
    assert "CA Cerro" not in pipeline.ALIASES


def test_ireland_and_iceland_resolve_to_different_teams_once_both_exist():
    # Regression test for a real, still-live false positive: bare
    # "Iceland" scores 85.71 against "Ireland" via token_sort_ratio --
    # above fuzzy_threshold, and neither name has a digit for the digit
    # guard to gate on -- so whichever of the two is sighted *second*
    # used to fuzzy-merge into the other instead of creating its own
    # team, since only one of the two genuine canonical rows existed yet
    # to exact-match against (verified live: every "Iceland" sighting
    # merged into "Ireland" this way). Both rows already existing
    # simultaneously (the end state of the one-off data fix that split
    # them back apart live) is what actually closes this -- each new
    # sighting then takes the ordinary exact-match path first and never
    # reaches fuzzy matching at all.
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    connection.execute(
        "INSERT INTO teams (id, canonical_name, sport) VALUES "
        "('team-ireland-t', 'Ireland', 'football'), "
        "('team-iceland-t', 'Iceland', 'football')"
    )
    connection.commit()

    catalog = FixtureCatalog(
        connection,
        provider_id="meridianbet",
        aliases=pipeline.ALIASES,
        token_aliases=pipeline.TOKEN_ALIASES,
        league_aliases=pipeline.LEAGUE_ALIASES,
    )

    ireland_result = catalog.match(
        sport="football", league="Liga Nacija",
        home_team_raw="Kosovo", away_team_raw="Ireland",
        start_time=datetime.fromisoformat("2026-09-24T18:45:00+00:00"),
    )
    iceland_result = catalog.match(
        sport="football", league="Liga Nacija",
        home_team_raw="Iceland", away_team_raw="Estonia",
        start_time=datetime.fromisoformat("2026-09-26T16:00:00+00:00"),
    )

    assert ireland_result.event.away_team.id == "team-ireland-t"
    assert iceland_result.event.home_team.id == "team-iceland-t"


def test_ireland_spelling_aliases_unify_three_providers_same_match():
    # Regression test for the *other* fragmentation the Ireland/Iceland
    # investigation turned up: Meridianbet's bare "Ireland", Mozzart's
    # "Republic Of Ireland", and api-football's "Rep. Of Ireland" are
    # the same real senior national team, spelled three different ways.
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    start_time = datetime.fromisoformat("2026-09-24T18:45:00+00:00")

    catalogs = {
        provider_id: FixtureCatalog(
            connection,
            provider_id=provider_id,
            aliases=pipeline.ALIASES,
            token_aliases=pipeline.TOKEN_ALIASES,
            league_aliases=pipeline.LEAGUE_ALIASES,
        )
        for provider_id in ("meridianbet", "mozzart", "api-football")
    }

    results = {
        "meridianbet": catalogs["meridianbet"].match(
            sport="football", league="Liga Nacija",
            home_team_raw="Kosovo", away_team_raw="Ireland", start_time=start_time,
        ),
        "mozzart": catalogs["mozzart"].match(
            sport="football", league="Liga nacija (B) - Evropa",
            home_team_raw="Kosovo", away_team_raw="Republic Of Ireland", start_time=start_time,
        ),
        "api-football": catalogs["api-football"].match(
            sport="football", league="UEFA Nations League",
            home_team_raw="Kosovo", away_team_raw="Rep. Of Ireland", start_time=start_time,
        ),
    }

    team_ids = {r.event.away_team.id for r in results.values()}
    assert len(team_ids) == 1


def test_mls_league_alias_unifies_two_providers_same_match():
    # Regression test for a real cross-provider gap found live: Mozzart
    # reports MLS as "SAD - MLS" (Serbian for "USA - MLS") and
    # Meridianbet reports it as "MLS Liga" -- neither string shares
    # enough characters with api-football's own "Major League Soccer" for
    # token_sort_ratio to ever bridge them, so the same real fixture
    # resolved to two separate canonical events until
    # pipeline.LEAGUE_ALIASES explicitly said so. Each entry was verified
    # against real fixtures on both sides before being added, not just
    # guessed from the league name alone -- see LEAGUE_ALIASES' own
    # comment for why that distinction matters. EPL and Serie A aliases use
    # API-Football's country-qualified names; its bare league names are not
    # globally unique and must not be treated as England/Italy by default.
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    start_time = datetime.fromisoformat("2026-10-10T11:30:00+00:00")

    mozzart_catalog = FixtureCatalog(
        connection,
        provider_id="mozzart",
        aliases=pipeline.ALIASES,
        token_aliases=pipeline.TOKEN_ALIASES,
        league_aliases=pipeline.LEAGUE_ALIASES,
    )
    meridianbet_catalog = FixtureCatalog(
        connection,
        provider_id="meridianbet",
        aliases=pipeline.ALIASES,
        token_aliases=pipeline.TOKEN_ALIASES,
        league_aliases=pipeline.LEAGUE_ALIASES,
    )

    mozzart_result = mozzart_catalog.match(
        sport="football", league="SAD - MLS",
        home_team_raw="Inter Miami", away_team_raw="Orlando City SC",
        start_time=start_time,
    )
    meridianbet_result = meridianbet_catalog.match(
        sport="football", league="MLS Liga",
        home_team_raw="Inter Miami", away_team_raw="Orlando City SC",
        start_time=start_time,
    )

    assert mozzart_result.event is not None
    assert meridianbet_result.event is not None
    assert mozzart_result.event.id == meridianbet_result.event.id


def test_epl_league_alias_unifies_the_odds_api_and_meridianbet():
    # Both provider spellings resolve to API-Football's country-qualified
    # canonical league name even when the reference feed arrives later.
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    start_time = datetime.fromisoformat("2026-10-10T11:30:00+00:00")

    odds_api_catalog = FixtureCatalog(
        connection,
        provider_id="the-odds-api",
        aliases=pipeline.ALIASES,
        token_aliases=pipeline.TOKEN_ALIASES,
        league_aliases=pipeline.LEAGUE_ALIASES,
    )
    meridianbet_catalog = FixtureCatalog(
        connection,
        provider_id="meridianbet",
        aliases=pipeline.ALIASES,
        token_aliases=pipeline.TOKEN_ALIASES,
        league_aliases=pipeline.LEAGUE_ALIASES,
    )

    odds_api_result = odds_api_catalog.match(
        sport="football", league="EPL",
        home_team_raw="Arsenal", away_team_raw="Chelsea",
        start_time=start_time,
    )
    meridianbet_result = meridianbet_catalog.match(
        sport="football", league="Premier Liga",
        home_team_raw="Arsenal", away_team_raw="Chelsea",
        start_time=start_time,
    )

    assert odds_api_result.event is not None
    assert meridianbet_result.event is not None
    assert odds_api_result.event.id == meridianbet_result.event.id


def test_serie_a_league_alias_unifies_meridianbet_and_mozzart():
    # Both provider spellings resolve to API-Football's country-qualified
    # canonical league name even when the reference feed arrives later.
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    start_time = datetime.fromisoformat("2026-10-10T11:30:00+00:00")

    meridianbet_catalog = FixtureCatalog(
        connection,
        provider_id="meridianbet",
        aliases=pipeline.ALIASES,
        token_aliases=pipeline.TOKEN_ALIASES,
        league_aliases=pipeline.LEAGUE_ALIASES,
    )
    mozzart_catalog = FixtureCatalog(
        connection,
        provider_id="mozzart",
        aliases=pipeline.ALIASES,
        token_aliases=pipeline.TOKEN_ALIASES,
        league_aliases=pipeline.LEAGUE_ALIASES,
    )

    meridianbet_result = meridianbet_catalog.match(
        sport="football", league="Serija A",
        home_team_raw="Inter Milano", away_team_raw="AC Milan",
        start_time=start_time,
    )
    mozzart_result = mozzart_catalog.match(
        sport="football", league="Italija 1",
        home_team_raw="Inter Milano", away_team_raw="AC Milan",
        start_time=start_time,
    )

    assert meridianbet_result.event is not None
    assert mozzart_result.event is not None
    assert meridianbet_result.event.id == mozzart_result.event.id


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
        def __init__(self, sport_key, api_key=None):
            self._sport_key = sport_key
            self._api_key = api_key

        @property
        def source(self):
            return f"the-odds-api:{self._sport_key}"

    monkeypatch.setattr(pipeline, "TheOddsApiCollector", FakeLiveCollector)

    collectors = pipeline.build_collectors(config.load_config())

    assert len(collectors) == 1
    assert collectors[0].source == "the-odds-api:soccer_epl"


def test_odds_api_key_from_app_config_is_passed_to_the_collector(monkeypatch):
    # ODDS_API_KEY flows through AppConfig -> _the_odds_api_collector,
    # the same config-boundary shape api_football_key already has --
    # not read directly from os.environ by pipeline.py itself.
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_SOURCE", "the-odds-api")
    monkeypatch.setenv("ODDS_SPORT_KEY", "soccer_epl")
    monkeypatch.setenv("ODDS_API_KEY", "test-odds-api-key")

    class FakeLiveCollector:
        def __init__(self, sport_key, api_key=None):
            self._sport_key = sport_key
            self.api_key = api_key

        @property
        def source(self):
            return f"the-odds-api:{self._sport_key}"

    monkeypatch.setattr(pipeline, "TheOddsApiCollector", FakeLiveCollector)

    collectors = pipeline.build_collectors(config.load_config())

    assert collectors[0].api_key == "test-odds-api-key"
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


def test_meridianbet_capture_dir_adds_a_supplemental_collector(monkeypatch, tmp_path):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("MERIDIANBET_CAPTURE_DIR", str(tmp_path))

    collectors = pipeline.build_collectors(config.load_config())

    assert len(collectors) == 3
    assert isinstance(collectors[2], MeridianbetFileCollector)
    assert collectors[2].capture_dir == tmp_path


def test_meridianbet_mode_defaults_to_manual_explicitly(monkeypatch, tmp_path):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("MERIDIANBET_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIANBET_MODE", "manual")

    collectors = pipeline.build_collectors(config.load_config())

    assert isinstance(collectors[2], MeridianbetFileCollector)


def test_meridianbet_mode_auto_is_rejected(monkeypatch, tmp_path):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("MERIDIANBET_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIANBET_MODE", "auto")

    with pytest.raises(ValueError, match="MERIDIANBET_MODE"):
        pipeline.build_collectors(config.load_config())


def test_no_meridianbet_capture_dir_means_no_supplemental_collector(monkeypatch):
    _clear_source_env(monkeypatch)

    collectors = pipeline.build_collectors(config.load_config())

    assert not any(isinstance(c, MeridianbetFileCollector) for c in collectors)


def test_mozzart_prematch_capture_dir_adds_a_second_collector(monkeypatch, tmp_path):
    # Two separate directories exist to avoid a real overwrite race: the
    # capture tooling saves every Mozzart response under the same
    # default filename ("live.json") regardless of phase, so a live and
    # a pre-match capture landing in the same directory close together
    # can clobber each other before the poller reads either one.
    _clear_source_env(monkeypatch)
    live_dir = tmp_path / "mozzart-live"
    prematch_dir = tmp_path / "mozzart-prematch"
    monkeypatch.setenv("MOZZART_CAPTURE_DIR", str(live_dir))
    monkeypatch.setenv("MOZZART_PREMATCH_CAPTURE_DIR", str(prematch_dir))

    collectors = pipeline.build_collectors(config.load_config())

    assert len(collectors) == 4
    assert isinstance(collectors[2], MozzartFileCollector)
    assert isinstance(collectors[3], MozzartFileCollector)
    assert collectors[2].capture_dir == live_dir
    assert collectors[3].capture_dir == prematch_dir


def test_mozzart_prematch_capture_dir_alone_works_without_the_live_one(monkeypatch, tmp_path):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("MOZZART_PREMATCH_CAPTURE_DIR", str(tmp_path))

    collectors = pipeline.build_collectors(config.load_config())

    assert len(collectors) == 3
    assert isinstance(collectors[2], MozzartFileCollector)
    assert collectors[2].capture_dir == tmp_path


def test_mozzart_prematch_mode_auto_is_rejected(monkeypatch, tmp_path):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("MOZZART_PREMATCH_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setenv("MOZZART_MODE", "auto")

    with pytest.raises(ValueError, match="MOZZART_MODE"):
        pipeline.build_collectors(config.load_config())


def test_mozzart_and_meridianbet_can_both_be_active_at_once(monkeypatch, tmp_path):
    _clear_source_env(monkeypatch)
    mozzart_dir = tmp_path / "mozzart"
    meridianbet_dir = tmp_path / "meridianbet"
    monkeypatch.setenv("MOZZART_CAPTURE_DIR", str(mozzart_dir))
    monkeypatch.setenv("MERIDIANBET_CAPTURE_DIR", str(meridianbet_dir))

    collectors = pipeline.build_collectors(config.load_config())

    assert len(collectors) == 4
    assert isinstance(collectors[2], MozzartFileCollector)
    assert isinstance(collectors[3], MeridianbetFileCollector)


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


def test_api_football_source_used_as_primary_when_configured(monkeypatch):
    # ODDS_SOURCE=api-football exists so a deployment with only an
    # API_FOOTBALL_KEY (no the-odds-api key) can build a dataset made
    # entirely of real observations, without the JSON demo's synthetic
    # primary collectors also landing in the same database.
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_SOURCE", "api-football")
    monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")

    collectors = pipeline.build_collectors(config.load_config())

    assert len(collectors) == 1
    assert isinstance(collectors[0], ApiFootballCollector)
    assert collectors[0].provider_id == "api-football"


def test_api_football_source_without_key_raises(monkeypatch):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_SOURCE", "api-football")

    with pytest.raises(ApiFootballError):
        pipeline.build_collectors(config.load_config())


def test_api_football_primary_is_not_also_added_as_supplemental(monkeypatch, tmp_path):
    # Without the odds_source != "api-football" guard in
    # _supplemental_collectors, this would build two ApiFootballCollector
    # instances and poll the same endpoint twice every cycle.
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_SOURCE", "api-football")
    monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
    monkeypatch.setenv("MOZZART_CAPTURE_DIR", str(tmp_path))

    collectors = pipeline.build_collectors(config.load_config())

    assert len(collectors) == 2
    assert isinstance(collectors[0], ApiFootballCollector)
    assert isinstance(collectors[1], MozzartFileCollector)


def _run_repository() -> CollectorRunRepository:
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    return CollectorRunRepository(connection)


def test_no_collector_run_repository_means_no_the_odds_api_supplemental(monkeypatch):
    # collector_run_repository defaults to None -- the-odds-api
    # supplemental is skipped entirely regardless of ODDS_API_KEY, not
    # just its rate-limit check, since there's nothing to check it
    # against. Every real call site (run_ingestion) always supplies one.
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")

    collectors = pipeline.build_collectors(config.load_config())

    assert not any(isinstance(c, TheOddsApiCollector) for c in collectors)


def test_odds_api_key_adds_a_supplemental_collector_on_first_ever_run(monkeypatch):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")

    collectors = pipeline.build_collectors(config.load_config(), _run_repository())

    odds_api = [c for c in collectors if isinstance(c, TheOddsApiCollector)]
    assert len(odds_api) == 1
    assert odds_api[0].source == "the-odds-api:soccer_epl"


def test_no_odds_api_key_means_no_the_odds_api_supplemental(monkeypatch):
    _clear_source_env(monkeypatch)

    collectors = pipeline.build_collectors(config.load_config(), _run_repository())

    assert not any(isinstance(c, TheOddsApiCollector) for c in collectors)


def test_the_odds_api_primary_is_not_also_added_as_supplemental(monkeypatch):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_SOURCE", "the-odds-api")
    monkeypatch.setenv("ODDS_API_KEY", "test-key")

    collectors = pipeline.build_collectors(config.load_config(), _run_repository())

    assert len(collectors) == 1


def test_the_odds_api_supplemental_is_skipped_within_the_min_interval(monkeypatch):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    monkeypatch.setenv("ODDS_API_MIN_INTERVAL_HOURS", "4")
    repository = _run_repository()

    now = datetime.fromisoformat("2026-09-15T12:00:00+00:00")
    repository.save(
        CollectorRun(
            id="run-1",
            source="the-odds-api:soccer_epl",
            started_at=now - timedelta(hours=1),
            finished_at=now - timedelta(hours=1),
            status=CollectorRunStatus.SUCCESS,
            records_received=0,
            records_accepted=0,
            records_rejected=0,
            collector_version="0.1.0",
        )
    )

    collectors = pipeline.build_collectors(config.load_config(), repository, now=now)

    assert not any(isinstance(c, TheOddsApiCollector) for c in collectors)


def test_the_odds_api_supplemental_resumes_once_the_min_interval_has_passed(monkeypatch):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    monkeypatch.setenv("ODDS_API_MIN_INTERVAL_HOURS", "4")
    repository = _run_repository()

    now = datetime.fromisoformat("2026-09-15T12:00:00+00:00")
    repository.save(
        CollectorRun(
            id="run-1",
            source="the-odds-api:soccer_epl",
            started_at=now - timedelta(hours=5),
            finished_at=now - timedelta(hours=5),
            status=CollectorRunStatus.SUCCESS,
            records_received=0,
            records_accepted=0,
            records_rejected=0,
            collector_version="0.1.0",
        )
    )

    collectors = pipeline.build_collectors(config.load_config(), repository, now=now)

    assert any(isinstance(c, TheOddsApiCollector) for c in collectors)


def test_the_odds_api_supplemental_counts_a_failed_run_against_the_interval_too(monkeypatch):
    # The gate is about spacing out requests actually sent, not just
    # successful ones -- a FAILED run still consumed part of the free
    # tier's request budget.
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    monkeypatch.setenv("ODDS_API_MIN_INTERVAL_HOURS", "4")
    repository = _run_repository()

    now = datetime.fromisoformat("2026-09-15T12:00:00+00:00")
    repository.save(
        CollectorRun(
            id="run-1",
            source="the-odds-api:soccer_epl",
            started_at=now - timedelta(hours=1),
            finished_at=now - timedelta(hours=1),
            status=CollectorRunStatus.FAILED,
            records_received=0,
            records_accepted=0,
            records_rejected=0,
            collector_version="0.1.0",
        )
    )

    collectors = pipeline.build_collectors(config.load_config(), repository, now=now)

    assert not any(isinstance(c, TheOddsApiCollector) for c in collectors)


def test_run_detection_uses_wall_clock_time_for_any_non_demo_source(monkeypatch):
    # analysis_time must be real wall-clock "now" for every real source,
    # not just the-odds-api -- checked as odds_source != "demo" (see
    # run_detection) so api-football (and any future real source) gets
    # this right automatically instead of needing this condition
    # remembered by hand. Proven with an old observed_at: under
    # demo_analysis_time an old snapshot looks "fresh" (analysis_time is
    # computed from the newest observation in the batch); under real
    # wall-clock time the same snapshot is correctly rejected as stale,
    # so no surebet is created at all.
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setenv("ODDS_SOURCE", "api-football")
    cfg = config.load_config()
    runtime = build_runtime(cfg)

    ancient_start = datetime.fromisoformat("2000-01-01T00:00:00+00:00")
    match = FixtureCatalog(runtime.connection, provider_id="test").match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=ancient_start,
    )
    event = match.event
    _save_surebet_snapshots(runtime.odds_repository, event.id, observed_at=ancient_start)

    summary = pipeline.run_detection(runtime, [event], cfg)

    assert summary["active_surebets"] == 0


def test_resolve_freshness_policy_uses_demo_policy_for_demo_source(monkeypatch):
    _clear_source_env(monkeypatch)
    cfg = config.load_config()

    assert pipeline.resolve_freshness_policy(cfg) is pipeline.DEMO_FRESHNESS_POLICY


def test_resolve_freshness_policy_uses_configured_thresholds_for_a_real_source(monkeypatch):
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("ODDS_SOURCE", "api-football")
    monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
    monkeypatch.setenv("MAX_QUOTE_AGE_MINUTES", "90")
    monkeypatch.setenv("MAX_QUOTE_SPREAD_MINUTES", "45")
    cfg = config.load_config()

    policy = pipeline.resolve_freshness_policy(cfg)

    assert policy.max_snapshot_age == timedelta(minutes=90)
    assert policy.max_observation_spread == timedelta(minutes=45)


def test_run_detection_uses_the_configured_production_freshness_window(monkeypatch):
    # A quote 40 minutes old would have failed the old hardcoded 5-minute
    # DEMO_FRESHNESS_POLICY every real source used to be evaluated
    # against -- with MAX_QUOTE_AGE_MINUTES configured wide enough, the
    # same real-source surebet must now actually be detected.
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setenv("ODDS_SOURCE", "api-football")
    monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
    monkeypatch.setenv("MAX_QUOTE_AGE_MINUTES", "60")
    monkeypatch.setenv("MAX_QUOTE_SPREAD_MINUTES", "30")
    cfg = config.load_config()
    runtime = build_runtime(cfg)

    match = FixtureCatalog(runtime.connection, provider_id="api-football").match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=datetime.now(UTC) + timedelta(days=1),
    )
    event = match.event
    quote_time = datetime.now(UTC) - timedelta(minutes=40)
    _save_surebet_snapshots(runtime.odds_repository, event.id, observed_at=quote_time)

    summary = pipeline.run_detection(runtime, [event], cfg)

    assert summary["active_surebets"] == 1


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


def test_run_detection_excludes_a_finished_event_from_detection(monkeypatch):
    # A FINISHED event's pre-match odds are never a meaningful surebet
    # candidate again, real status or not -- run_detection must filter
    # it out before persist_detected_signals ever sees it, not merely
    # expire an already-created signal after the fact.
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("DB_PATH", ":memory:")
    cfg = config.load_config()
    runtime = build_runtime(cfg)

    quote_time = datetime.now(UTC) - timedelta(minutes=1)
    match = FixtureCatalog(runtime.connection, provider_id="test").match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=quote_time,
    )
    event = match.event
    _save_surebet_snapshots(runtime.odds_repository, event.id, observed_at=quote_time)
    runtime.event_status_repository.update(
        event_id=event.id, lifecycle=EventLifecycle.FINISHED, updated_at=quote_time,
    )

    result = pipeline.run_detection(runtime, [event], cfg)

    assert result["active_surebets"] == 0
    assert runtime.signal_repository.find_active("SUREBET") == []


def test_run_detection_still_detects_a_live_events_surebet(monkeypatch):
    # LIVE is not a terminal status -- only FINISHED/POSTPONED_OR_CANCELED
    # exclude an event from detection (see EventLifecycle's docstring).
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("DB_PATH", ":memory:")
    cfg = config.load_config()
    runtime = build_runtime(cfg)

    quote_time = datetime.now(UTC) - timedelta(minutes=1)
    match = FixtureCatalog(runtime.connection, provider_id="test").match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=quote_time,
    )
    event = match.event
    _save_surebet_snapshots(runtime.odds_repository, event.id, observed_at=quote_time)
    runtime.event_status_repository.update(
        event_id=event.id, lifecycle=EventLifecycle.LIVE, updated_at=quote_time,
    )

    result = pipeline.run_detection(runtime, [event], cfg)

    assert result["active_surebets"] == 1


def _save_totals_surebet_snapshots(odds_repository, event_id, observed_at):
    # Shaped like what ApiFootballCollector actually extracts from a real
    # "Goals Over/Under" bet's 2.5 line (see
    # collectors.api_football_collector._extract_totals_2_5_odds) --
    # OVER/UNDER only, no 1/X/2 -- proving run_detection's dynamic market
    # discovery (OddsRepository.distinct_markets_for_events) reaches
    # TOTALS_2_5_MARKET, not just DEFAULT_MARKET.
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
    # Before DETECTED_MARKETS existed (a since-removed hand-maintained
    # tuple in pipeline.py, replaced by dynamic discovery -- see
    # test_run_detection_discovers_a_market_never_named_by_any_constant),
    # run_detection() only ever called persist_detected_signals with
    # market=DEFAULT_MARKET --
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


def _save_live_surebet_snapshots(odds_repository, event_id, observed_at):
    # Shaped like what MozzartFileCollector actually extracts for an
    # in-play match (see
    # mozzart_file_collector._resolve_market_and_lifecycle) -- market=
    # LIVE_MARKET, not DEFAULT_MARKET.
    for outcome, odds in [("1", "2.50"), ("X", "4.00"), ("2", "4.00")]:
        odds_repository.save(
            OddsSnapshot(
                event_id=event_id,
                bookmaker=Bookmaker("bet1", "Bet1"),
                market=LIVE_MARKET,
                outcome=outcome,
                odds=Decimal(odds),
                observed_at=observed_at,
            )
        )


def test_run_detection_detects_a_live_market_surebet_not_just_pre_match(monkeypatch):
    # Before LIVE_MARKET was added to the since-removed DETECTED_MARKETS
    # tuple, run_detection() never once called persist_detected_signals with
    # market=LIVE_MARKET -- a Mozzart live snapshot was ingested and
    # stored (see test_mozzart_file_collector.py) but could never produce
    # a signal, no matter how good the arbitrage, because nothing ever
    # asked detect_surebet_candidates to look at LIVE_MARKET. This is the
    # "Mozzart LIVE_MARKET snapshot -> run_detection() -> signal can be
    # created" proof, mirroring
    # test_run_detection_detects_a_totals_surebet_not_just_default_market
    # above for TOTALS_2_5_MARKET.
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("DB_PATH", ":memory:")
    cfg = config.load_config()
    runtime = build_runtime(cfg)

    quote_time = datetime.now(UTC) - timedelta(minutes=1)
    match = FixtureCatalog(runtime.connection, provider_id="mozzart").match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=quote_time,
    )
    event = match.event
    _save_live_surebet_snapshots(runtime.odds_repository, event.id, observed_at=quote_time)

    summary = pipeline.run_detection(runtime, [event], cfg)

    assert summary["active_surebets"] == 1
    active = runtime.signal_repository.find_active("SUREBET")
    assert len(active) == 1
    assert active[0].market.phase == LIVE_MARKET.phase


def test_run_detection_keeps_live_and_pre_match_surebets_for_the_same_event_separate(
    monkeypatch,
):
    # LIVE_MARKET and DEFAULT_MARKET are distinct MarketIdentity values
    # for the exact same event -- a pre-match 1X2 price and a live 1X2
    # price are never the same market (see models.market.MarketPhase), so
    # both should be independently detectable at once, not merged or
    # mutually exclusive.
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("DB_PATH", ":memory:")
    cfg = config.load_config()
    runtime = build_runtime(cfg)

    quote_time = datetime.now(UTC) - timedelta(minutes=1)
    match = FixtureCatalog(runtime.connection, provider_id="mozzart").match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=quote_time,
    )
    event = match.event
    _save_surebet_snapshots(runtime.odds_repository, event.id, observed_at=quote_time)
    _save_live_surebet_snapshots(runtime.odds_repository, event.id, observed_at=quote_time)

    summary = pipeline.run_detection(runtime, [event], cfg)

    assert summary["active_surebets"] == 2
    phases = {signal.market.phase for signal in runtime.signal_repository.find_active("SUREBET")}
    assert phases == {DEFAULT_MARKET.phase, LIVE_MARKET.phase}


def test_run_detection_discovers_a_market_never_named_by_any_constant(monkeypatch):
    # Regression test for the old DETECTED_MARKETS tuple's own failure
    # mode: LIVE_MARKET was ingested and stored correctly for a real
    # stretch of this project's history before anyone added it to that
    # hand-maintained list, so it was never detected. Markets are now
    # discovered from odds_snapshots itself (OddsRepository.
    # distinct_markets_for_events), not named one by one -- this uses a
    # MarketIdentity (THREE_WAY, FIRST_HALF) that no DEFAULT_MARKET/
    # TOTALS_2_5_MARKET/HANDICAP_MINUS_1_MARKET/LIVE_MARKET constant ever
    # named (all of those are FULL_TIME), proving detection reaches it
    # purely because the data says it's there.
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("DB_PATH", ":memory:")
    cfg = config.load_config()
    runtime = build_runtime(cfg)
    first_half_market = MarketIdentity(
        market_type=MarketType.THREE_WAY, period=MarketPeriod.FIRST_HALF,
        phase=MarketPhase.PRE_MATCH,
    )

    quote_time = datetime.now(UTC) - timedelta(minutes=1)
    match = FixtureCatalog(runtime.connection, provider_id="mozzart").match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=quote_time,
    )
    event = match.event
    for outcome, odds in [("1", "2.50"), ("X", "4.00"), ("2", "4.00")]:
        runtime.odds_repository.save(
            OddsSnapshot(
                event_id=event.id,
                bookmaker=Bookmaker("bet1", "Bet1"),
                market=first_half_market,
                outcome=outcome,
                odds=Decimal(odds),
                observed_at=quote_time,
            )
        )

    summary = pipeline.run_detection(runtime, [event], cfg)

    assert summary["active_surebets"] == 1
    active = runtime.signal_repository.find_active("SUREBET")
    assert active[0].market.period == MarketPeriod.FIRST_HALF


def test_run_detection_skips_an_unsupported_market_type_without_crashing(monkeypatch, caplog):
    # A market_type present in the data but with no
    # models.market.REQUIRED_OUTCOMES entry (MONEYLINE -- the enum value
    # exists but detection was never wired up for it) must be skipped
    # with a visible warning, not silently ignored and not a crash that
    # would also block detecting the *other*, genuinely supported market
    # in the very same cycle.
    _clear_source_env(monkeypatch)
    monkeypatch.setenv("DB_PATH", ":memory:")
    cfg = config.load_config()
    runtime = build_runtime(cfg)
    unsupported_market = MarketIdentity(
        market_type=MarketType.MONEYLINE, period=MarketPeriod.FULL_TIME,
        phase=MarketPhase.PRE_MATCH,
    )

    quote_time = datetime.now(UTC) - timedelta(minutes=1)
    match = FixtureCatalog(runtime.connection, provider_id="mozzart").match(
        sport="football", league="L", home_team_raw="A", away_team_raw="B",
        start_time=quote_time,
    )
    event = match.event
    runtime.odds_repository.save(
        OddsSnapshot(
            event_id=event.id,
            bookmaker=Bookmaker("bet1", "Bet1"),
            market=unsupported_market,
            outcome="1",
            odds=Decimal("2.50"),
            observed_at=quote_time,
        )
    )
    _save_surebet_snapshots(runtime.odds_repository, event.id, observed_at=quote_time)

    # The package logger (see observability.logging_config.configure_logging)
    # sets propagate=False once configured, which -- if any earlier test in
    # this same process already called it -- would otherwise keep this
    # record from ever reaching caplog's own handler on the root logger.
    # Attaching caplog's handler directly works regardless of that.
    package_logger = logging.getLogger("anomaly_detection_engine")
    package_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level("WARNING", logger="anomaly_detection_engine"):
            summary = pipeline.run_detection(runtime, [event], cfg)
    finally:
        package_logger.removeHandler(caplog.handler)

    assert summary["active_surebets"] == 1  # the supported DEFAULT_MARKET surebet still detected
    assert any(
        "unsupported_market_type_present" in record.message for record in caplog.records
    )
