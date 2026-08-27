from datetime import datetime

from anomaly_detection_engine.models.collector_run import (
    CollectorRun,
    CollectorRunStatus,
)


def test_collector_run_calculates_duration_and_acceptance_rate():
    run = CollectorRun(
        id="run-001",
        source="Mozzart",
        started_at=datetime.fromisoformat(
            "2026-08-27T08:00:00+00:00"
        ),
        finished_at=datetime.fromisoformat(
            "2026-08-27T08:00:04+00:00"
        ),
        status=CollectorRunStatus.PARTIAL,
        records_received=100,
        records_accepted=95,
        records_rejected=5,
        collector_version="0.1.0",
    )

    assert run.duration_seconds == 4.0
    assert run.acceptance_rate == 0.95

def test_collector_run_with_no_records_has_zero_acceptance_rate():
    run = CollectorRun(
        id="run-002",
        source="Mozzart",
        started_at=datetime.fromisoformat(
            "2026-08-27T08:00:00+00:00"
        ),
        finished_at=datetime.fromisoformat(
            "2026-08-27T08:00:01+00:00"
        ),
        status=CollectorRunStatus.SUCCESS,
        records_received=0,
        records_accepted=0,
        records_rejected=0,
    )

    assert run.acceptance_rate == 0.0