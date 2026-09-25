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

There is intentionally no `reject` command yet. A rejected mapping needs a
defined runtime policy preventing ordinary ingestion from immediately learning
it again; adding only a new label would not complete that lifecycle.

## After an identity change

1. Re-run the complete test, lint, and type-check suite.
2. Replay against the same baseline copy.
3. Run the identity audit on the replay result.
4. Compare and retain the replay/audit reports with the change record.
5. Back up the live database immediately before any approved data repair.
6. Run the repair as a reviewed script/transaction, never as ad-hoc production
   SQL without a saved copy and explicit before/after counts.

## Retention

Preview retention before deleting history:

```bash
python scripts/run_retention_cleanup.py --dry-run
```

Run it only after confirming that the retained raw payload window still covers
the replay and forensic needs of recent matching changes.
