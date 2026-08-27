from datetime import datetime
from pathlib import Path

from anomaly_detection_engine.analysis.arbitrage import calculate_arbitrage
from anomaly_detection_engine.analysis.best_odds import find_best_odds
from anomaly_detection_engine.collectors.json_collector import (
    DEFAULT_MARKET,
    JsonOddsCollector,
)
from anomaly_detection_engine.matching.event_matcher import EventMatcher
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.normalization.team_normalizer import TeamNormalizer


ALIASES = {
    "Man Utd": "Manchester United",
    "Man. United": "Manchester United",
    "Liverpool FC": "Liverpool",
    "Liv": "Liverpool",
    "FCB": "Barcelona",
    "Barca": "Barcelona",
    "Real Madrid CF": "Real Madrid",
}


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


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    sample_path = project_root / "data" / "samples" / "odds_sample.json"

    collector = JsonOddsCollector(sample_path)

    raw_events = collector.collect()
    events = build_demo_events()

    canonical_names = {
        event.home_team.canonical_name
        for event in events
    } | {
        event.away_team.canonical_name
        for event in events
    }

    normalizer = TeamNormalizer(canonical_names, ALIASES, fuzzy_threshold=80)
    matcher = EventMatcher(events, normalizer)

    snapshots: list[OddsSnapshot] = []

    for raw_event in raw_events:
        match = matcher.match(
            sport=raw_event.sport,
            league=raw_event.league,
            home_team_raw=raw_event.home_team,
            away_team_raw=raw_event.away_team,
            start_time=raw_event.start_time,
        )

        if match.event is None:
            print(
                f"SKIP: could not match {raw_event.home_team} vs "
                f"{raw_event.away_team} ({match.reason})"
            )
            continue

        bookmaker = Bookmaker(raw_event.source.lower(), raw_event.source)

        for outcome in ("1", "X", "2"):
            snapshots.append(
                OddsSnapshot(
                    event_id=match.event.id,
                    bookmaker=bookmaker,
                    market=raw_event.market,
                    outcome=outcome,
                    odds=raw_event.odds[outcome],
                    observed_at=raw_event.observed_at,
                )
            )

    for event in events:
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


if __name__ == "__main__":
    main()
