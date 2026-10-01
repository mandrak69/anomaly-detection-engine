# Identity garbage collection

Canonical identity rows are not disposable merely because no current event references them.
The identity subsystem distinguishes two concepts:

- **orphan**: a provisional team or competition is referenced by zero events;
- **GC-safe**: the orphan carries no durable identity knowledge and may be forgotten.

The cleanup rule intentionally fails closed. A candidate is removable only when it is
`PROVISIONAL`, has no event/reference-provider identity, and has no mapping in a durable or
quarantined trust state. `VERIFIED`, `SUSPECT`, and `REJECTED` mappings always protect their
canonical entity. An unknown future trust state protects it too.

`UNVERIFIED` mappings are disposable with a GC-safe entity. For competitions this includes
`source_team_mappings.competition_id`: migration 20 uses that column as the contextual scope
for team identity, so deleting a competition without considering those rows would leave stale
identity context even though the column is not a SQL foreign key.

`team_competitions` membership rows do not by themselves protect an orphan. They are derived
context and are removed together with a GC-safe parent.

## Operational use

Dry-run is the default:

```bash
python scripts/cleanup_identity.py
python scripts/cleanup_identity.py --entity team
python scripts/cleanup_identity.py --entity competition --id competition-123
python scripts/cleanup_identity.py --json
```

Apply only after reviewing the report:

```bash
python scripts/cleanup_identity.py --apply
```

The apply path takes SQLite's writer lock with `BEGIN IMMEDIATE`, recomputes the candidate set
inside that transaction, deletes only `UNVERIFIED` mapping rows plus `team_competitions`, and
then deletes the canonical entity. Any failure rolls the entire cleanup back.

A `REJECTED` mapping is deliberately **not garbage**. It is negative knowledge: a human has
already established that a provider identity was wrong. Deleting it would allow ordinary
ingestion to forget that decision and potentially relearn the same bad mapping.
