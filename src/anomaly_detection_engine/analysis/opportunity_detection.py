from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.analysis.arbitrage import calculate_arbitrage
from anomaly_detection_engine.analysis.best_odds import find_best_odds
from anomaly_detection_engine.analysis.freshness import FreshnessPolicy, validate_freshness
from anomaly_detection_engine.analysis.outlier_detector import detect_outliers
from anomaly_detection_engine.models.event import Event
from anomaly_detection_engine.models.market import MarketIdentity
from anomaly_detection_engine.storage.odds_repository import OddsRepository

# Shared signal-type labels -- defined here (not in reporting/ or storage/)
# since both depend on this module for the candidate types themselves;
# reporting.opportunity_report and storage.signal_repository both import
# these rather than defining their own copies.
SUREBET = "SUREBET"
VALUE_GAP = "VALUE_GAP"


@dataclass(frozen=True)
class SurebetLeg:
    outcome: str
    bookmaker: str
    odds: Decimal


@dataclass(frozen=True)
class SurebetCandidate:
    """One arbitrage: a single opportunity with three legs, not three
    separate ones -- this is the structural difference from OpportunityRow
    (reporting/opportunity_report.py), which flattens each leg into its
    own display row. Identity for persistence purposes is (event, market)
    -- *not* which bookmakers/odds make up the legs, since those can
    change between polls while the same underlying opportunity persists.
    """

    event: Event
    market: MarketIdentity
    profit_percent: Decimal
    legs: tuple[SurebetLeg, ...]


@dataclass(frozen=True)
class ValueGapCandidate:
    """One bookmaker pricing an outcome well above the consensus of its
    peers. Identity for persistence purposes is (event, market, outcome)
    -- the bookmaker/odds/deviation are current state, not identity: a
    different bookmaker becoming the outlier for the same outcome is a
    change to the same condition, not a new one.
    """

    event: Event
    market: MarketIdentity
    outcome: str
    bookmaker: str
    odds: Decimal
    deviation_percent: Decimal


@dataclass(frozen=True)
class SurebetDetectionSweep:
    """candidates plus which events were actually evaluated this sweep --
    distinguishing "evaluated, genuinely no surebet" from "not evaluated
    at all" (missing/stale data) matters to a caller like
    SignalRepository.reconcile(): resolving an ACTIVE signal is only
    correct in the first case. An event skipped for missing snapshots or
    failed freshness is absent from both candidates *and*
    evaluated_event_ids, so it must not be resolved just because it
    didn't produce a candidate this time.
    """

    candidates: list[SurebetCandidate]
    evaluated_event_ids: frozenset[str]


@dataclass(frozen=True)
class ValueGapDetectionSweep:
    """See SurebetDetectionSweep -- same distinction, for value gaps."""

    candidates: list[ValueGapCandidate]
    evaluated_event_ids: frozenset[str]


def detect_surebet_candidates(
    events: list[Event],
    odds_repository: OddsRepository,
    market: MarketIdentity,
    *,
    freshness_policy: FreshnessPolicy,
    analysis_time: datetime,
) -> SurebetDetectionSweep:
    """Finds every real arbitrage (calculate_arbitrage.is_surebet) across
    the given events, gated by freshness the same way opportunity_report
    is (see that module for why: comparing odds across bookmakers only
    means something if they were simultaneously valid).

    analysis_time is the "now" freshness is measured against, and is
    required rather than defaulted to real wall-clock time: it must be
    the caller's actual notion of "now" (real time in production), never
    derived from the snapshots themselves (e.g. their own newest
    observed_at) -- doing that would make every batch of snapshots look
    fresh relative to itself no matter how old they all actually are,
    which defeats the freshness check entirely. A demo/test harness
    replaying fixed historical data is expected to pass its own explicit
    stand-in "now" here; only such a caller should ever do that, not this
    function itself.

    Deliberately does not apply a "minimum profit worth reporting"
    threshold -- that is a presentation-layer decision
    (reporting.opportunity_report's min_surebet_profit_percent) about
    what is worth a human's attention, not a fact about whether the
    arbitrage exists. Anything persisted from this should keep the raw
    profit_percent so that decision can be revisited later without
    having thrown away the underlying data.

    Returns both the candidates and which events were actually evaluated
    (see SurebetDetectionSweep) -- an event with no snapshots yet, or
    whose snapshots failed freshness, is evaluated_event_ids-absent, not
    just candidate-absent, so a caller reconciling persisted signals can
    tell "genuinely no surebet here" apart from "couldn't tell this
    sweep".
    """
    candidates: list[SurebetCandidate] = []
    evaluated_event_ids: set[str] = set()

    for event in events:
        snapshots = odds_repository.find_latest_for_market(
            event_id=event.id,
            market=market,
        )
        if not snapshots:
            continue

        freshness = validate_freshness(
            snapshots,
            analysis_time=analysis_time,
            policy=freshness_policy,
        )
        if not freshness.valid:
            continue

        # From here on the event's data was good enough to draw a real
        # conclusion from -- "no candidate" past this point means "no
        # surebet", not "couldn't tell".
        evaluated_event_ids.add(event.id)

        best = find_best_odds(snapshots, event_id=event.id, market=market)
        if len(best) != 3:
            continue

        arbitrage = calculate_arbitrage(best)
        if not arbitrage.is_surebet:
            continue

        legs = tuple(
            SurebetLeg(outcome=outcome, bookmaker=item.bookmaker_name, odds=item.odds)
            for outcome, item in best.items()
        )
        candidates.append(
            SurebetCandidate(
                event=event,
                market=market,
                profit_percent=arbitrage.theoretical_profit_percent,
                legs=legs,
            )
        )

    return SurebetDetectionSweep(
        candidates=candidates, evaluated_event_ids=frozenset(evaluated_event_ids)
    )


def detect_value_gap_candidates(
    events: list[Event],
    odds_repository: OddsRepository,
    market: MarketIdentity,
    *,
    freshness_policy: FreshnessPolicy,
    analysis_time: datetime,
    threshold_percent: Decimal = Decimal("15.0"),
    min_bookmakers: int = 3,
) -> ValueGapDetectionSweep:
    """Finds every outcome priced well above its peers' consensus
    (detect_outliers, favorable direction only), gated by freshness.

    See detect_surebet_candidates for why analysis_time is required
    rather than derived from the snapshots themselves, and for why the
    return value also reports which events were actually evaluated.

    threshold_percent/min_bookmakers are passed straight through to
    detect_outliers -- unlike SUREBET's profit threshold, this is part
    of the outlier *detection* itself (what counts as an outlier at all),
    not an extra presentation-layer filter on top, so it stays here
    rather than being deferred to the report.
    """
    candidates: list[ValueGapCandidate] = []
    evaluated_event_ids: set[str] = set()

    for event in events:
        snapshots = odds_repository.find_latest_for_market(
            event_id=event.id,
            market=market,
        )
        if not snapshots:
            continue

        freshness = validate_freshness(
            snapshots,
            analysis_time=analysis_time,
            policy=freshness_policy,
        )
        if not freshness.valid:
            continue

        evaluated_event_ids.add(event.id)

        for outlier in detect_outliers(
            snapshots,
            event_id=event.id,
            market=market,
            threshold_percent=threshold_percent,
            min_bookmakers=min_bookmakers,
        ):
            if outlier.deviation_percent <= 0:
                continue

            candidates.append(
                ValueGapCandidate(
                    event=event,
                    market=market,
                    outcome=outlier.outcome,
                    bookmaker=outlier.bookmaker_name,
                    odds=outlier.odds,
                    deviation_percent=outlier.deviation_percent,
                )
            )

    return ValueGapDetectionSweep(
        candidates=candidates, evaluated_event_ids=frozenset(evaluated_event_ids)
    )
