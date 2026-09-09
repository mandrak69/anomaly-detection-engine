from dataclasses import dataclass
from datetime import datetime, timedelta

from anomaly_detection_engine.models.odds import OddsSnapshot


@dataclass(frozen=True)
class FreshnessPolicy:
    max_snapshot_age: timedelta
    max_observation_spread: timedelta
    # A snapshot's quote_time landing a few seconds ahead of analysis_time
    # isn't data corruption -- it's ordinary clock skew between this
    # process and a real provider's own clock. Only a skew *larger* than
    # this counts as "snapshot-from-future"; unlike the two thresholds
    # above (genuine per-deployment business decisions with no universal
    # default), this is a technical tolerance most callers want the same
    # sensible value for, so it defaults rather than being required.
    allowed_future_skew: timedelta = timedelta(seconds=10)


@dataclass(frozen=True)
class FreshnessResult:
    valid: bool
    # The individually-fresh subset of whatever was passed in -- what a
    # caller should actually run best-odds/outlier detection over, not
    # the original, possibly-mixed list. Empty whenever valid is False.
    fresh_snapshots: list[OddsSnapshot]
    # bookmaker_id of every snapshot excluded for being individually too
    # old or too far in the future -- kept for visibility/logging, no
    # longer something that by itself makes the whole batch invalid (see
    # validate_freshness's docstring for why).
    stale_sources: tuple[str, ...]
    max_age_seconds: float
    observation_spread_seconds: float
    reason: str | None


def validate_freshness(
    snapshots: list[OddsSnapshot],
    *,
    analysis_time: datetime,
    policy: FreshnessPolicy,
) -> FreshnessResult:
    """Filters snapshots individually for freshness, then checks that
    the surviving ("fresh") cohort was observed closely enough together
    to be compared at all.

    One stale (or clock-skewed-into-the-future) bookmaker no longer
    invalidates every other bookmaker's genuinely fresh quote for the
    same event/market -- a real cross-provider event can easily have
    Bet365/Pinnacle/Unibet all fresh and one slower-updating bookmaker
    45 minutes stale; the old all-or-nothing check would have thrown out
    a real, evaluable surebet among the first three just because the
    fourth lagged. Both individual staleness *and* individual
    future-skew are filtered the same way -- a single bookmaker with a
    skewed clock shouldn't block everyone else's data either, the same
    reasoning extended to both failure modes.

    Age and spread are measured against each snapshot's quote_time
    (source_timestamp if the source provided one, else observed_at), not
    observed_at directly -- see OddsSnapshot.quote_time. observed_at is
    only "when we polled", which can look arbitrarily fresh even for a
    quote the source itself computed or cached hours earlier; a source
    that timestamps its own prices is telling us something age-relevant
    that a poll timestamp alone can't.

    Once the fresh cohort is known, observation_spread is checked *only*
    across it (not the original, possibly-wider list) -- comparing odds
    across bookmakers only means something if they were simultaneously
    valid, and a spread computed against snapshots already excluded for
    staleness would be a meaningless number. If the fresh cohort itself
    still spans more than max_observation_spread, the whole sweep is
    still "couldn't tell this cycle" (reason="observation-spread-too-
    large"), not a further per-snapshot filter -- narrowing further
    would mean guessing which subset of an already-fresh cohort is the
    "right" one to compare, which this function has no basis to decide.
    """
    if not snapshots:
        return FreshnessResult(
            valid=False,
            fresh_snapshots=[],
            stale_sources=(),
            max_age_seconds=0.0,
            observation_spread_seconds=0.0,
            reason="no-snapshots",
        )

    stale_sources: set[str] = set()
    fresh: list[OddsSnapshot] = []

    for snapshot in snapshots:
        age = analysis_time - snapshot.quote_time

        if age < -policy.allowed_future_skew or age > policy.max_snapshot_age:
            stale_sources.add(snapshot.bookmaker.id)
            continue

        fresh.append(snapshot)

    if not fresh:
        return FreshnessResult(
            valid=False,
            fresh_snapshots=[],
            stale_sources=tuple(sorted(stale_sources)),
            max_age_seconds=0.0,
            observation_spread_seconds=0.0,
            reason="no-fresh-snapshots",
        )

    observed_times = [snapshot.quote_time for snapshot in fresh]
    oldest_observation = min(observed_times)
    newest_observation = max(observed_times)
    observation_spread = newest_observation - oldest_observation
    max_age = max(analysis_time - snapshot.quote_time for snapshot in fresh)

    if observation_spread > policy.max_observation_spread:
        return FreshnessResult(
            valid=False,
            fresh_snapshots=[],
            stale_sources=tuple(sorted(stale_sources)),
            max_age_seconds=max_age.total_seconds(),
            observation_spread_seconds=observation_spread.total_seconds(),
            reason="observation-spread-too-large",
        )

    return FreshnessResult(
        valid=True,
        fresh_snapshots=fresh,
        stale_sources=tuple(sorted(stale_sources)),
        max_age_seconds=max_age.total_seconds(),
        observation_spread_seconds=observation_spread.total_seconds(),
        reason=None,
    )
