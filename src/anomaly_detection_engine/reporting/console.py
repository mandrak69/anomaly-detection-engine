import os
from datetime import UTC, datetime
from decimal import Decimal

from anomaly_detection_engine.analysis.arbitrage import calculate_arbitrage
from anomaly_detection_engine.analysis.best_odds import find_best_odds
from anomaly_detection_engine.analysis.freshness import validate_freshness
from anomaly_detection_engine.config import AppConfig
from anomaly_detection_engine.models.event import Event
from anomaly_detection_engine.models.market import DEFAULT_MARKET
from anomaly_detection_engine.pipeline import demo_analysis_time, resolve_freshness_policy
from anomaly_detection_engine.reporting.movement_report import (
    build_movement_report,
    render_movement_report,
)
from anomaly_detection_engine.reporting.opportunity_report import (
    build_opportunity_report,
    render_opportunity_report,
)
from anomaly_detection_engine.runtime import Runtime

# Report-only threshold ("is this surebet worth a line in the printed
# report"), deliberately not part of AppConfig -- see AppConfig's own
# docstring for why: SUREBET candidates are persisted unconditionally by
# pipeline.persist_detected_signals, so this only ever affects what this
# module prints, never what gets detected/stored.
_MIN_SUREBET_PROFIT_PERCENT_ENV_VAR = "MIN_SUREBET_PROFIT_PERCENT"


def print_reports(runtime: Runtime, events: list[Event], config: AppConfig) -> None:
    """Prints the per-event best-odds/arbitrage breakdown and the
    threshold-filtered opportunity/movement reports.

    Presentation on top of what pipeline.run_detection already computed
    and persisted -- not part of the core pipeline (see
    pipeline.run_detection's docstring), and not called by app.py's
    main() by default. Call this explicitly (a script, a REPL, a future
    CLI flag) when you actually want to see the human-facing reports;
    the core pipeline runs, detects, and persists signals without it.
    """
    analysis_time = (
        datetime.now(UTC)
        if config.odds_source != "demo"
        else demo_analysis_time(events, runtime.odds_repository)
    )
    min_surebet_profit_percent = Decimal(
        os.environ.get(_MIN_SUREBET_PROFIT_PERCENT_ENV_VAR, "1.0")
    )
    freshness_policy = resolve_freshness_policy(config)

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
            policy=freshness_policy,
        )
        if not freshness.valid:
            print(f"\nSKIP {event.display_name}: not fresh ({freshness.reason})")
            continue

        best = find_best_odds(freshness.fresh_snapshots, event_id=event.id, market=DEFAULT_MARKET)
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
        freshness_policy=freshness_policy,
        analysis_time=analysis_time,
        min_surebet_profit_percent=min_surebet_profit_percent,
        min_value_gap_percent=config.min_value_gap_percent,
    )
    print(render_opportunity_report(opportunity_rows))

    print("\n" + "=" * 72)
    print("ODDS MOVEMENT (significant change between the last two readings)")
    print("=" * 72)
    movement_rows = build_movement_report(events, runtime.odds_repository, DEFAULT_MARKET)
    print(render_movement_report(movement_rows))
