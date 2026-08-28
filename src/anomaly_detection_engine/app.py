import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from anomaly_detection_engine.analysis.arbitrage import calculate_arbitrage
from anomaly_detection_engine.analysis.best_odds import find_best_odds
from anomaly_detection_engine.analysis.freshness import FreshnessPolicy, validate_freshness
from anomaly_detection_engine.collectors.base import OddsCollector
from anomaly_detection_engine.collectors.json_collector import (
    DEFAULT_MARKET,
    JsonOddsCollector,
)
from anomaly_detection_engine.collectors.the_odds_api_collector import TheOddsApiCollector
from anomaly_detection_engine.ingestion.service import OddsIngestionService
from anomaly_detection_engine.matching.event_matcher import EventMatcher
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.raw_odds import RawEventOdds
from anomaly_detection_engine.normalization.team_normalizer import TeamNormalizer
from anomaly_detection_engine.observability.logging_config import configure_logging
from anomaly_detection_engine.observability.metrics import IngestionMetrics
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

    Only meaningful for the live-source demo path: there is no real
    fixtures/event catalog yet (see architecture.md's Matching Layer for
    what a real deployment would resolve against instead), so this is a
    stand-in that trusts the source's own team names as canonical.
    """
    events: dict[tuple[str, str], Event] = {}
    for raw in raw_events:
        key = (raw.home_team, raw.away_team)
        if key not in events:
            index = len(events) + 1
            events[key] = Event(
                id=f"event-{index:03d}",
                sport=raw.sport,
                league=raw.league,
                home_team=Team(f"home-{index}", raw.home_team),
                away_team=Team(f"away-{index}", raw.away_team),
                start_time=raw.start_time,
            )
    return list(events.values())


def build_collector_and_events() -> tuple[OddsCollector, list[Event]]:
    if os.environ.get("ODDS_SOURCE") == "the-odds-api":
        sport_key = os.environ.get("ODDS_SPORT_KEY", "soccer_epl")
        live_collector = TheOddsApiCollector(sport_key)
        raw_events = live_collector.collect()
        events = build_events_from_raw(raw_events)
        return _ReplayCollector(live_collector.source, raw_events), events

    project_root = Path(__file__).resolve().parents[2]
    sample_path = project_root / "data" / "samples" / "odds_sample.json"
    return JsonOddsCollector(sample_path), build_demo_events()


def main() -> None:
    configure_logging()

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)

    odds_repository = OddsRepository(connection)
    collector_run_repository = CollectorRunRepository(connection)
    raw_payload_repository = RawPayloadRepository(connection)

    collector, events = build_collector_and_events()
    print(f"Source: {collector.source} ({len(events)} events)")

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
        f"Collector run {run.id}: {run.status.value} "
        f"({run.records_accepted}/{run.records_received} accepted)"
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

    print("\n" + "-" * 72)
    print(f"Metrics: {metrics.snapshot()}")


if __name__ == "__main__":
    main()
