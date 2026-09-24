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

## Round: systematic Mozzart/Meridianbet suffix diff + Ireland/Iceland (2026-09-24)

Different method than every round above: instead of auditing
`resolution_method IS NULL` legacy rows, this round grouped `events` by
`(competition_id, start_time)` and looked for pairs sharing exactly one
side's `team_id` but not the other -- i.e. proven-live duplicates (same
competition, same kickoff, one side already agreeing) rather than a
name-similarity guess. Found 24 such pairs; two were excluded (see
below), the rest fixed via a `pipeline.ALIASES` entry (so a *future*
sighting resolves correctly without ever reaching fuzzy matching) plus a
one-off merge of the already-existing duplicate event.

**Aliased and merged** (Meridianbet/Mozzart spelling -> target already
used elsewhere): Sabah Masazir/Sabah, Galatasaray Istanbul/Galatasaray,
Inter Milano/Inter, Viking FK/Viking, Fenerbahce Istanbul/Fenerbahce,
PSG/Paris Saint-Germain, VfB Stuttgart/Stuttgart, CSD Xelaju MC/Xelajú,
MS Tira/Tira, Ironi Baka El Garbiya/Baqa Al-Gharbiyye, Independiente
Santa Fe/Santa Fe, Aguilas Doradas Rionegro/Águilas Doradas,
Turks&Caicos Islands/Turks and Caicos Islands, Saint Martin/Saint-Martin,
Antigva & Barbuda/Antigua and Barbuda, Atletico Fenix/CA Fenix
Montevideo, Colon FC/Colon Montevideo, CS Cerrito/Cerrito, MS
Ashdod/Ashdod, MS Football Hapoel Kiryat Yam/Kiryat Yam,
Guadalupe/Guadeloupe, Jaguares/Jaguares de Cordoba, Arsenal FC/Arsenal,
Lille OSC/Lille (the last two from the round immediately before this
one, same method).

**Merged without a global alias:** `CA Cerro` -> `Club Atletico Cerro`.
The live duplicate pair was genuine (same competition, same kickoff,
shared opponent Deportivo Maldonado) and was merged, but no
`pipeline.ALIASES` entry was added -- `Club Atletico Cerro`'s own team
row was separately found to already have an unrelated Paraguayan fixture
(`vs Sevilla Atletico`, under Paraguay's own "Paragvaj 1 - Klausura")
wrongly attached to it, so routing more sightings there isn't safe until
that's untangled on its own.

### Ireland / Iceland country collision

A bigger, different-shaped bug found while reviewing the batch above:
bare `"Iceland"` scores 85.71 against `"Ireland"` under
`token_sort_ratio` -- above `fuzzy_threshold` -- and neither name has a
digit for the digit-run guard (`_digit_tokens`) to gate on, since that
guard only ever fires on an *embedded number* difference, not a
same-length-different-letter one. Confirmed live: **every** sighting of
Iceland's senior team, from both Meridianbet and Mozzart, had been
silently merging into `Ireland`'s canonical row since no genuine
`Iceland` row existed yet to exact-match against first. The identical
shape recurred one level down: `"Ireland U21"` (Meridianbet) scores 90.9
against `"Iceland U21"`, merging Ireland's U21 team into Iceland's.

A related but mechanically different bug on the same cluster: `"Republic
Ireland M21"` and `"Northern Ireland M21"` (Mozzart) *do* carry a digit,
so the digit guard correctly refused to fuzzy-match them against the
bare senior team -- but with no `"...U21"` row exists yet either, that
just left them stuck at `resolution_method="unknown"` (a brand-new team
every sighting) instead of merging cleanly, unlike `"Iceland M21"` which
already merges fine into `"Iceland U21"` once that row exists (both
carry a matching `"21"` digit token, so a plain fuzzy match bridges the
M21/U21 spelling on its own, no alias needed).

**Fixed:**
- Created three genuinely-missing canonical teams: `Iceland`, `Ireland
  U21`, `Northern Ireland U21`. Their own existence is what closes the
  country-collision half of the bug going forward -- once they exist, a
  future sighting takes the exact-match path and never reaches fuzzy
  matching at all; no `pipeline.ALIASES` entry was needed or added for
  either.
- Retargeted the 6 already-wrongly-merged events onto the correct new
  team (2x Iceland vs Estonia; Slovakia U21 vs Ireland U21 from each
  provider's own spelling; Malta U21 vs Northern Ireland U21 from each
  provider's own spelling).
- Retargeted 3 more events carrying Ireland's *senior* team under its
  other two spellings (`Republic Of Ireland` from Mozzart, `Rep. Of
  Ireland` from api-football) onto the single existing `Ireland` row,
  merging the one pair that shared a competition_id (Kosovo vs Ireland,
  both under Meridianbet's `Liga Nacija`) and leaving the other two as
  separate events (different `competition_id` per provider -- see the
  Nations League item below). Added `pipeline.ALIASES` entries for
  `Republic Of Ireland` and `Rep. Of Ireland` (this half of the bug
  needed an explicit alias, unlike the country-collision half, since
  neither spelling is close enough to `Ireland` for even a safe fuzzy
  match to bridge) plus `Republic Ireland M21`/`Northern Ireland M21` ->
  their now-existing `...U21` rows.
- Dropped the now-fully-orphaned `Republic Of Ireland` and `Rep. Of
  Ireland` team rows and every stale `source_team_mappings` row that
  would otherwise have kept short-circuiting a future sighting back onto
  the wrong team (the same write-once-cache mechanism documented in
  `docs/league-identity-mapping-decisions.md`).

**Not investigated further, same cluster:** whether `Slovakia U21`
(Meridianbet) and `Slovakia M21` (Mozzart) are themselves split the same
way `Ireland`/`Northern Ireland`'s U21 sides were -- noticed in passing
(they resolved to two different team rows for the same match) but not
chased, since neither event needed merging under this round's
same-`competition_id`-only merge policy.

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

### 6. "Toronto II vs Toronto II" -- a team playing itself

Turned up as a byproduct of the 2026-09-24 round's `(competition_id,
start_time)` grouping: `event-763a5b7855` has `home_team_id ==
away_team_id` (both `Toronto II`), alongside a second, plausible-looking
`event-12a628dccc` (`Toronto II vs New York RB II`) at the same kickoff.
Not a name-fragmentation case like everything else in this document --
something produced a genuinely broken row. Not investigated further;
flagging only.

### 7. "Inter Milano (W)" and "Paris Saint-Germain (W)" -- women's fixtures merged onto the men's team

Found while checking every event under the `Inter Milano`/`Paris
Saint-Germain` buckets before the 2026-09-24 alias round repointed their
*other* events: Meridianbet's `source_team_mappings` has `"Inter Milano
(W)"` and `"Paris Saint-Germain (W)"` both mapped straight onto the same
team row as the bare men's name (`Inter Milano`, `Paris Saint-Germain`),
so a women's fixture for each club (e.g. `Inter Milano (W) vs Hacken
Gothenburg (W)`) currently displays under the men's team. Independiente
Santa Fe has the identical shape (`Independiente Santa Fe (W)` -> the
men's team). None of these were touched by the alias round -- it only
repointed each club's other *men's* fixtures, deliberately leaving the
`(W)` mapping and its event alone rather than guessing which existing
women's team (if any) it should actually point to.

### 8. "Liga 1" / "Francuska 1" -- Ligue 1 (France) split the same way EPL/"Engleska 1" was

Noticed while repointing `Lille OSC` -> `Lille`: `Lille OSC vs Le Havre
AC` sits under Meridianbet's `Liga 1`, while `Lille vs Le Havre` (the
same real fixture, same kickoff) sits under a *different* competition
row, `Francuska 1`. Same shape as the EPL/"Engleska 1" split documented
in `docs/league-identity-mapping-decisions.md`, just not yet
cross-checked against real fixtures the way that one was before being
aliased -- left alone here rather than guessed.

### 9. UEFA Nations League -- competition names split per provider, seen across many country pairs

Turned up repeatedly while investigating the Ireland/Iceland cluster:
Meridianbet's own Nations League bucket (`Liga Nacija` /
`"Evropa - Liga Nacija"`) and Mozzart's own (`"Liga nacija (A/B/C) -
Evropa"`) never got cross-mapped, so a correctly-identified country pair
(e.g. `Georgia vs Northern Ireland`, `Northern Ireland vs Hungary`, both
already resolving to the *right* teams) still ends up as two separate
events, one per provider's competition spelling. Likely affects every
Nations League group, not just the pairs noticed in passing here. Not
fixed in this round -- unlike the Ireland/Iceland team-identity bug, this
is a competition-identity problem and needs the same real-fixture
verification `docs/league-identity-mapping-decisions.md`'s own rounds
used before aliasing anything, across (at minimum) every Nations League
group letter, not just the handful of pairs this investigation happened
to touch.

## What's left unaudited

This cleanup worked from `source_team_mappings` rows where
`resolution_method IS NULL` (i.e. predate the audit column). Any
corrupted mapping created *after* that column existed, or a corruption in
`source_competition_mappings` beyond the two exact-match league fixes
done early on (`Primera B`/`Primera Nacional`), was not in scope here and
hasn't been swept with this method.
