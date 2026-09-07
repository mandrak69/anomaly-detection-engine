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
    """Age and spread are measured against each snapshot's quote_time
    (source_timestamp if the source provided one, else observed_at), not
    observed_at directly -- see OddsSnapshot.quote_time. observed_at is
    only "when we polled", which can look arbitrarily fresh even for a
    quote the source itself computed or cached hours earlier; a source
    that timestamps its own prices is telling us something age-relevant
    that a poll timestamp alone can't.

    A quote_time up to policy.allowed_future_skew ahead of analysis_time
    is tolerated, not rejected as "snapshot-from-future" -- a few
    seconds of clock skew between this process and a real provider's own
    clock is normal and not evidence of corrupted data. Only a skew
    larger than that is treated as genuinely suspicious.
    """
    if not snapshots:
        return FreshnessResult(
            valid=False,
            stale_sources=(),
            max_age_seconds=0.0,
            observation_spread_seconds=0.0,
            reason="no-snapshots",
        )

    stale_sources: set[str] = set()
    ages: list[timedelta] = []

    for snapshot in snapshots:
        age = analysis_time - snapshot.quote_time

        if age < -policy.allowed_future_skew:
            return FreshnessResult(
                valid=False,
                stale_sources=(),
                max_age_seconds=0.0,
                observation_spread_seconds=0.0,
                reason="snapshot-from-future",
            )

        ages.append(age)

        if age > policy.max_snapshot_age:
            stale_sources.add(snapshot.bookmaker.id)

    observed_times = [snapshot.quote_time for snapshot in snapshots]

    oldest_observation = min(observed_times)
    newest_observation = max(observed_times)

    observation_spread = newest_observation - oldest_observation
    max_age = max(ages)

    if stale_sources:
        return FreshnessResult(
            valid=False,
            stale_sources=tuple(sorted(stale_sources)),
            max_age_seconds=max_age.total_seconds(),
            observation_spread_seconds=observation_spread.total_seconds(),
            reason="stale-snapshots",
        )

    if observation_spread > policy.max_observation_spread:
        return FreshnessResult(
            valid=False,
            stale_sources=(),
            max_age_seconds=max_age.total_seconds(),
            observation_spread_seconds=observation_spread.total_seconds(),
            reason="observation-spread-too-large",
        )

    return FreshnessResult(
        valid=True,
        stale_sources=(),
        max_age_seconds=max_age.total_seconds(),
        observation_spread_seconds=observation_spread.total_seconds(),
        reason=None,
    )