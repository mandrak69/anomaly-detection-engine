# Team-identity mapping cleanup -- decision log

## Background

`source_team_mappings` caches every (provider, raw team name) -> canonical
team resolution permanently, so a raw name is never re-resolved once seen
once (see `FixtureCatalog`'s own docstring). An older fuzzy scorer
(`WRatio`, since replaced by `token_sort_ratio` -- see
`team_normalizer.py`'s own comment on the fix) produced false-positive
matches for short, generic shared tokens (e.g. "Town", "United", a city
name), permanently merging genuinely different real clubs into one
canonical team. Those bad cache rows all predate the `resolution_method`
audit column (migration 12) and show up as `resolution_method IS NULL`.

Fixing this has two layers, and they were not always both done together:

1. **The cache row** (`source_team_mappings`) -- cheap, prevents the same
   raw name from being mis-resolved again on a *future* sighting. Doesn't
   touch anything already in `events`.
2. **Existing `events` rows** created *before* the cache fix -- these still
   point at the wrong team and need per-event forensic correction: for
   each event under a corrupted bucket, look up the real raw
   `home_team`/`away_team` strings from `raw_payloads` (joined via
   `odds_snapshots.collector_run_id`, matched on the event's own
   `start_time`, disambiguated by the *other* side's already-correct
   canonical name), and repoint `events.home_team_id`/`away_team_id` to a
   newly created or already-existing correctly-named team.

Every round below did both. Each round was backed up
(`data/anomaly_detection.db.backup-<timestamp>`) before writing, checked
with `PRAGMA foreign_key_check` and a whole-database duplicate-event scan
(`GROUP BY competition_id, home_team_id, away_team_id, start_time HAVING
COUNT(*) > 1`) after writing, and a couple of rounds turned up an
accidental exact-duplicate event from the repointing itself (two DB rows
for the same real fixture) -- those were merged (`odds_snapshots` moved to
the surviving event id, the duplicate `events` row deleted) rather than
left in place.

## Fixed (all rounds)

Every one of these was resolved with **direct forensic evidence** --
either the raw name matched the team's own canonical name exactly under
today's `token_sort_ratio` scorer (zero ambiguity), or the *specific
event* was traced back to its real raw `home_team`/`away_team` pair via
`raw_payloads`. None of these were guessed.

Corrected buckets (raw name(s) that were merged in -> real identity):

- **Stoke City**, **New England Revolution**, **New York Red Bulls**,
  **San Jose Earthquakes**, **Kalmar FF**, **Kristiansund BK**, **Leon**,
  **Atletico San Luis**, **Coventry City**, **Angelholms FF** (10 teams,
  the original "exact-match" audit)
- **San Martin S.J.**, **Detroit City**, **Newcastle United**, **New York
  City II**, **Sarpsborg 08 FF**, **Atvidabergs FF** (discovered as
  forensic side-effects of the round above)
- **Atlanta United II**, **Columbus Crew II**, **Connecticut FC**,
  **Drogheda United**, **Loudoun United** (Toronto II / DC United / FC
  Seoul buckets)
- **Athlone Town**, **Longford Town**, **Ipswich Town** (the original
  "The Town" bucket -- the very first one found, fixed at the cache level
  early on but its *events* were only forensically corrected in this
  later pass)
- **Arsenal**, **Leeds United** (Arsenal Tula / Minnesota United FC
  buckets)
- **San Antonio**, **San Lorenzo**, **Talleres Cordoba**, **Belgrano
  Cordoba**, **Rosario Central**, **IF Karlstad**, **San Luis**,
  **Orense SC**, **Houston Dynamo FC II**, **FCI Levadia II**, **Racing
  Louisville W**, **Valerenga W**, **Botafogo**, **Palmeiras**,
  **Charlotte Independence**, **Chattanooga Red Wolves**, **Union
  Omaha**, **Levadia U19**, **FK Zalgiris Vilnius**, **Nottingham Forest
  U21** (final sweep -- `resolution_method IS NULL` legacy mappings
  re-audited against the current scorer)

`Nottingham Forest M21` (mozzart, Serbian for "men's U21") and
`Nottingham Forest U21` (api-football) were both pointed at **one** new
team rather than two, since they unambiguously name the same real
reserve side -- the only case in this whole cleanup where two *different*
raw strings were deliberately folded into a single new canonical team.

## Investigated, left unresolved -- decisions

These were looked at with the same method and deliberately **not**
auto-fixed. Each is a real, open data-quality item; they're recorded here
so a future pass doesn't have to re-discover them from scratch.

### 1. "Husqvarna" (api-football) -> currently `Husqvarna W`

One event (`Husqvarna W vs Skovde AIK`, 2026-09-18T17:00) has no
resolvable raw evidence: no captured payload at that exact timestamp has
an opponent matching "Skovde AIK" by any comparison key. The closest
candidate seen was `Husqvarna | IFK Skovde` -- but "IFK Skovde" and
"Skovde AIK" read as two different real clubs from the same city, not a
spelling variant of one, so this isn't safe to treat as a match.

**Decision:** leave the `Husqvarna` (men's) -> `Husqvarna W` cache
mapping as-is for now. Needs either a fresh capture of the actual
Husqvarna/Skovde AIK fixture to forensically confirm, or a manual look at
which "Husqvarna" team actually played Skovde AIK.

### 2. "Nottingham Forest vs Aston Villa" -- two duplicate events, both likely mislabeled

`event-a32f635d9e` and `event-cf00ced8bc` are the same nominal fixture
(2026-09-23T18:00, one captured with English league labels, one with
Serbian labels -- "Engleska Liga Kup W" etc.) -- itself another instance
of the known league-name-fragmentation problem (see
`docs/manual-capture-sources.md`'s Meridianbet/Mozzart Serbian-naming
notes), not something this cleanup pass touches.

Worse: the raw evidence at that timestamp only contains `Nottingham
Forest W vs Aston Villa W` (WSL Cup, women's) -- there's no evidence
either event is really the men's fixture. That means **both** home and
away sides are likely wrong here, not just the Nottingham Forest side
this investigation was scoped to, and fixing it correctly means also
auditing the `Aston Villa` bucket (not yet investigated at all).

**Decision:** left both events as-is. Out of scope for a Nottingham
Forest-only pass -- needs its own round that (a) audits `Aston Villa` the
same way, (b) decides whether/how to merge the resulting duplicate W-vs-W
event once both sides are corrected.

### 3. Copiapo / Deportes Copiapo -- noticed in passing, not investigated

While resolving the `Union San Felipe` bucket, two events turned up for
the exact same kickoff (2026-09-26T15:30) against "Deportes Copiapo" and
"Copiapo" respectively -- almost certainly the same real match, split
because those two spellings never fuzzy-matched to one canonical team.
This is the Copiapo team's own fragmentation, not `Union San Felipe`'s.

**Decision:** not investigated further. Flagging only.

### 4. Other spelling-pair duplicates noticed but not chased

Same shape as #3, spotted incidentally while investigating unrelated
buckets, not investigated:

- `Charlotte vs Chicago` / `Charlotte FC vs Chicago Fire` (same kickoff,
  2026-09-26T23:30) -- `Chicago`/`Chicago Fire` fragmentation.
- `Nottingham Forest vs Arsenal` / `Nottingham Forest vs Arsenal FC`
  (same kickoff, 2026-10-18T15:30) -- `Arsenal`/`Arsenal FC` fragmentation
  (note: unrelated to the `Arsenal`/`Arsenal Tula` fix above, which was a
  different real bug already corrected).

### 5. "UAE M23" (mozzart) -- confirmed not a bug

Legacy mapping's cached target and what today's scorer would produce are
the same team (`UAE M23`), just recorded under a different
`resolution_method` (was "alias", would be "fuzzy" today). No action
needed.

## What's left unaudited

This cleanup worked from `source_team_mappings` rows where
`resolution_method IS NULL` (i.e. predate the audit column). Any
corrupted mapping created *after* that column existed, or a corruption in
`source_competition_mappings` beyond the two exact-match league fixes
done early on (`Primera B`/`Primera Nacional`), was not in scope here and
hasn't been swept with this method.
