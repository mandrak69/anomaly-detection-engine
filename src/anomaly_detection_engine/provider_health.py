from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from sqlite3 import Connection


@dataclass(frozen=True)
class ProviderHealth:
    """Recent health and data freshness for one collector_runs.source.

    Two different kinds of "recent" matter here and are deliberately not
    conflated: last_success_at/the failure counters are about whether the
    collector itself runs without error, while last_fresh_capture_at --
    the most recent run that actually produced a non-empty source_payload,
    not just "ran without raising" -- is about whether the underlying data
    is actually current. A manual-capture source (see collectors.
    manual_capture_collector.ManualCaptureCollector.collect) reports
    SUCCESS on every cycle even when no new drop file exists at all
    (collect() returns an empty result, not an error); without this
    distinction a HAR import nobody has re-run in a week looks perfectly
    healthy.
    """

    source: str
    last_run_at: datetime
    last_run_status: str
    last_success_at: datetime | None
    last_fresh_capture_at: datetime | None
    consecutive_failures: int
    window_hours: float
    runs_in_window: int
    failures_in_window: int
    last_error_type: str | None
    last_error_message: str | None

    @property
    def failure_rate_in_window(self) -> float:
        if self.runs_in_window == 0:
            return 0.0
        return self.failures_in_window / self.runs_in_window

    def staleness_seconds(self, *, now: datetime) -> float | None:
        """None means no run of this source has ever produced fresh data
        -- distinct from, and worse than, a large-but-known staleness."""
        if self.last_fresh_capture_at is None:
            return None
        return (now - self.last_fresh_capture_at).total_seconds()

    def is_stale(self, *, now: datetime, stale_after_seconds: float) -> bool:
        staleness = self.staleness_seconds(now=now)
        return staleness is None or staleness > stale_after_seconds

    def to_evidence(self, *, now: datetime, stale_after_seconds: float) -> dict[str, object]:
        staleness = self.staleness_seconds(now=now)
        return {
            "source": self.source,
            "last_run_at": self.last_run_at.isoformat(),
            "last_run_status": self.last_run_status,
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
            "last_fresh_capture_at": (
                self.last_fresh_capture_at.isoformat() if self.last_fresh_capture_at else None
            ),
            "staleness_hours": round(staleness / 3600, 1) if staleness is not None else None,
            "is_stale": self.is_stale(now=now, stale_after_seconds=stale_after_seconds),
            "consecutive_failures": self.consecutive_failures,
            "runs_in_window": self.runs_in_window,
            "failures_in_window": self.failures_in_window,
            "failure_rate_in_window": round(self.failure_rate_in_window, 3),
            "last_error_type": self.last_error_type,
            "last_error_message": self.last_error_message,
        }


def provider_health_report(
    connection: Connection,
    *,
    window_hours: float = 24.0,
    now: datetime | None = None,
) -> list[ProviderHealth]:
    """One ProviderHealth per distinct collector_runs.source, sorted by
    source name. Requires connection.row_factory = sqlite3.Row.

    `partial` is treated as a form of success here, not a failure: it is
    api-football's own normal state under this project's free-tier
    pagination cap (see api_football_collector's own
    "pagination_capped_by_plan" warning), not an error condition --
    counting it as a failure would make that one source look permanently
    unhealthy for a constraint that was never a bug.
    """
    now = now if now is not None else datetime.now(UTC)
    window_start = now - timedelta(hours=window_hours)

    sources = [
        row[0] for row in connection.execute("SELECT DISTINCT source FROM collector_runs")
    ]

    reports: list[ProviderHealth] = []
    for source in sorted(sources):
        latest = connection.execute(
            """
            SELECT started_at, status, error_type, error_message
            FROM collector_runs WHERE source = ?
            ORDER BY started_at DESC LIMIT 1
            """,
            (source,),
        ).fetchone()

        last_success_row = connection.execute(
            """
            SELECT started_at FROM collector_runs
            WHERE source = ? AND status IN ('success', 'partial')
            ORDER BY started_at DESC LIMIT 1
            """,
            (source,),
        ).fetchone()

        last_capture_row = connection.execute(
            """
            SELECT started_at FROM collector_runs
            WHERE source = ? AND source_payload IS NOT NULL
            ORDER BY started_at DESC LIMIT 1
            """,
            (source,),
        ).fetchone()

        window_counts = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failures
            FROM collector_runs
            WHERE source = ? AND started_at >= ?
            """,
            (source, window_start.isoformat()),
        ).fetchone()

        recent_statuses = [
            row[0]
            for row in connection.execute(
                "SELECT status FROM collector_runs WHERE source = ? "
                "ORDER BY started_at DESC LIMIT 50",
                (source,),
            )
        ]
        consecutive_failures = 0
        for status in recent_statuses:
            if status != "failed":
                break
            consecutive_failures += 1

        reports.append(
            ProviderHealth(
                source=source,
                last_run_at=datetime.fromisoformat(latest["started_at"]),
                last_run_status=latest["status"],
                last_success_at=(
                    datetime.fromisoformat(last_success_row["started_at"])
                    if last_success_row
                    else None
                ),
                last_fresh_capture_at=(
                    datetime.fromisoformat(last_capture_row["started_at"])
                    if last_capture_row
                    else None
                ),
                consecutive_failures=consecutive_failures,
                window_hours=window_hours,
                runs_in_window=window_counts["total"] or 0,
                failures_in_window=window_counts["failures"] or 0,
                last_error_type=latest["error_type"],
                last_error_message=latest["error_message"],
            )
        )
    return reports
