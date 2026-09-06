import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from anomaly_detection_engine.analysis.arbitrage import calculate_arbitrage
from anomaly_detection_engine.analysis.best_odds import find_best_odds
from anomaly_detection_engine.analysis.freshness import FreshnessPolicy, validate_freshness
from anomaly_detection_engine.analysis.movement_detection import detect_movements
from anomaly_detection_engine.analysis.opportunity_detection import (
    SUREBET,
    VALUE_GAP,
    detect_surebet_candidates,
    detect_value_gap_candidates,
)
from anomaly_detection_engine.collectors.base import OddsCollector
from anomaly_detection_engine.collectors.json_collector import JsonOddsCollector
from anomaly_detection_engine.collectors.mozzart_file_collector import MozzartFileCollector
from anomaly_detection_engine.collectors.the_odds_api_collector import (
    TheOddsApiCollector,
    TheOddsApiManualCollector,
)
from anomaly_detection_engine.ingestion.service import OddsIngestionService
from anomaly_detection_engine.models.event import Event
from anomaly_detection_engine.models.market import DEFAULT_MARKET, MarketIdentity
from anomaly_detection_engine.observability.logging_config import configure_logging
from anomaly_detection_engine.observability.metrics import IngestionMetrics
from anomaly_detection_engine.reporting.movement_report import (
    build_movement_report,
    render_movement_report,
)
from anomaly_detection_engine.reporting.opportunity_report import (
    build_opportunity_report,
    render_opportunity_report,
)
from anomaly_detection_engine.storage.collector_run_repository import CollectorRunRepository
from anomaly_detection_engine.storage.database import create_connection, initialize_database
from anomaly_detection_engine.storage.fixture_catalog import FixtureCatalog
from anomaly_detection_engine.storage.movement_repository import MovementRepository
from anomaly_detection_engine.storage.odds_repository import OddsRepository
from anomaly_detection_engine.storage.raw_payload_repository import RawPayloadRepository
from anomaly_detection_engine.storage.signal_repository import (
    SignalRepository,
    from_surebet,
    from_value_gap,
)

# Default location for the persistent runtime database -- overridable via
# DB_PATH so tests/alternate deployments aren't forced to use this exact
# file. ":memory:" (pass DB_PATH=:memory:) is still supported for anyone
# who wants the old throwaway-per-run behavior back.
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "anomaly_detection.db"

# How long a connection waits for a lock before raising "database is
# locked", in seconds. Matters now that manual-capture sources can be
# triggered by independent watch_capture.py processes: two of them
# spawning app.py at close to the same moment both open this same file,
# and SQLite allows only one writer at a time. A generous timeout makes
# the second one wait instead of failing outright.
DB_BUSY_TIMEOUT_SECONDS = 30

_VALID_ODDS_SOURCES = ("demo", "the-odds-api")

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
}

# Demo dataset uses fixed calendar timestamps rather than live polling, so
# freshness is evaluated relative to the newest observation in the batch
# (not wall-clock "now", which would drift stale as real time passes).
DEMO_FRESHNESS_POLICY = FreshnessPolicy(
    max_snapshot_age=timedelta(minutes=5),
    max_observation_spread=timedelta(minutes=5),
)


@dataclass(frozen=True)
class AppConfig:
    """Every environment-variable-driven decision, resolved exactly once
    at startup -- so nothing downstream (run_ingestion, run_analysis,
    persist_detected_signals) reads os.environ directly, and every choice
    fails loudly here rather than silently deep inside a poll cycle.
    """

    db_path: str
    odds_source: str
    sport_key: str
    odds_api_mode: str
    odds_api_capture_dir: str | None
    mozzart_capture_dir: str | None
    mozzart_mode: str
    min_surebet_profit_percent: Decimal
    min_value_gap_percent: Decimal


def load_config() -> AppConfig:
    odds_source = os.environ.get("ODDS_SOURCE", "demo")
    if odds_source not in _VALID_ODDS_SOURCES:
        # A typo here (e.g. "the-odds-ap1") must not silently fall back to
        # demo data -- that is exactly the kind of thing that would go
        # unnoticed once this runs unattended.
        raise ValueError(
            f"ODDS_SOURCE={odds_source!r} must be one of {_VALID_ODDS_SOURCES}."
        )

    return AppConfig(
        db_path=os.environ.get("DB_PATH", str(DEFAULT_DB_PATH)),
        odds_source=odds_source,
        sport_key=os.environ.get("ODDS_SPORT_KEY", "soccer_epl"),
        odds_api_mode=os.environ.get("ODDS_API_MODE", "auto"),
        odds_api_capture_dir=os.environ.get("ODDS_API_CAPTURE_DIR"),
        mozzart_capture_dir=os.environ.get("MOZZART_CAPTURE_DIR"),
        mozzart_mode=os.environ.get("MOZZART_MODE", "manual"),
        min_surebet_profit_percent=Decimal(os.environ.get("MIN_SUREBET_PROFIT_PERCENT", "1.0")),
        min_value_gap_percent=Decimal(os.environ.get("MIN_VALUE_GAP_PERCENT", "15.0")),
    )


@dataclass
class Runtime:
    """Every wired-up dependency a poll/analysis cycle needs, built once
    by build_runtime() -- plain data, no framework/DI container."""

    connection: sqlite3.Connection
    odds_repository: OddsRepository
    collector_run_repository: CollectorRunRepository
    raw_payload_repository: RawPayloadRepository
    signal_repository: SignalRepository
    movement_repository: MovementRepository
    metrics: IngestionMetrics


def build_runtime(config: AppConfig) -> Runtime:
    if config.db_path != ":memory:":
        Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = create_connection(config.db_path, timeout=DB_BUSY_TIMEOUT_SECONDS)
    initialize_database(connection)

    return Runtime(
        connection=connection,
        odds_repository=OddsRepository(connection),
        collector_run_repository=CollectorRunRepository(connection),
        raw_payload_repository=RawPayloadRepository(connection),
        signal_repository=SignalRepository(connection),
        movement_repository=MovementRepository(connection),
        metrics=IngestionMetrics(),
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
        return TheOddsApiCollector(config.sport_key)

    if mode == "manual":
        if not config.odds_api_capture_dir:
            raise ValueError(
                "ODDS_API_MODE=manual requires ODDS_API_CAPTURE_DIR to be set."
            )
        return TheOddsApiManualCollector(
            Path(config.odds_api_capture_dir), sport_key=config.sport_key
        )

    raise ValueError(f"ODDS_API_MODE={mode!r} must be 'auto' or 'manual'.")


def _mozzart_collector(config: AppConfig) -> OddsCollector | None:
    """Builds the Mozzart supplemental collector, if MOZZART_CAPTURE_DIR is set.

    MOZZART_MODE exists (default and currently only valid value:
    "manual") so the mode is an explicit, visible flag rather than
    something inferred from which env vars happen to be set -- the same
    reasoning as _the_odds_api_collector's ODDS_API_MODE, even though
    Mozzart has no working automatic mode yet (mozzartbet.com's
    Cloudflare bot-management, see MozzartFileCollector).
    """
    if not config.mozzart_capture_dir:
        return None

    if config.mozzart_mode != "manual":
        raise ValueError(
            f"MOZZART_MODE={config.mozzart_mode!r} is not supported -- Mozzart has "
            "no automatic mode yet (see README.md's Data Collection section)."
        )

    return MozzartFileCollector(Path(config.mozzart_capture_dir))


def _supplemental_collectors(config: AppConfig) -> list[OddsCollector]:
    """Manual-capture collectors layered on top of whichever primary
    source is active in build_collectors(), so they land in the same
    ingestion cycle and get matched against the same FixtureCatalog as
    everyone else instead of running in isolation. Each is opt-in via
    its own *_CAPTURE_DIR env var, so a run with none configured behaves
    exactly as before.

    Adding another manual-capture source (MaxBet, Soccer, ...) later is
    the same shape: its own _xxx_collector() helper reading its own
    AppConfig fields, appended here -- no other wiring changes needed.
    """
    collectors: list[OddsCollector] = []

    mozzart = _mozzart_collector(config)
    if mozzart is not None:
        collectors.append(mozzart)

    return collectors


def build_collectors(config: AppConfig) -> list[OddsCollector]:
    """Returns the poll cycle(s) to run this invocation.

    The JSON demo path runs two polls against two fixed sample files (a
    second one with moved odds) so the movement report has something to
    compare on a single script run, instead of only being demonstrable
    across separate invocations. The live path stays single-poll: a
    second real API call a few seconds later would double credit usage
    without a real market having necessarily moved in that time.

    Unlike the old build_collectors_and_events(), no discovery pass or
    replay wrapping is needed here: FixtureCatalog resolves events on the
    fly as records are ingested (see run_ingestion()), so each collector's
    collect() only ever needs to run once, called naturally by
    OddsIngestionService.run() itself.
    """
    if config.odds_source == "the-odds-api":
        primary_collectors: list[OddsCollector] = [_the_odds_api_collector(config)]
    else:
        samples_dir = Path(__file__).resolve().parents[2] / "data" / "samples"
        primary_collectors = [
            JsonOddsCollector(samples_dir / "odds_sample.json"),
            JsonOddsCollector(samples_dir / "odds_sample_poll2.json"),
        ]

    return [*primary_collectors, *_supplemental_collectors(config)]


def run_ingestion(runtime: Runtime, config: AppConfig) -> list[Event]:
    """Runs every configured collector's poll cycle and returns every
    event known to the shared FixtureCatalog afterward, ready for
    analysis -- the "collect -> validate -> match -> persist" half of the
    pipeline, deliberately separate from analysis/reporting below.
    """
    collectors = build_collectors(config)
    sources = ", ".join(collector.source for collector in collectors)
    print(f"Sources: {sources} ({len(collectors)} poll(s))")

    catalog: FixtureCatalog | None = None

    for poll_number, collector in enumerate(collectors, start=1):
        # One FixtureCatalog per collector, scoped to that collector's own
        # source label (its team-name mapping cache is per-source), but
        # all sharing the same connection/tables -- so a team or event
        # resolved by one collector is immediately visible to the next.
        catalog = FixtureCatalog(runtime.connection, source=collector.source, aliases=ALIASES)

        service = OddsIngestionService(
            collector=collector,
            matcher=catalog,
            odds_repository=runtime.odds_repository,
            collector_run_repository=runtime.collector_run_repository,
            raw_payload_repository=runtime.raw_payload_repository,
            collector_version="0.1.0",
            metrics=runtime.metrics,
        )
        run = service.run()
        print(
            f"Poll {poll_number}/{len(collectors)} - collector run {run.id}: "
            f"{run.status.value} ({run.records_accepted}/{run.records_received} accepted)"
        )

    return catalog.list_events() if catalog is not None else []


def _demo_analysis_time(events: list[Event], odds_repository: OddsRepository) -> datetime:
    """The demo dataset uses fixed calendar timestamps rather than live
    polling, so "now" for freshness purposes is the newest observation
    across everything ingested this run -- not wall-clock time, which
    would make demo data look permanently stale.

    Deliberately kept local to this demo-only code path rather than being
    the default inside detect_surebet_candidates/detect_value_gap_candidates
    themselves: those must always receive an explicit analysis_time from
    their caller (real wall-clock time in any live deployment), never
    silently fall back to "newest observation in the batch" -- that would
    hide genuinely stale data behind snapshots that are merely close to
    each other in time (see analysis.opportunity_detection).
    """
    all_observed_at = [
        snapshot.observed_at
        for event in events
        for snapshot in odds_repository.find_by_event(event.id)
    ]
    return max(all_observed_at) if all_observed_at else datetime.now(timezone.utc)


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
) -> dict:
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
    """
    observed_at = datetime.now(timezone.utc)

    surebet_sweep = detect_surebet_candidates(
        events, odds_repository, market, freshness_policy=freshness_policy,
        analysis_time=analysis_time,
    )
    signal_repository.reconcile(
        SUREBET,
        market,
        [from_surebet(candidate) for candidate in surebet_sweep.candidates],
        observed_at=observed_at,
        evaluated_event_ids=surebet_sweep.evaluated_event_ids,
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
        market,
        [from_value_gap(candidate) for candidate in value_gap_sweep.candidates],
        observed_at=observed_at,
        evaluated_event_ids=value_gap_sweep.evaluated_event_ids,
    )

    movements = detect_movements(events, odds_repository, market)
    for movement in movements:
        movement_repository.save(movement, detected_at=observed_at)

    return {
        "active_surebets": len(signal_repository.find_active(SUREBET)),
        "active_value_gaps": len(signal_repository.find_active(VALUE_GAP)),
        "movements_recorded": len(movements),
    }


def run_analysis(runtime: Runtime, events: list[Event], config: AppConfig) -> None:
    """Prints the per-event breakdown and the opportunity/movement
    reports, then persists the same detection sweep -- everything
    downstream of ingestion.
    """
    analysis_time = (
        datetime.now(timezone.utc)
        if config.odds_source == "the-odds-api"
        else _demo_analysis_time(events, runtime.odds_repository)
    )

    for event in events:
        snapshots = runtime.odds_repository.find_latest_for_market(
            event_id=event.id,
            market=DEFAULT_MARKET,
        )
        if not snapshots:
            continue

        freshness = validate_freshness(
            snapshots,
            analysis_time=analysis_time,
            policy=DEMO_FRESHNESS_POLICY,
        )
        if not freshness.valid:
            print(f"\nSKIP {event.display_name}: not fresh ({freshness.reason})")
            continue

        best = find_best_odds(snapshots, event_id=event.id, market=DEFAULT_MARKET)
        if not best:
            continue

        result = calculate_arbitrage(best)

        print("\n" + "=" * 72)
        print(event.display_name)
        print("=" * 72)
        for outcome in ("1", "X", "2"):
            item = best[outcome]
            print(f"{outcome:>2}: {item.odds:.2f} @ {item.bookmaker_name}")

        print(f"Arbitrage margin: {result.margin:.4f}")
        print(f"Surebet: {'YES' if result.is_surebet else 'NO'}")
        print(f"Theoretical profit: {result.theoretical_profit_percent:.2f}%")

    print("\n" + "=" * 72)
    print("OPPORTUNITIES (surebets and notable best-odds gaps only)")
    print("=" * 72)
    opportunity_rows = build_opportunity_report(
        events,
        runtime.odds_repository,
        DEFAULT_MARKET,
        freshness_policy=DEMO_FRESHNESS_POLICY,
        analysis_time=analysis_time,
        min_surebet_profit_percent=config.min_surebet_profit_percent,
        min_value_gap_percent=config.min_value_gap_percent,
    )
    print(render_opportunity_report(opportunity_rows))

    print("\n" + "=" * 72)
    print("ODDS MOVEMENT (significant change between the last two readings)")
    print("=" * 72)
    movement_rows = build_movement_report(events, runtime.odds_repository, DEFAULT_MARKET)
    print(render_movement_report(movement_rows))

    sweep_summary = persist_detected_signals(
        events,
        runtime.odds_repository,
        runtime.signal_repository,
        runtime.movement_repository,
        market=DEFAULT_MARKET,
        freshness_policy=DEMO_FRESHNESS_POLICY,
        analysis_time=analysis_time,
        min_value_gap_percent=config.min_value_gap_percent,
    )
    print("\n" + "-" * 72)
    print(f"Signals: {sweep_summary}")

    print("\n" + "-" * 72)
    print(f"Metrics: {runtime.metrics.snapshot()}")


def main() -> None:
    configure_logging()

    config = load_config()
    runtime = build_runtime(config)

    events = run_ingestion(runtime, config)
    run_analysis(runtime, events, config)


if __name__ == "__main__":
    main()
