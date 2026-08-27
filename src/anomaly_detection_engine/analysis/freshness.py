from dataclasses import dataclass
from datetime import datetime, timedelta

from anomaly_detection_engine.models.odds import OddsSnapshot


@dataclass(frozen=True)
class FreshnessPolicy:
    max_snapshot_age: timedelta
    max_observation_spread: timedelta


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
        age = analysis_time - snapshot.observed_at

        if age.total_seconds() < 0:
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

    observed_times = [snapshot.observed_at for snapshot in snapshots]

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