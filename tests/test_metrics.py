from datetime import datetime, timezone

from anomaly_detection_engine.models.collector_run import CollectorRun, CollectorRunStatus
from anomaly_detection_engine.observability.metrics import IngestionMetrics


def build_run(status: CollectorRunStatus, received: int, accepted: int, rejected: int) -> CollectorRun:
    return CollectorRun(
        id="run-001",
        source="stub",
        started_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
        finished_at=datetime(2026, 8, 27, 0, 0, 4, tzinfo=timezone.utc),
        status=status,
        records_received=received,
        records_accepted=accepted,
        records_rejected=rejected,
    )


def test_accumulates_totals_across_multiple_runs():
    metrics = IngestionMetrics()

    metrics.record_run(build_run(CollectorRunStatus.SUCCESS, 3, 3, 0), [])
    metrics.record_run(build_run(CollectorRunStatus.PARTIAL, 5, 4, 1), ["semantic: invalid-odds-value"])

    snapshot = metrics.snapshot()

    assert snapshot["total_runs"] == 2
    assert snapshot["total_received"] == 8
    assert snapshot["total_accepted"] == 7
    assert snapshot["total_rejected"] == 1
    assert snapshot["runs_by_status"] == {"success": 1, "partial": 1}


def test_groups_rejection_reasons_by_stage_prefix():
    metrics = IngestionMetrics()

    metrics.record_run(
        build_run(CollectorRunStatus.PARTIAL, 3, 1, 2),
        [
            "structural: missing-home-team",
            "identity: no-event-match",
        ],
    )
    metrics.record_run(
        build_run(CollectorRunStatus.PARTIAL, 2, 1, 1),
        ["structural: missing-away-team"],
    )

    snapshot = metrics.snapshot()

    assert snapshot["rejections_by_reason"] == {"structural": 2, "identity": 1}
