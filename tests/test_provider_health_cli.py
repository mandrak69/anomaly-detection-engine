import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from anomaly_detection_engine.storage.database import create_connection, initialize_database
from provider_health import main


def _now() -> datetime:
    # main() always computes its own "now" from the real wall clock (it
    # takes no injectable clock) -- fixtures below are built relative to
    # this same real now, not a fixed constant, so the test stays
    # correct regardless of when it actually runs.
    return datetime.now(UTC)


def make_db(tmp_path, *, started_at: datetime, status: str = "success", source_payload=None):
    path = tmp_path / "provider-health-cli.db"
    connection = create_connection(path)
    initialize_database(connection)
    connection.execute(
        """
        INSERT INTO collector_runs (
            id, source, started_at, finished_at, status,
            records_received, records_accepted, records_rejected, source_payload
        ) VALUES ('r1', 'mozzart-file:mozzart', ?, ?, ?, 0, 0, 0, ?)
        """,
        (started_at.isoformat(), started_at.isoformat(), status, source_payload),
    )
    connection.commit()
    connection.close()
    return path


def test_cli_reports_stale_source_as_text(tmp_path, capsys):
    path = make_db(tmp_path, started_at=_now() - timedelta(days=8), source_payload="{}")

    main(["--db-path", str(path)])

    output = capsys.readouterr().out
    assert "mozzart-file:mozzart" in output
    assert "STALE" in output


def test_cli_fail_on_stale_exits_nonzero(tmp_path):
    path = make_db(tmp_path, started_at=_now() - timedelta(days=8), source_payload="{}")

    with pytest.raises(SystemExit) as exc_info:
        main(["--db-path", str(path), "--fail-on-stale", "--stale-after-hours", "1"])

    assert exc_info.value.code == 2


def test_cli_json_output_is_well_formed(tmp_path, capsys):
    path = make_db(tmp_path, started_at=_now(), source_payload="{}")

    main(["--db-path", str(path), "--json"])

    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["source"] == "mozzart-file:mozzart"
    assert payload[0]["is_stale"] is False


def test_cli_does_not_fail_on_fresh_data(tmp_path):
    path = make_db(tmp_path, started_at=_now(), source_payload="{}")

    main(["--db-path", str(path), "--fail-on-stale", "--fail-on-failures"])


def test_cli_opens_database_read_only(tmp_path):
    path = make_db(tmp_path, started_at=_now(), source_payload="{}")

    main(["--db-path", str(path)])

    check = sqlite3.connect(path)
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()
