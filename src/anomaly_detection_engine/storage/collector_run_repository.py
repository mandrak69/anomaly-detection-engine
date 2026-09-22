from datetime import datetime
from sqlite3 import Connection, Row

from anomaly_detection_engine.models.collector_run import CollectorRun, CollectorRunStatus


class CollectorRunRepository:
    def __init__(self, connection: Connection):
        self._connection = connection

    def save(self, run: CollectorRun) -> None:
        """Inserts a full, already-final CollectorRun row in one shot --
        for a caller (test, one-off script) building a complete record
        directly rather than living through the RUNNING -> final
        two-phase lifecycle. OddsIngestionService.run() itself never
        calls this: it uses start()/finish() instead, specifically so
        the row exists (as RUNNING) before any odds_snapshots/
        raw_payloads row referencing it is written -- see those methods.
        """
        self._connection.execute(
            """
            INSERT INTO collector_runs (
                id,
                source,
                started_at,
                finished_at,
                status,
                records_received,
                records_accepted,
                records_rejected,
                collector_version,
                error_type,
                error_message,
                provider_id,
                parser_version,
                source_payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.id,
                run.source,
                run.started_at.isoformat(),
                run.finished_at.isoformat() if run.finished_at else None,
                run.status.value,
                run.records_received,
                run.records_accepted,
                run.records_rejected,
                run.collector_version,
                run.error_type,
                run.error_message,
                run.provider_id,
                run.parser_version,
                run.source_payload,
            ),
        )
        self._connection.commit()

    def start(self, run: CollectorRun) -> None:
        """Inserts a new CollectorRun row while the run is still in
        progress -- run.status must be RUNNING and run.finished_at must
        be None. Called once, at the very start of
        OddsIngestionService.run(), before any odds_snapshots/
        raw_payloads row referencing this run_id is written, so those
        rows' FK (migration 11) is always backed by a real parent row
        from the moment they're inserted, never pointing at a run_id
        that doesn't exist in this table yet.
        """
        assert run.status == CollectorRunStatus.RUNNING
        assert run.finished_at is None
        self.save(run)

    def finish(self, run: CollectorRun) -> None:
        """Updates the existing RUNNING row (inserted by start(), same
        run.id) to its final status/counts/finished_at -- an UPDATE, not
        a second INSERT, since the row already exists. run.status must
        not be RUNNING and run.finished_at must be set.
        """
        assert run.status != CollectorRunStatus.RUNNING
        assert run.finished_at is not None
        self._connection.execute(
            """
            UPDATE collector_runs SET
                finished_at = ?,
                status = ?,
                records_received = ?,
                records_accepted = ?,
                records_rejected = ?,
                collector_version = ?,
                error_type = ?,
                error_message = ?,
                provider_id = ?,
                parser_version = ?,
                source_payload = ?
            WHERE id = ?
            """,
            (
                run.finished_at.isoformat(),
                run.status.value,
                run.records_received,
                run.records_accepted,
                run.records_rejected,
                run.collector_version,
                run.error_type,
                run.error_message,
                run.provider_id,
                run.parser_version,
                run.source_payload,
                run.id,
            ),
        )
        self._connection.commit()

    def recover_stale_running(
        self, *, older_than: datetime, recovered_at: datetime
    ) -> list[str]:
        """Marks FAILED every RUNNING row whose started_at is older than
        `older_than` -- called once at process startup (see
        runtime.build_runtime), not periodically inside a live process.

        A row is written as RUNNING at the very start of
        OddsIngestionService.run() and only ever moved to a final status
        by finish() at the end (see start()/finish()) -- a process killed
        or crashed in between (the AppHang-style terminations this
        project has hit in practice) leaves that row RUNNING forever,
        with nothing else to ever notice or explain it, unless something
        explicitly looks for it. This is that explicit check, not a
        silent cleanup: error_type="process_interrupted" makes the cause
        visible in the same collector_runs row any other failure would
        show up in, rather than a plain status flip with no explanation.

        older_than (not "any RUNNING row") exists specifically because
        this project supports multiple concurrent watch_capture.py-
        spawned processes sharing one database file (see FixtureCatalog's
        own docstring) -- a RUNNING row could genuinely belong to a
        sibling process still in the middle of its own run, not this
        process's own past crash. Real runs finish in low single-digit
        seconds (see CollectorRun.duration_seconds), so a threshold of
        AppConfig.stale_running_threshold (default 60 minutes) is
        deliberately generous: marking a still-genuinely-running
        sibling's row FAILED out from under it would be worse than
        leaving a truly stuck row RUNNING a little longer before the
        next startup catches it.

        Returns the recovered run ids, purely so the caller can log them
        -- this method already persists the change itself.

        One atomic UPDATE ... WHERE status = 'running' AND started_at < ?,
        not a SELECT followed by a separate UPDATE-by-id: with multiple
        processes sharing this database, a row that was RUNNING when the
        SELECT read it could have been finish()ed by its own process
        (moved to SUCCESS/PARTIAL/FAILED) in the gap before a later,
        separate UPDATE applied -- overwriting a legitimate result with
        a wrong FAILED/"process_interrupted" verdict. Re-checking
        `status = 'running'` as part of the same UPDATE that changes it
        closes that gap: a row can only be caught by this statement if
        it is still RUNNING at the exact moment SQLite applies the
        write, the same guarantee RETURNING then reads back from.
        """
        rows = self._connection.execute(
            """
            UPDATE collector_runs SET
                status = ?,
                finished_at = ?,
                error_type = ?,
                error_message = ?
            WHERE status = ? AND started_at < ?
            RETURNING id
            """,
            (
                CollectorRunStatus.FAILED.value,
                recovered_at.isoformat(),
                "process_interrupted",
                "Left in RUNNING state past process startup -- the process "
                "that started this run likely crashed or was killed before "
                "reaching finish() (see recover_stale_running).",
                CollectorRunStatus.RUNNING.value,
                older_than.isoformat(),
            ),
        ).fetchall()
        self._connection.commit()

        return [row["id"] for row in rows]

    def find_by_id(self, run_id: str) -> CollectorRun | None:
        row = self._connection.execute(
            "SELECT * FROM collector_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

        return self._map_row(row) if row else None

    def find_latest_by_source(self, source: str) -> CollectorRun | None:
        """Most recent CollectorRun for one exact `source` string (e.g.
        "the-odds-api:soccer_epl"), regardless of its status -- used to
        rate-limit a collector whose provider's request budget can't
        simply ride the same per-cycle cadence every other collector
        uses (see pipeline._the_odds_api_supplemental_collector). A
        FAILED run still counts as "we tried recently" here: the point
        is spacing out requests actually sent, not just successful ones.
        """
        row = self._connection.execute(
            "SELECT * FROM collector_runs WHERE source = ? ORDER BY started_at DESC LIMIT 1",
            (source,),
        ).fetchone()

        return self._map_row(row) if row else None

    @staticmethod
    def _map_row(row: Row) -> CollectorRun:
        return CollectorRun(
            id=row["id"],
            source=row["source"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            status=CollectorRunStatus(row["status"]),
            records_received=row["records_received"],
            records_accepted=row["records_accepted"],
            records_rejected=row["records_rejected"],
            collector_version=row["collector_version"],
            error_type=row["error_type"],
            error_message=row["error_message"],
            provider_id=row["provider_id"],
            parser_version=row["parser_version"],
            source_payload=row["source_payload"],
        )
