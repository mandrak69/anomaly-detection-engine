# Operations runbook

This runbook covers the supported safety checks around identity changes and the
SQLite database. Run commands from the repository root with the project virtual
environment active.

## Before changing matching or normalization

1. Create a verified backup.
2. Run the identity audit and save its output as the baseline.
3. Run the proposed ingestion/replay against a database copy.
4. Review the JSON diff before running anything against the live database.

## Backup and verification

Create a timestamped backup using the configured `DB_PATH`:

```bash
python scripts/database_backup.py backup
```

Select explicit paths when reviewing a production copy:

```bash
python scripts/database_backup.py backup \
  --source-db data/anomaly_detection.db \
  --output backups/before-identity-change.db
```

The command:

- refuses to overwrite an existing database or manifest;
- uses SQLite's online-backup API so committed WAL pages are included;
- runs `PRAGMA integrity_check` and `PRAGMA foreign_key_check`;
- writes `<backup>.manifest.json` containing the SHA-256 checksum, schema
  version, size, and creation time.

Verify a backup later:

```bash
python scripts/database_backup.py verify backups/before-identity-change.db
```

If its adjacent manifest exists, checksum verification is automatic. A valid
backup should also be restored to a separate location and opened by the normal
inspection tools periodically; a backup that has never been restored is not a
fully tested recovery plan.

## Read-only identity audit

```bash
python scripts/audit_identity.py
python scripts/audit_identity.py --json > identity-audit.json
python scripts/audit_identity.py --fail-on-findings
```

The audit opens the database with SQLite `mode=ro` and `query_only`. It reports,
but never repairs:

- exact duplicate canonical fixtures;
- orphan provisional teams and competitions;
- canonical entities marked `CONFLICT`;
- `SUSPECT` and `UNVERIFIED` mappings;
- multiple verified API-Football IDs attached to one canonical entity;
- countryless competition rows that collide with several country-specific rows.

An audit finding is evidence to inspect, not permission to auto-delete or merge.

## Replay against a database copy

The replay harness creates a consistent copy, sets `DB_PATH` for only the child
command, and captures before/after counts and identity states:

```bash
python scripts/replay_against_copy.py \
  --source-db data/anomaly_detection.db \
  --output-db data/replay-identity-change.db \
  --report data/replay-identity-change.json \
  -- python -m anomaly_detection_engine.app
```

The output database must not already exist. This prevents an old replay from
being mistaken for the result of the current change.

Review at least:

- team, competition, event, and snapshot deltas;
- trust-state changes;
- resolution-method changes;
- new `PROVISIONAL`/`CONFLICT` identities;
- orphan-count changes;
- command exit status and final integrity result.

The child command decides what data is replayed. Point manual collectors at
retained capture directories through the normal environment configuration. Do
not point the replay command at live capture directories that another process
may consume or archive.

## Manual identity verification

List quarantined mappings:

```bash
python scripts/identity_mapping.py suspects --sport football
```

Verify or retarget a team or competition mapping with the existing `team` and
`competition` commands. Verify a provider fixture ID with its full compatibility
snapshot:

```bash
python scripts/identity_mapping.py event \
  --provider api-football \
  --sport football \
  --source-event-id 123456 \
  --event-id event-abc123 \
  --home-name "Arsenal" \
  --away-name "Chelsea" \
  --competition-name "England - Premier League" \
  --country England \
  --home-source-team-id 42 \
  --away-source-team-id 49 \
  --source-competition-id 39
```

The raw names are required because a provider ID alone cannot detect later ID
recycling or identity drift. Manual event verification can deliberately replace
a `SUSPECT` mapping, so it should only be run after reviewing the source payload.

Mark a mapping `REJECTED` with an audit reason -- a terminal "a human looked at
this and it was wrong", distinct from `SUSPECT`'s "unresolved, needs a look".
Every write path that could otherwise relearn the same wrong mapping already
refuses to touch a `REJECTED` row the same way it already refuses to touch
`SUSPECT`; the only way past it is `team`/`competition`/`event` above, the same
escape hatch that already clears `SUSPECT`:

```bash
python scripts/identity_mapping.py reject \
  --provider mozzart --sport football --kind team \
  --source-team-id 12345 \
  --reason "recycled provider id, now a different club"

python scripts/identity_mapping.py reject \
  --provider mozzart --kind event \
  --source-event-id mz-98765 --reason "wrong fixture entirely"
```

`--kind` selects the mapping shape (`team`/`competition`/`event`); `--sport` is
required for `team`/`competition`, `--source-event-id` for `event`. At least one
of `--source-name`/`--source-team-id` (team) or `--source-name`/
`--source-competition-id` (competition) must identify an *existing* mapping --
rejecting a key that was never resolved raises rather than silently doing
nothing. `scripts/audit_identity.py` reports rejected mappings under their own
`rejected-<table>` finding (always `WARNING`, since it is a closed decision, not
an open one) so a rejection stays visible rather than quietly disappearing.

## After an identity change

1. Re-run the complete test, lint, and type-check suite.
2. Replay against the same baseline copy.
3. Run the identity audit on the replay result.
4. Compare and retain the replay/audit reports with the change record.
5. Back up the live database immediately before any approved data repair.
6. Run the repair as a reviewed script/transaction, never as ad-hoc production
   SQL without a saved copy and explicit before/after counts.

## Poller reliability

The background poller (`scripts/start_background_poller.ps1`) is a
long-running pythonw.exe process; Windows has been observed freezing it
outright (AppHangXProcB1, or idle-throttling "Efficiency Mode") for 24+
hours with the process still showing as alive and nothing to detect it.

Start it (writes `poller.pid`, checked by later `-SkipIfRunning` calls
against the process's own command line, not just its name):

```powershell
.\scripts\start_background_poller.ps1
```

Check whether it is both running and still making progress, and
auto-restart it if not (crashed: restarted immediately; alive but silent
for longer than `-StalenessSeconds`, default 4h: killed then restarted):

```powershell
.\scripts\poller_watchdog.ps1
```

Register it to run automatically every 15 minutes (no elevated rights
needed for this "when logged on" trigger; the mode this project's own
tooling previously hit Access Denied on was a different, "run whether
logged on or not" trigger):

```powershell
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "<repo>\scripts\poller_watchdog.ps1"'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName 'AnomalyDetectionEnginePollerWatchdog' -Action $action -Trigger $trigger
```

`New-TimeSpan -Days 3650`, not `[TimeSpan]::MaxValue` -- the latter
serializes to a duration outside what the Task Scheduler XML schema
accepts and the registration fails.

Pair this with `scripts/install_startup_shortcut.ps1` (every-login
recovery) -- the watchdog task covers a hang or crash mid-session, the
startup shortcut covers whatever happens between the watchdog's own
15-minute checks and a reboot/logout.

## Retention

Preview retention before deleting history:

```bash
python scripts/run_retention_cleanup.py --dry-run
```

Run it only after confirming that the retained raw payload window still covers
the replay and forensic needs of recent matching changes.
