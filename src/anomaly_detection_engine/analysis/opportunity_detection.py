from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.analysis.arbitrage import calculate_arbitrage
from anomaly_detection_engine.analysis.best_odds import find_best_odds
from anomaly_detection_engine.analysis.freshness import FreshnessPolicy, validate_freshness
from anomaly_detection_engine.analysis.outlier_detector import detect_outliers
from anomaly_detection_engine.models.event import Event
from anomaly_detection_engine.models.market import MarketIdentity, required_outcomes
from anomaly_detection_engine.models.signal import SignalIdentity
from anomaly_detection_engine.storage.odds_repository import OddsRepository


@dataclass(frozen=True)
class SurebetLeg:
    outcome: str
    bookmaker: str
    odds: Decimal
    # Exact quote provenance (see OddsSnapshot.collector_run_id/
    # quote_time) -- lets any future report trace a persisted leg back
    # to precisely which run/provider/original payload it came from,
    # instead of trying to reconstruct it later from timing alone.
    bookmaker_id: str | None
    quote_time: datetime | None
    collector_run_id: str | None
    # What fraction of total stake this leg gets so every outcome pays
    # out identically -- see arbitrage.ArbitrageResult.stake_percent.
    stake_percent: Decimal


@dataclass(frozen=True)
class SurebetCandidate:
    """One arbitrage: a single opportunity with one leg per required
    outcome of the market (see models.market.required_outcomes -- three
    for THREE_WAY, two for TOTALS), not that many separate opportunities
    -- this is the structural difference from OpportunityRow
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
    # Exact quote provenance -- see SurebetLeg's own fields for why.
    bookmaker_id: str | None
    quote_time: datetime | None
    collector_run_id: str | None


@dataclass(frozen=True)
class SurebetDetectionSweep:
    """candidates plus which (event, market) combinations were actually
    evaluated this sweep -- distinguishing "evaluated, genuinely no
    surebet" from "not evaluated at all" (missing/stale data, or missing
    one of the market's required outcomes -- see
    models.market.required_outcomes) matters to a caller like
    SignalRepository.reconcile(): resolving an ACTIVE signal is only
    correct in the first case. An event skipped for missing snapshots,
    failed freshness, or fewer than all of the market's required
    outcomes currently quoted is absent from both candidates *and*
    evaluated_keys, so it must not be resolved just because it didn't
    produce a candidate this time -- a previously-ACTIVE surebet whose
    last leg simply isn't being quoted this poll is not the same as that
    arbitrage having closed. outcome is always None here -- see
    SignalIdentity.
    """

    candidates: list[SurebetCandidate]
    evaluated_keys: frozenset[SignalIdentity]


@dataclass(frozen=True)
class ValueGapDetectionSweep:
    """See SurebetDetectionSweep for the general distinction. VALUE_GAP
    additionally needs *per-outcome* evaluation granularity, not just
    per-event: detect_outliers skips any outcome with fewer than
    min_bookmakers snapshots, so one outcome for an event can be
    genuinely evaluated while a sibling outcome (fewer bookmakers
    quoting it, or just missing this sweep) was not -- evaluated_keys
    reflects that per-outcome, not just per-event.
    """

    candidates: list[ValueGapCandidate]
    evaluated_keys: frozenset[SignalIdentity]


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

    Returns both the candidates and which (event, market) combinations
    were actually evaluated (see SurebetDetectionSweep) -- an event with
    no snapshots yet, whose snapshots failed freshness, or for which
    fewer than all of required_outcomes(market.market_type) are
    currently quoted is absent from evaluated_keys as well as
    candidates, so a caller reconciling persisted signals can tell
    "genuinely no surebet here" apart from "couldn't tell this sweep".
    Which outcomes must all be present, and how many-way the arbitrage
    math is, both come from the market's own type (see
    models.market.required_outcomes) -- not hardcoded to three-way 1X2.
    """
    needed = required_outcomes(market.market_type)
    needed_set = frozenset(needed)

    candidates: list[SurebetCandidate] = []
    evaluated_keys: set[SignalIdentity] = set()

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
        snapshots = freshness.fresh_snapshots

        best = find_best_odds(snapshots, event_id=event.id, market=market)
        if set(best) != needed_set:
            # Missing an outcome entirely (e.g. no bookmaker currently
            # quotes "X", or "UNDER" for a TOTALS market) is the same
            # "couldn't tell" case freshness failing already is -- not
            # "evaluated, no surebet". If a previously-ACTIVE surebet's
            # last leg simply stopped being quoted this poll, that must
            # not be resolvable: nothing here confirms the arbitrage is
            # actually gone, only that this sweep can't see every leg to
            # check.
            continue

        # From here on the event's data was good enough to draw a real
        # conclusion from -- "no candidate" past this point means "no
        # surebet", not "couldn't tell". outcome=None: SUREBET has no
        # per-outcome identity (see SignalIdentity).
        evaluated_keys.add(SignalIdentity(event_id=event.id, market=market, outcome=None))

        arbitrage = calculate_arbitrage(best, required_outcomes=needed)
        if not arbitrage.is_surebet:
            continue

        legs = tuple(
            SurebetLeg(
                outcome=outcome,
                bookmaker=item.bookmaker_name,
                odds=item.odds,
                bookmaker_id=item.bookmaker_id,
                quote_time=item.quote_time,
                collector_run_id=item.collector_run_id,
                stake_percent=arbitrage.stake_percent[outcome],
            )
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

    return SurebetDetectionSweep(candidates=candidates, evaluated_keys=frozenset(evaluated_keys))


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

    evaluated_keys is computed per-outcome, not just per-event: an event
    passing freshness doesn't mean every one of its outcomes had enough
    bookmakers to evaluate. detect_outliers itself skips any outcome
    with fewer than min_bookmakers snapshots (comparing a price against
    a "consensus" of one other price isn't meaningful) -- the count
    here mirrors that same threshold so the two stay in lockstep by
    construction, without detect_outliers needing to report anything
    back itself.
    """
    candidates: list[ValueGapCandidate] = []
    evaluated_keys: set[SignalIdentity] = set()

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
        snapshots = freshness.fresh_snapshots

        outcome_counts: dict[str, int] = {}
        for snapshot in snapshots:
            outcome_counts[snapshot.outcome] = outcome_counts.get(snapshot.outcome, 0) + 1
        for outcome, count in outcome_counts.items():
            if count >= min_bookmakers:
                evaluated_keys.add(
                    SignalIdentity(event_id=event.id, market=market, outcome=outcome)
                )

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
                    bookmaker_id=outlier.bookmaker_id,
                    quote_time=outlier.quote_time,
                    collector_run_id=outlier.collector_run_id,
                )
            )

    return ValueGapDetectionSweep(candidates=candidates, evaluated_keys=frozenset(evaluated_keys))
