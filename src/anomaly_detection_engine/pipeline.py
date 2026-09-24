import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from anomaly_detection_engine.analysis.freshness import FreshnessPolicy
from anomaly_detection_engine.analysis.movement_detection import detect_movements
from anomaly_detection_engine.analysis.opportunity_detection import (
    SurebetCandidate,
    ValueGapCandidate,
    detect_surebet_candidates,
    detect_value_gap_candidates,
)
from anomaly_detection_engine.collectors.api_football_collector import ApiFootballCollector
from anomaly_detection_engine.collectors.base import OddsCollector
from anomaly_detection_engine.collectors.json_collector import JsonOddsCollector
from anomaly_detection_engine.collectors.meridianbet_file_collector import (
    MeridianbetFileCollector,
)
from anomaly_detection_engine.collectors.mozzart_file_collector import MozzartFileCollector
from anomaly_detection_engine.collectors.the_odds_api_collector import (
    TheOddsApiCollector,
    TheOddsApiManualCollector,
)
from anomaly_detection_engine.config import AppConfig
from anomaly_detection_engine.ingestion.service import OddsIngestionService
from anomaly_detection_engine.models.event import Event
from anomaly_detection_engine.models.market import (
    REQUIRED_OUTCOMES,
    EventLifecycle,
    MarketIdentity,
)
from anomaly_detection_engine.models.signal import SUREBET, VALUE_GAP
from anomaly_detection_engine.runtime import Runtime
from anomaly_detection_engine.storage.bookmaker_catalog import BookmakerCatalog
from anomaly_detection_engine.storage.collector_run_repository import CollectorRunRepository
from anomaly_detection_engine.storage.fixture_catalog import FixtureCatalog
from anomaly_detection_engine.storage.movement_repository import MovementRepository
from anomaly_detection_engine.storage.odds_repository import OddsRepository
from anomaly_detection_engine.storage.signal_repository import SignalCandidate, SignalRepository

logger = logging.getLogger(__name__)

# Known variant spellings fed to every FixtureCatalog instance below --
# harmless where irrelevant (e.g. for Mozzart's own Serbian team names),
# and what still makes the JSON demo's "Man Utd"/"Man. United"/
# "Manchester United" rows resolve to one canonical team on first sight,
# same as before FixtureCatalog replaced the old fixed event list.
ALIASES = {
    "Man Utd": "Manchester United",
    "Man. United": "Manchester United",
    "Liverpool FC": "Liverpool",
    "Liv": "Liverpool",
    "FCB": "Barcelona",
    "Barca": "Barcelona",
    "Real Madrid CF": "Real Madrid",
    # Meridianbet appends a club-suffix ("FC"/"OSC"/...) that scores below
    # fuzzy_threshold against the bare name other sources use -- verified
    # live: "Arsenal FC" and "Lille OSC" (Meridianbet) each created their
    # own separate canonical team instead of merging into "Arsenal"/
    # "Lille" (the-odds-api/Mozzart's own spelling), splitting a shared
    # Champions League fixture into two events.
    "Arsenal FC": "Arsenal",
    "Lille OSC": "Lille",
    # Same club-suffix/prefix/abbreviation gap as above, found by
    # systematically comparing every Meridianbet/Mozzart team pair
    # sharing a competition_id+kickoff+one already-matching side --
    # each entry verified the same way (same real fixture, only this
    # club's own name differs). Target picked as whichever spelling an
    # existing provider already used elsewhere (not guessed), except a
    # bare acronym ("PSG"), which -- like "FCB"/"Barca" above -- maps to
    # the full name instead.
    "Sabah Masazir": "Sabah",
    "Galatasaray Istanbul": "Galatasaray",
    "Inter Milano": "Inter",
    "Viking FK": "Viking",
    "Fenerbahce Istanbul": "Fenerbahce",
    "PSG": "Paris Saint-Germain",
    "VfB Stuttgart": "Stuttgart",
    "CSD Xelaju MC": "Xelajú",
    "MS Tira": "Tira",
    "Ironi Baka El Garbiya": "Baqa Al-Gharbiyye",
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
    # "CA Cerro" deliberately NOT aliased here despite a verified live
    # duplicate (see docs) -- the Uruguayan "Club Atletico Cerro" bucket
    # it would target already has an unrelated Paraguayan fixture
    # ("Sevilla Atletico") wrongly merged into it from elsewhere, so
    # pointing more sightings at it isn't safe until that's untangled
    # first.
    #
    # These four exist for a different reason than every entry above:
    # not a spelling gap fuzzy matching fails to bridge, but the
    # opposite -- a single-letter-different country name pair
    # ("Ireland"/"Iceland") that token_sort_ratio scores *above*
    # fuzzy_threshold, verified live to have silently merged Iceland's
    # senior and U21 national teams into Ireland's (and vice versa for
    # the U21 side) from their very first sighting, since no genuine
    # "Iceland"/"Ireland U21" canonical row existed yet to exact-match
    # against first. Once those rows exist (see the one-off data fix
    # that split them back out), "Iceland" and "Ireland U21" resolve
    # correctly on their own via the ordinary exact-match path and need
    # no alias -- the entries below are for the *other* fragmentation
    # this same investigation turned up: three still-separate spellings
    # of Ireland's own senior team ("Ireland" from Meridianbet,
    # "Republic Of Ireland" from Mozzart, "Rep. Of Ireland" from
    # api-football) and a senior/U21 collision on two more teams, for a
    # subtly different reason than the country mix-up above: "Republic
    # Ireland M21"/"Northern Ireland M21" DO have a digit, so the digit
    # guard (see team_normalizer._digit_tokens) correctly refuses to
    # fuzzy-match them against the bare, digit-less senior team -- but
    # with no "...U21" canonical row existing yet either, that just left
    # them stuck at "unknown" (a new team every sighting) rather than
    # merging cleanly the way "Iceland M21" already does once "Iceland
    # U21" exists. An explicit alias, not the exact-match self-healing
    # the country fix relies on, since "Republic Ireland M21" and
    # "Ireland U21" aren't spelled closely enough for even a
    # digit-guard-safe fuzzy match to bridge on its own.
    "Republic Of Ireland": "Ireland",
    "Rep. Of Ireland": "Ireland",
    "Republic Ireland M21": "Ireland U21",
    "Northern Ireland M21": "Northern Ireland U21",
}

# Word-level, unlike ALIASES above: ALIASES only ever matches a raw name
# that is *entirely* one of its keys, so "UAE" as a key there could never
# match within "UAE M23" -- these are substituted one word at a time
# instead (see TeamNormalizer._expand_tokens), before ALIASES/fuzzy
# matching even runs. An acronym like "UAE" also shares essentially no
# characters with "United Arab Emirates", so no fuzzy scorer could ever
# bridge that gap either way. Confirmed live: Mozzart and Meridianbet
# reporting the exact same real Asian Games U23 match as "UAE M23" vs
# "United Arab Emirates U23" resolved to two separate canonical events
# until this was added. Only essentially unambiguous 3-letter codes are
# included -- unlike a club nickname, "UAE"/"USA" have no other plausible
# meaning in a football context, so this carries none of the false-merge
# risk a guessed club alias would.
TOKEN_ALIASES = {
    "UAE": "United Arab Emirates",
    "USA": "United States",
}

# Same shape as ALIASES above, but fed to FixtureCatalog's *competition*
# resolution instead of its team resolution. A one-off tournament like
# "Asian Games U23" is rare enough (unlike a club appearing in every
# season) that teaching the matcher to handle its naming quirks
# algorithmically isn't worth it -- a plain, explicit "these are the same
# league" entry is simpler and just as effective. Whichever spelling is
# used as the value here is what every variant resolves to, regardless of
# which provider's spelling a real run happens to see first -- unlike the
# fuzzy-match path, an explicit alias is not order-dependent.
LEAGUE_ALIASES = {
    "Azijske igre M23": "Azijske Igre U23",
    # Verified by checking each side's actual teams, not just the name --
    # a name-only guess here is exactly how "Primera Division" (Peru's
    # own top flight under api-football) could have been wrongly aliased
    # to Meridianbet's "La Liga" (Spain) if it had been. Every entry below
    # was confirmed by cross-referencing real fixtures on both sides.
    #
    # API-Football's bare "Premier League"/"Serie A" names are ambiguous
    # across countries, so canonical targets use its country-qualified
    # spelling. If
    # API-Football has not been ingested yet the other provider creates a
    # provisional row under this exact name; the later reference sighting
    # promotes that row instead of creating a parallel league.
    "EPL": "England - Premier League",
    "Premier Liga": "England - Premier League",
    "Engleska - Premier Liga": "England - Premier League",
    "Engleska 1": "England - Premier League",
    "Serija A": "Italy - Serie A",
    "Italija 1": "Italy - Serie A",
    "MLS Liga": "USA - Major League Soccer",
    "SAD - MLS Liga": "USA - Major League Soccer",
    "SAD - MLS": "USA - Major League Soccer",
    "Major League Soccer": "USA - Major League Soccer",
    "Liga Nacija": "UEFA Nations League",
    "Liga nacija (A) - Evropa": "UEFA Nations League",
    "Liga nacija (B) - Evropa": "UEFA Nations League",
    "Liga nacija (C) - Evropa": "UEFA Nations League",
    "Liga nacija (D) - Evropa": "UEFA Nations League",
}

# Demo dataset uses fixed calendar timestamps rather than live polling, so
# freshness is evaluated relative to the newest observation in the batch
# (not wall-clock "now", which would drift stale as real time passes).
# Deliberately fixed, not config-driven: this is a fact about the demo
# data's own tight timestamp clustering, not a per-deployment policy
# decision the way max_quote_age/max_observation_spread (AppConfig,
# used for every real source -- see resolve_freshness_policy) are.
DEMO_FRESHNESS_POLICY = FreshnessPolicy(
    max_snapshot_age=timedelta(minutes=5),
    max_observation_spread=timedelta(minutes=5),
)


def resolve_freshness_policy(config: AppConfig) -> FreshnessPolicy:
    """The FreshnessPolicy any given run should actually use: the fixed
    DEMO_FRESHNESS_POLICY for the JSON demo (see its own comment for
    why), or a policy built from AppConfig.max_quote_age/
    max_observation_spread for every real source -- both run_detection
    and reporting.console.print_reports call this rather than each
    picking a policy on their own, so the two can never silently drift
    apart on what "fresh" means for the same config.
    """
    if config.odds_source == "demo":
        return DEMO_FRESHNESS_POLICY

    return FreshnessPolicy(
        max_snapshot_age=config.max_quote_age,
        max_observation_spread=config.max_observation_spread,
    )



def _the_odds_api_collector(config: AppConfig) -> OddsCollector:
    """Builds the live-source primary collector according to ODDS_API_MODE.

    "auto" (default) fetches over HTTP. "manual" reads a manually-saved
    copy of the identical response shape from ODDS_API_CAPTURE_DIR
    instead -- for when the API itself is temporarily unreachable (rate
    limit, outage, exhausted quota) but a response body can still be
    obtained by hand. Both modes are explicit, not inferred from which
    env vars happen to be set, so a misconfigured mode fails loudly here
    rather than silently doing the wrong thing.
    """
    mode = config.odds_api_mode

    if mode == "auto":
        return TheOddsApiCollector(config.sport_key, api_key=config.odds_api_key)

    if mode == "manual":
        if not config.odds_api_capture_dir:
            raise ValueError(
                "ODDS_API_MODE=manual requires ODDS_API_CAPTURE_DIR to be set."
            )
        return TheOddsApiManualCollector(
            Path(config.odds_api_capture_dir), sport_key=config.sport_key
        )

    raise ValueError(f"ODDS_API_MODE={mode!r} must be 'auto' or 'manual'.")


def _mozzart_collectors(config: AppConfig) -> list[OddsCollector]:
    """Builds the Mozzart supplemental collector(s): one for
    MOZZART_CAPTURE_DIR, and -- only if it's set -- a second one for
    MOZZART_PREMATCH_CAPTURE_DIR.

    The second directory exists purely to avoid an overwrite race: the
    capture tooling saves every response under the same default filename
    ("live.json") regardless of whether it captured the live or the
    pre-match listing, so a live capture and a pre-match capture landing
    in the *same* directory close together in time can silently
    overwrite each other before the poller's next cycle reads either
    one. Each directory's collector still resolves phase per match from
    its own status.isLive/status.name (see
    mozzart_file_collector._resolve_market_and_lifecycle) -- routing to
    two directories only removes the race, it isn't a second source of
    truth for which phase a match is in, so a stray wrong-phase match
    landing in the "wrong" directory is still tagged correctly rather
    than mislabeled.

    MOZZART_MODE exists (default and currently only valid value:
    "manual") so the mode is an explicit, visible flag rather than
    something inferred from which env vars happen to be set -- the same
    reasoning as _the_odds_api_collector's ODDS_API_MODE, even though
    Mozzart has no working automatic mode yet (mozzartbet.com's
    Cloudflare bot-management, see MozzartFileCollector). It governs
    both directories: there's no meaningful case for one being manual
    and the other automatic.
    """
    if not config.mozzart_capture_dir and not config.mozzart_prematch_capture_dir:
        return []

    if config.mozzart_mode != "manual":
        raise ValueError(
            f"MOZZART_MODE={config.mozzart_mode!r} is not supported -- Mozzart has "
            "no automatic mode yet (see README.md's Data Collection section)."
        )

    collectors: list[OddsCollector] = []
    if config.mozzart_capture_dir:
        collectors.append(MozzartFileCollector(Path(config.mozzart_capture_dir)))
    if config.mozzart_prematch_capture_dir:
        collectors.append(MozzartFileCollector(Path(config.mozzart_prematch_capture_dir)))
    return collectors


def _meridianbet_collector(config: AppConfig) -> OddsCollector | None:
    """Builds the Meridianbet supplemental collector, if
    MERIDIANBET_CAPTURE_DIR is set. Same opt-in, manual-capture-only
    shape as Mozzart -- MERIDIANBET_MODE exists for the same reason
    MOZZART_MODE does (an explicit, visible flag rather than an inferred
    one), even though "manual" is the only mode implemented so far.
    """
    if not config.meridianbet_capture_dir:
        return None

    if config.meridianbet_mode != "manual":
        raise ValueError(
            f"MERIDIANBET_MODE={config.meridianbet_mode!r} is not supported -- "
            "Meridianbet has no automatic mode yet."
        )

    return MeridianbetFileCollector(Path(config.meridianbet_capture_dir))


def _api_football_collector(config: AppConfig) -> OddsCollector | None:
    """Builds the api-football.com supplemental collector, if
    API_FOOTBALL_KEY is set. Opt-in the same way Mozzart is -- absence of
    the key means this source is simply not configured, not an error.

    Only used when api-football is *not* already the primary source (see
    build_collectors/_supplemental_collectors) -- ODDS_SOURCE=api-football
    builds its own primary ApiFootballCollector directly, and adding one
    here too would poll the exact same endpoint twice every cycle.
    """
    if not config.api_football_key:
        return None

    return ApiFootballCollector(api_key=config.api_football_key)


def _the_odds_api_supplemental_collector(
    config: AppConfig,
    collector_run_repository: CollectorRunRepository,
    *,
    now: datetime,
) -> OddsCollector | None:
    """Builds the-odds-api.com supplemental collector, if ODDS_API_KEY is
    set -- opt-in the same way api-football/Mozzart are. Only used when
    the-odds-api is *not* already the primary source (ODDS_SOURCE=
    the-odds-api already polls it every cycle via _the_odds_api_collector,
    same "don't poll the same endpoint twice" reasoning as
    _api_football_collector).

    Unlike every other supplemental collector, this one is also
    rate-limited against real elapsed time (config.odds_api_min_interval),
    not just included-or-not: the-odds-api's free tier is ~500 requests/
    *month*, not api-football's 100/*day* -- simply riding the same
    per-cycle cadence every other collector uses would burn a month's
    budget in days once burst windows or a tight POLL_INTERVAL_SECONDS
    are in play. Checks the real collector_runs history (via
    find_latest_by_source, keyed on this exact collector's own `source`,
    e.g. "the-odds-api:soccer_epl") rather than tracking state in memory,
    so the gate survives a poller restart correctly instead of allowing
    an extra request right after every restart.
    """
    if not config.odds_api_key or config.odds_source == "the-odds-api":
        return None

    collector = TheOddsApiCollector(config.sport_key, api_key=config.odds_api_key)
    latest = collector_run_repository.find_latest_by_source(collector.source)
    if latest is not None and now - latest.started_at < config.odds_api_min_interval:
        return None

    return collector


def _supplemental_collectors(
    config: AppConfig,
    collector_run_repository: CollectorRunRepository | None = None,
    *,
    now: datetime | None = None,
) -> list[OddsCollector]:
    """Collectors layered on top of whichever primary source is active in
    build_collectors(), so they land in the same ingestion cycle and get
    matched against the same FixtureCatalog as everyone else instead of
    running in isolation. Each is opt-in via its own env var (a capture
    dir, an API key), so a run with none configured behaves exactly as
    before.

    collector_run_repository defaults to None, which disables only
    _the_odds_api_supplemental_collector's rate-limit *lookup* (it still
    respects ODDS_API_KEY being unset or the-odds-api being primary) --
    every real call site (run_ingestion) always supplies the real
    repository; None only matters for callers (older tests, a future
    one-off script) that have no CollectorRunRepository handy and don't
    care about ODDS_API_KEY. now similarly defaults to real wall-clock
    time via datetime.now(UTC) when not given, mirroring the demo-vs-real
    "now" distinction already made elsewhere in this module.

    Adding another source later is the same shape: its own
    _xxx_collector() helper reading its own AppConfig fields, appended
    here -- no other wiring changes needed.
    """
    collectors: list[OddsCollector] = []

    collectors.extend(_mozzart_collectors(config))

    meridianbet = _meridianbet_collector(config)
    if meridianbet is not None:
        collectors.append(meridianbet)

    if config.odds_source != "api-football":
        api_football = _api_football_collector(config)
        if api_football is not None:
            collectors.append(api_football)

    if collector_run_repository is not None:
        odds_api = _the_odds_api_supplemental_collector(
            config, collector_run_repository, now=now if now is not None else datetime.now(UTC)
        )
        if odds_api is not None:
            collectors.append(odds_api)

    return collectors


def build_collectors(
    config: AppConfig,
    collector_run_repository: CollectorRunRepository | None = None,
    *,
    now: datetime | None = None,
) -> list[OddsCollector]:
    """Returns the poll cycle(s) to run this invocation.

    The JSON demo path runs two polls against two fixed sample files (a
    second one with moved odds) so the movement report has something to
    compare on a single script run, instead of only being demonstrable
    across separate invocations. The live paths (the-odds-api,
    api-football) stay single-poll: a second real API call a few seconds
    later would double credit usage without a real market having
    necessarily moved in that time.

    ODDS_SOURCE=api-football exists for exactly one reason: to let
    api-football.com be used *without* also pulling in the JSON demo's
    synthetic data, for anyone who only has an API_FOOTBALL_KEY (no
    the-odds-api key) and wants a dataset made entirely of real
    observations -- mixing demo and real data into the same persistent
    database would otherwise be the only option. ApiFootballCollector
    itself still raises if API_FOOTBALL_KEY ends up unset, the same
    "fail loudly at the collector, not silently in load_config()"
    pattern _the_odds_api_collector already follows for ODDS_API_KEY.

    Unlike the old build_collectors_and_events(), no discovery pass or
    replay wrapping is needed here: FixtureCatalog resolves events on the
    fly as records are ingested (see run_ingestion()), so each collector's
    collect() only ever needs to run once, called naturally by
    OddsIngestionService.run() itself.
    """
    if config.odds_source == "the-odds-api":
        primary_collectors: list[OddsCollector] = [_the_odds_api_collector(config)]
    elif config.odds_source == "api-football":
        primary_collectors = [ApiFootballCollector(api_key=config.api_football_key)]
    else:
        samples_dir = Path(__file__).resolve().parents[2] / "data" / "samples"
        primary_collectors = [
            JsonOddsCollector(samples_dir / "odds_sample.json"),
            JsonOddsCollector(samples_dir / "odds_sample_poll2.json"),
        ]

    return [
        *primary_collectors,
        *_supplemental_collectors(config, collector_run_repository, now=now),
    ]


def run_ingestion(runtime: Runtime, config: AppConfig) -> list[Event]:
    """Runs every configured collector's poll cycle and returns every
    event actually touched this cycle, ready for analysis -- the
    "collect -> validate -> match -> persist" half of the pipeline,
    deliberately separate from analysis/reporting below.

    Deliberately not catalog.list_events() (every event the shared
    FixtureCatalog has ever created, across every run this database has
    ever seen): none of this project's sources say when a match is
    over, so a long-finished event would otherwise be handed to
    run_detection forever, always failing freshness and permanently
    "stale" instead of simply falling out of consideration once nothing
    reports on it anymore. Each OddsIngestionService.touched_events
    reports only the events its own poll actually resolved a record
    against; the union across every poll this cycle is this function's
    return value.

    Logs via the standard logging module, not print() -- matters now
    that poller.run_forever() calls this every cycle of a
    long-running process: a plain print() would bypass whatever log
    handlers/files an operator has configured for that process, the
    same reasoning run_detection's own print()s were removed for
    earlier (see that function's docstring).
    """
    collectors = build_collectors(config, runtime.collector_run_repository)
    sources = ", ".join(collector.source for collector in collectors)
    logger.info(
        "ingestion.cycle.started", extra={"sources": sources, "poll_count": len(collectors)}
    )

    touched: dict[str, Event] = {}

    for poll_number, collector in enumerate(collectors, start=1):
        # One FixtureCatalog per collector, scoped to that collector's
        # provider_id (its team-name mapping cache is per-provider, so
        # auto/manual collectors for the same real provider share one
        # cache -- see FixtureCatalog's docstring), but all sharing the
        # same connection/tables -- so a team or event resolved by one
        # collector is immediately visible to the next.
        catalog = FixtureCatalog(
            runtime.connection,
            provider_id=collector.provider_id,
            aliases=ALIASES,
            token_aliases=TOKEN_ALIASES,
            league_aliases=LEAGUE_ALIASES,
        )
        # Same per-provider scoping FixtureCatalog uses (see its own
        # docstring): two collectors for the same real provider (auto vs.
        # manual capture) share one bookmaker-mapping cache, so a
        # bookmaker resolved by one is immediately reused by the other.
        bookmaker_catalog = BookmakerCatalog(runtime.connection, provider_id=collector.provider_id)

        service = OddsIngestionService(
            collector=collector,
            matcher=catalog,
            odds_repository=runtime.odds_repository,
            collector_run_repository=runtime.collector_run_repository,
            raw_payload_repository=runtime.raw_payload_repository,
            bookmaker_catalog=bookmaker_catalog,
            collector_version="0.1.0",
            metrics=runtime.metrics,
            event_status_repository=runtime.event_status_repository,
        )
        run = service.run()
        logger.info(
            "ingestion.cycle.poll_completed",
            extra={
                "poll_number": poll_number,
                "poll_count": len(collectors),
                "run_id": run.id,
                "status": run.status.value,
                "records_accepted": run.records_accepted,
                "records_received": run.records_received,
            },
        )
        for event in service.touched_events:
            touched[event.id] = event

    return list(touched.values())


def demo_analysis_time(events: list[Event], odds_repository: OddsRepository) -> datetime:
    """The demo dataset uses fixed calendar timestamps rather than live
    polling, so "now" for freshness purposes is the newest observation
    across everything ingested this run -- not wall-clock time, which
    would make demo data look permanently stale.

    Not the default inside detect_surebet_candidates/
    detect_value_gap_candidates themselves: those must always receive an
    explicit analysis_time from their caller (real wall-clock time in any
    live deployment), never silently fall back to "newest observation in
    the batch" -- that would hide genuinely stale data behind snapshots
    that are merely close to each other in time (see
    analysis.opportunity_detection). Public (not demo-only-pipeline-
    private) because reporting.console needs the exact same "now" this
    module's own run_detection uses, to gate on the same freshness
    reporting displays as what was actually detected/persisted.
    """
    all_quote_times = [
        snapshot.quote_time
        for event in events
        for snapshot in odds_repository.find_by_event(event.id)
    ]
    return max(all_quote_times) if all_quote_times else datetime.now(UTC)


def from_surebet(candidate: SurebetCandidate) -> SignalCandidate:
    """Adapts a detection-layer SurebetCandidate into the storage-layer
    SignalCandidate SignalRepository.reconcile() actually needs. Lives
    here, in the orchestration layer that already depends on both
    analysis and storage, rather than in storage.signal_repository
    itself -- storage has no business importing analysis.
    opportunity_detection's candidate types just to convert them; that
    dependency belongs to whoever is wiring detection into persistence.
    """
    return SignalCandidate(
        signal_type=SUREBET,
        event_id=candidate.event.id,
        market=candidate.market,
        outcome=None,
        edge_percent=candidate.profit_percent,
        details={
            "legs": [
                {
                    "outcome": leg.outcome,
                    "bookmaker": leg.bookmaker,
                    "bookmaker_id": leg.bookmaker_id,
                    "odds": str(leg.odds),
                    "quote_time": leg.quote_time.isoformat() if leg.quote_time else None,
                    "collector_run_id": leg.collector_run_id,
                    "stake_percent": str(leg.stake_percent),
                }
                for leg in candidate.legs
            ]
        },
    )


def from_value_gap(candidate: ValueGapCandidate) -> SignalCandidate:
    """See from_surebet -- same adapter role for ValueGapCandidate."""
    return SignalCandidate(
        signal_type=VALUE_GAP,
        event_id=candidate.event.id,
        market=candidate.market,
        outcome=candidate.outcome,
        edge_percent=candidate.deviation_percent,
        details={
            "bookmaker": candidate.bookmaker,
            "bookmaker_id": candidate.bookmaker_id,
            "odds": str(candidate.odds),
            "quote_time": candidate.quote_time.isoformat() if candidate.quote_time else None,
            "collector_run_id": candidate.collector_run_id,
        },
    )


def persist_detected_signals(
    events: list[Event],
    odds_repository: OddsRepository,
    signal_repository: SignalRepository,
    movement_repository: MovementRepository,
    *,
    market: MarketIdentity,
    freshness_policy: FreshnessPolicy,
    analysis_time: datetime,
    min_value_gap_percent: Decimal = Decimal("15.0"),
    observed_at: datetime | None = None,
) -> dict[str, int]:
    """Runs one detection sweep and persists the result -- the "where and
    how to keep derived information" half of the pipeline, deliberately
    separate from reporting (the OPPORTUNITIES/ODDS MOVEMENT sections
    above): this decides what counts as a signal and stores it; a future
    presentation layer (dashboard, Telegram bot, whatever) would read
    from these tables instead of recomputing anything.

    SUREBET candidates are persisted unconditionally (no minimum-profit
    threshold): that cutoff is a "worth telling a human" business
    decision (reporting.opportunity_report's min_surebet_profit_percent),
    not a fact about whether the arbitrage exists, and persisting
    everything keeps that decision revisitable later without having
    discarded the underlying data. VALUE_GAP uses the same
    min_value_gap_percent threshold as the report, since that one *is*
    part of the outlier detection itself (see
    analysis.opportunity_detection).

    SUREBET/VALUE_GAP go through SignalRepository.reconcile() (stateful:
    new/still-active/resolved). Movements go through
    MovementRepository.save() (point-in-time, append-only) -- see those
    modules for why the two need different lifecycle handling.

    observed_at defaults to real wall-clock time, but run_detection
    passes its own explicitly -- it needs the exact same instant it later
    passes to SignalRepository.expire_active_signals's expired_at, so a
    signal reconciled ACTIVE this call has last_seen_at == expired_at and
    is not immediately re-expired by that follow-up call.
    """
    observed_at = observed_at if observed_at is not None else datetime.now(UTC)

    surebet_sweep = detect_surebet_candidates(
        events, odds_repository, market, freshness_policy=freshness_policy,
        analysis_time=analysis_time,
    )
    signal_repository.reconcile(
        SUREBET,
        [from_surebet(candidate) for candidate in surebet_sweep.candidates],
        observed_at=observed_at,
        evaluated_keys=surebet_sweep.evaluated_keys,
    )

    value_gap_sweep = detect_value_gap_candidates(
        events,
        odds_repository,
        market,
        freshness_policy=freshness_policy,
        analysis_time=analysis_time,
        threshold_percent=min_value_gap_percent,
    )
    signal_repository.reconcile(
        VALUE_GAP,
        [from_value_gap(candidate) for candidate in value_gap_sweep.candidates],
        observed_at=observed_at,
        evaluated_keys=value_gap_sweep.evaluated_keys,
    )

    movements = detect_movements(events, odds_repository, market)
    for movement in movements:
        movement_repository.save(movement, detected_at=observed_at)

    return {
        "active_surebets": len(signal_repository.find_active(SUREBET)),
        "active_value_gaps": len(signal_repository.find_active(VALUE_GAP)),
        "movements_recorded": len(movements),
    }


def run_detection(runtime: Runtime, events: list[Event], config: AppConfig) -> dict[str, int]:
    """Detects and persists signals/movements for every ingested event,
    across every market this cycle's events actually have odds for, then
    expires ACTIVE signals whose event's lifecycle has run out -- the
    last step of the *core* pipeline: collect -> validate -> match ->
    persist observations -> detect anomalies (per market) -> persist
    signals -> expire stale signals.

    Which markets to sweep is discovered from the data itself
    (OddsRepository.distinct_markets_for_events), not a hand-maintained
    list of MarketIdentity constants (the old DETECTED_MARKETS tuple) --
    that tuple already caused a real gap once: MozzartFileCollector's
    LIVE_MARKET snapshots were ingested and stored correctly but never
    reached detection for a real stretch of this project's history,
    simply because nobody had added LIVE_MARKET to the tuple yet. A
    market present in the data but genuinely unsupported (its
    market_type has no models.market.REQUIRED_OUTCOMES entry -- e.g. a
    hypothetical MONEYLINE collector added later, before detection
    logic for it exists) is skipped with a loud warning log, not a
    silent one and not a crash -- visible enough that the same "we're
    collecting something nothing ever analyzes" gap can't hide again,
    without making an intentionally-not-yet-supported market type fail
    the whole cycle.

    One persist_detected_signals() call per discovered market, not one
    call analyzing every market at once: detect_surebet_candidates/
    detect_value_gap_candidates and SignalRepository.reconcile() are all
    already scoped to a single MarketIdentity per call (see
    SignalCandidate.identity, which includes market), so looping here is
    the natural fit -- reconciling one market's sweep can never resolve
    a signal belonging to a market this cycle didn't just evaluate,
    since evaluated_keys/SignalIdentity carry the market they came from.

    movements_recorded is summed across the loop (each market's sweep
    finds its own, disjoint set of movements), but active_surebets/
    active_value_gaps are read once *after* the whole loop, not taken
    from any single iteration's return value -- SignalRepository.
    find_active() counts ACTIVE signals across every market at once, so
    the last market processed would otherwise clobber the true
    cross-market total in summary.

    Deliberately imports nothing from reporting.* and prints nothing at
    all -- returns its summary dict for the caller to do with as it
    likes (print it, log it, feed a future dashboard). Whether/how to
    display anything, including this function's own terse summary, is a
    presentation decision, not a detection fact; app.py's main() prints
    it, but this function itself must stay usable by any caller that
    only wants detection with no console output as a side effect (a
    test, a future notification service, ...). See
    reporting.console.print_reports for the full human-facing report,
    kept out of the core pipeline on purpose so the core never depends
    on it.

    Expiry is a separate step from detection, not folded into any
    per-market sweep, and runs exactly once after every market has been
    processed: run_ingestion() already scopes `events` to what was
    actually touched this cycle (see OddsIngestionService.touched_events),
    so a long-finished match that nothing reports on anymore never
    reaches persist_detected_signals at all, and its ACTIVE signals would
    otherwise stay ACTIVE forever -- "not evaluated" is not the same
    claim as "confirmed gone" (see SignalRepository.reconcile()), so
    reconcile() alone can never resolve them. expire_active_signals is
    the explicit, separate acknowledgment that a signal's event has
    simply run out of runway, regardless of whether this cycle evaluated
    it. now is computed once and threaded through every persist call and
    the final expire_active_signals call: a signal reconcile() just
    reconfirmed ACTIVE has last_seen_at == now, which excludes it from
    expire_active_signals's own cutoff check -- without sharing the same
    instant, a signal genuinely re-confirmed this exact cycle could be
    immediately re-expired by the very next line.
    """
    # Every non-demo source polls in real time, so real wall-clock "now"
    # is the correct analysis_time for any of them -- checked as "not
    # demo" rather than naming each real source (originally just
    # "the-odds-api") so a new real source (api-football, and whatever
    # comes after it) gets this right automatically instead of needing
    # this condition remembered and updated by hand every time one is
    # added.
    analysis_time = (
        datetime.now(UTC)
        if config.odds_source != "demo"
        else demo_analysis_time(events, runtime.odds_repository)
    )
    now = datetime.now(UTC)
    freshness_policy = resolve_freshness_policy(config)

    # Events a collector has told us are FINISHED/POSTPONED_OR_CANCELED
    # (see models.market.EventLifecycle) never reach detection at all --
    # a pre-match price for a match that has already ended or won't be
    # played is never a meaningful surebet/value-gap candidate, real
    # status or not. An event with no known lifecycle (get_many() has no
    # entry for it -- most events, since only ApiFootballCollector
    # reports status at all) is never excluded here: absence means
    # "unknown", not "over" -- see EventLifecycle's own docstring.
    lifecycles = runtime.event_status_repository.get_many([event.id for event in events])
    events = [
        event
        for event in events
        if lifecycles.get(event.id)
        not in (EventLifecycle.FINISHED, EventLifecycle.POSTPONED_OR_CANCELED)
    ]

    present_markets = runtime.odds_repository.distinct_markets_for_events(
        [event.id for event in events]
    )
    detectable_markets = [m for m in present_markets if m.market_type in REQUIRED_OUTCOMES]
    unsupported_market_types = {
        m.market_type for m in present_markets if m.market_type not in REQUIRED_OUTCOMES
    }
    if unsupported_market_types:
        logger.warning(
            "run_detection.unsupported_market_type_present",
            extra={
                "market_types": sorted(t.value for t in unsupported_market_types),
                "hint": "data is being ingested for this market_type but "
                "REQUIRED_OUTCOMES has no entry for it, so it is never analyzed",
            },
        )

    movements_recorded = 0
    for market in detectable_markets:
        sweep = persist_detected_signals(
            events,
            runtime.odds_repository,
            runtime.signal_repository,
            runtime.movement_repository,
            market=market,
            freshness_policy=freshness_policy,
            analysis_time=analysis_time,
            min_value_gap_percent=config.min_value_gap_percent,
            observed_at=now,
        )
        movements_recorded += sweep["movements_recorded"]

    summary = {
        "active_surebets": len(runtime.signal_repository.find_active(SUREBET)),
        "active_value_gaps": len(runtime.signal_repository.find_active(VALUE_GAP)),
        "movements_recorded": movements_recorded,
    }

    expired_ids = runtime.signal_repository.expire_active_signals(
        event_start_cutoff=now - config.signal_ttl,
        expired_at=now,
    )
    summary["signals_expired"] = len(expired_ids)

    return summary
