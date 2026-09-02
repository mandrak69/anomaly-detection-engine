import os
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from anomaly_detection_engine.analysis.arbitrage import calculate_arbitrage
from anomaly_detection_engine.analysis.best_odds import find_best_odds
from anomaly_detection_engine.analysis.freshness import FreshnessPolicy, validate_freshness
from anomaly_detection_engine.collectors.base import OddsCollector
from anomaly_detection_engine.collectors.json_collector import (
    DEFAULT_MARKET,
    JsonOddsCollector,
)
from anomaly_detection_engine.collectors.mozzart_file_collector import MozzartFileCollector
from anomaly_detection_engine.collectors.the_odds_api_collector import TheOddsApiCollector
from anomaly_detection_engine.ingestion.service import OddsIngestionService
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
from anomaly_detection_engine.storage.database import initialize_database
from anomaly_detection_engine.storage.fixture_catalog import FixtureCatalog
from anomaly_detection_engine.storage.odds_repository import OddsRepository
from anomaly_detection_engine.storage.raw_payload_repository import RawPayloadRepository


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


def _supplemental_collectors() -> list[OddsCollector]:
    """Manual-capture collectors layered on top of whichever primary
    source is active in build_collectors(), so they land in the same
    ingestion cycle and get matched against the same FixtureCatalog as
    everyone else instead of running in isolation. Each is opt-in via
    its own env var, so a run with none configured behaves exactly as
    before.

    Adding another manual-capture source (MaxBet, Soccer, ...) later is
    the same two lines: read its own env var, construct its
    FileCollector, append it here -- no other wiring changes needed.
    """
    collectors: list[OddsCollector] = []

    mozzart_dir = os.environ.get("MOZZART_CAPTURE_DIR")
    if mozzart_dir:
        collectors.append(MozzartFileCollector(Path(mozzart_dir)))

    return collectors


def build_collectors() -> list[OddsCollector]:
    """Returns the poll cycle(s) to run this invocation.

    The JSON demo path runs two polls against two fixed sample files (a
    second one with moved odds) so the movement report has something to
    compare on a single script run, instead of only being demonstrable
    across separate invocations. The live path stays single-poll: a
    second real API call a few seconds later would double credit usage
    without a real market having necessarily moved in that time.

    Unlike the old build_collectors_and_events(), no discovery pass or
    replay wrapping is needed here: FixtureCatalog resolves events on the
    fly as records are ingested (see main()), so each collector's
    collect() only ever needs to run once, called naturally by
    OddsIngestionService.run() itself.
    """
    if os.environ.get("ODDS_SOURCE") == "the-odds-api":
        sport_key = os.environ.get("ODDS_SPORT_KEY", "soccer_epl")
        primary_collectors: list[OddsCollector] = [TheOddsApiCollector(sport_key)]
    else:
        samples_dir = Path(__file__).resolve().parents[2] / "data" / "samples"
        primary_collectors = [
            JsonOddsCollector(samples_dir / "odds_sample.json"),
            JsonOddsCollector(samples_dir / "odds_sample_poll2.json"),
        ]

    return [*primary_collectors, *_supplemental_collectors()]


def main() -> None:
    configure_logging()

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)

    odds_repository = OddsRepository(connection)
    collector_run_repository = CollectorRunRepository(connection)
    raw_payload_repository = RawPayloadRepository(connection)

    collectors = build_collectors()
    sources = ", ".join(collector.source for collector in collectors)
    print(f"Sources: {sources} ({len(collectors)} poll(s))")

    metrics = IngestionMetrics()
    catalog: FixtureCatalog | None = None

    for poll_number, collector in enumerate(collectors, start=1):
        # One FixtureCatalog per collector, scoped to that collector's own
        # source label (its team-name mapping cache is per-source), but
        # all sharing the same connection/tables -- so a team or event
        # resolved by one collector is immediately visible to the next.
        catalog = FixtureCatalog(connection, source=collector.source, aliases=ALIASES)

        service = OddsIngestionService(
            collector=collector,
            matcher=catalog,
            odds_repository=odds_repository,
            collector_run_repository=collector_run_repository,
            raw_payload_repository=raw_payload_repository,
            collector_version="0.1.0",
            metrics=metrics,
        )
        run = service.run()
        print(
            f"Poll {poll_number}/{len(collectors)} - collector run {run.id}: "
            f"{run.status.value} ({run.records_accepted}/{run.records_received} accepted)"
        )

    events = catalog.list_events() if catalog is not None else []

    for event in events:
        snapshots = odds_repository.find_latest_for_market(
            event_id=event.id,
            market_type=DEFAULT_MARKET.market_type.value,
            market_period=DEFAULT_MARKET.period.value,
        )
        if not snapshots:
            continue

        freshness = validate_freshness(
            snapshots,
            analysis_time=max(snapshot.observed_at for snapshot in snapshots),
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
        odds_repository,
        DEFAULT_MARKET,
        min_surebet_profit_percent=Decimal(os.environ.get("MIN_SUREBET_PROFIT_PERCENT", "1.0")),
        min_value_gap_percent=Decimal(os.environ.get("MIN_VALUE_GAP_PERCENT", "15.0")),
    )
    print(render_opportunity_report(opportunity_rows))

    print("\n" + "=" * 72)
    print("ODDS MOVEMENT (significant change between the last two readings)")
    print("=" * 72)
    movement_rows = build_movement_report(events, odds_repository, DEFAULT_MARKET)
    print(render_movement_report(movement_rows))

    print("\n" + "-" * 72)
    print(f"Metrics: {metrics.snapshot()}")


if __name__ == "__main__":
    main()
