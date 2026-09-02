import os
import sqlite3
from datetime import datetime, timedelta
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
from anomaly_detection_engine.matching.event_matcher import EventMatcher
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.raw_odds import RawEventOdds
from anomaly_detection_engine.normalization.team_normalizer import TeamNormalizer
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
from anomaly_detection_engine.storage.odds_repository import OddsRepository
from anomaly_detection_engine.storage.raw_payload_repository import RawPayloadRepository


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


class _ReplayCollector(OddsCollector):
    """Replays an already-collected batch instead of hitting the network.

    Used for the live-source demo path below, which has to collect once
    up front to discover events before a matcher can be built -- this lets
    that same batch be handed to OddsIngestionService without a second,
    credit-consuming API call.
    """

    def __init__(self, source: str, raw_events: list[RawEventOdds]) -> None:
        self._source = source
        self._raw_events = raw_events

    @property
    def source(self) -> str:
        return self._source

    def collect(self) -> list[RawEventOdds]:
        return self._raw_events


def build_demo_events() -> list[Event]:
    return [
        Event(
            id="event-001",
            sport="football",
            league="demo-league",
            home_team=Team("team-001", "Manchester United"),
            away_team=Team("team-002", "Liverpool"),
            start_time=datetime.fromisoformat("2026-09-01T20:00:00+00:00"),
        ),
        Event(
            id="event-002",
            sport="football",
            league="demo-league",
            home_team=Team("team-003", "Real Madrid"),
            away_team=Team("team-004", "Barcelona"),
            start_time=datetime.fromisoformat("2026-09-02T21:00:00+00:00"),
        ),
    ]


def build_events_from_raw(raw_events: list[RawEventOdds]) -> list[Event]:
    """Derives canonical events straight from a collected batch.

    A stand-in for sources with no fixed canonical events to match
    against -- there is no real fixtures/event catalog yet (see
    architecture.md's Next Architectural Step), so this trusts the
    source's own team-name strings as canonical. IDs are prefixed
    "auto-" specifically so they can never collide with build_demo_events()'s
    fixed "event-NNN" IDs when both are combined into one events list
    (see build_collectors_and_events) -- a collision there would silently
    merge two unrelated matches under the same database event_id.

    Two different sources reporting the same real match under different
    team-name spellings ("Man Utd" vs "Manchester United") will NOT be
    merged into one event here -- each becomes its own canonical event,
    so their odds are never compared against each other. Resolving that
    needs the same fixtures catalog noted above; this is a known gap,
    not an oversight.
    """
    events: dict[tuple[str, str], Event] = {}
    for raw in raw_events:
        key = (raw.home_team, raw.away_team)
        if key not in events:
            index = len(events) + 1
            events[key] = Event(
                id=f"auto-{index:03d}",
                sport=raw.sport,
                league=raw.league,
                home_team=Team(f"auto-home-{index}", raw.home_team),
                away_team=Team(f"auto-away-{index}", raw.away_team),
                start_time=raw.start_time,
            )
    return list(events.values())


def _supplemental_collectors() -> list[OddsCollector]:
    """Manual-capture collectors layered on top of whichever primary
    source is active below, so they land in the same ingestion cycle and
    repository and get compared against everyone else instead of running
    in isolation. Each is opt-in via its own env var, so a run with none
    configured behaves exactly as before.

    Adding another manual-capture source (MaxBet, Soccer, ...) later is
    the same two lines: read its own env var, construct its
    FileCollector, append it here -- no other wiring changes needed.
    """
    collectors: list[OddsCollector] = []

    mozzart_dir = os.environ.get("MOZZART_CAPTURE_DIR")
    if mozzart_dir:
        collectors.append(MozzartFileCollector(Path(mozzart_dir)))

    return collectors


def build_collectors_and_events() -> tuple[list[OddsCollector], list[Event]]:
    """Returns the poll cycle(s) to run and the canonical events to match against.

    The JSON demo path runs two polls against two fixed sample files (a
    second one with moved odds) so the movement report has something to
    compare on a single script run, instead of only being demonstrable
    across separate invocations. The live path stays single-poll: two
    real API calls a few seconds apart would double credit usage without
    a real market having necessarily moved in that time.

    Any supplemental collectors (see _supplemental_collectors) run
    alongside whichever primary path is active. Every collector is
    collected from exactly once here and wrapped in a _ReplayCollector --
    this is a discovery pass to learn what events exist before the
    matcher can be built, and doing the real fetch/archive twice would
    waste API credits (live source) or archive a capture that was never
    actually ingested (file source).
    """
    if os.environ.get("ODDS_SOURCE") == "the-odds-api":
        sport_key = os.environ.get("ODDS_SPORT_KEY", "soccer_epl")
        primary_collectors: list[OddsCollector] = [TheOddsApiCollector(sport_key)]
        fixed_events: list[Event] = []
    else:
        samples_dir = Path(__file__).resolve().parents[2] / "data" / "samples"
        primary_collectors = [
            JsonOddsCollector(samples_dir / "odds_sample.json"),
            JsonOddsCollector(samples_dir / "odds_sample_poll2.json"),
        ]
        fixed_events = build_demo_events()

    supplemental_collectors = _supplemental_collectors()

    replay_collectors: list[OddsCollector] = []
    events_needed_from: list[RawEventOdds] = []

    for collector in primary_collectors:
        raw_events = collector.collect()
        replay_collectors.append(_ReplayCollector(collector.source, raw_events))
        if not fixed_events:
            events_needed_from.extend(raw_events)

    for collector in supplemental_collectors:
        raw_events = collector.collect()
        replay_collectors.append(_ReplayCollector(collector.source, raw_events))
        events_needed_from.extend(raw_events)

    events = fixed_events + build_events_from_raw(events_needed_from)

    return replay_collectors, events


def main() -> None:
    configure_logging()

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)

    odds_repository = OddsRepository(connection)
    collector_run_repository = CollectorRunRepository(connection)
    raw_payload_repository = RawPayloadRepository(connection)

    collectors, events = build_collectors_and_events()
    sources = ", ".join(collector.source for collector in collectors)
    print(f"Sources: {sources} ({len(events)} events, {len(collectors)} poll(s))")

    canonical_names = {
        event.home_team.canonical_name
        for event in events
    } | {
        event.away_team.canonical_name
        for event in events
    }

    normalizer = TeamNormalizer(canonical_names, ALIASES, fuzzy_threshold=80)
    matcher = EventMatcher(events, normalizer)

    metrics = IngestionMetrics()

    for poll_number, collector in enumerate(collectors, start=1):
        service = OddsIngestionService(
            collector=collector,
            matcher=matcher,
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
