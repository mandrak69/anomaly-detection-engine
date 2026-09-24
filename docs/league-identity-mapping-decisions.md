# League-identity mapping cleanup -- decision log

Sibling to [team-identity-mapping-decisions.md](team-identity-mapping-decisions.md)
-- same underlying class of problem, but for `competitions`/
`source_competition_mappings` instead of `teams`/`source_team_mappings`,
and a genuinely different root cause from the "The Town" family of bugs.

## Root cause: `token_sort_ratio` treats an embedded number as noise

Unlike the team-name bugs (an old `WRatio` scorer's token-blending false
positive), this one is a live gap in the *current* scorer:
`token_sort_ratio` scores two otherwise-identical league names that
differ only in one embedded number as near-100 -- a single-character
edit out of a long string -- even though that number is almost always
the entire reason the two are different real competitions (a tier, a
regional sub-group, an age group), not a spelling variant. Verified live:
`"Engleska 1"` (EPL) absorbed `"Engleska 3/4/5"` (English League One/Two/
National League); `"3. Division - Girone 2"` absorbed `Girone 3/4/5/6`
(different regional sub-groups of the same tier); several countries'
2nd/3rd divisions merged into their 1st; `"U19 Liga"` absorbed `"U21
Liga"` (a different age group).

**Code fix**: `TeamNormalizer.normalize()` (shared by both team and
league resolution) now requires a raw name's digit runs to match a
candidate's exactly before that candidate is even eligible for fuzzy
comparison -- see `_digit_tokens()` in `team_normalizer.py` and its own
docstring. Scoped to raw names that actually contain a digit, so a
digit-less name (e.g. a bare reserve-team marker) is unaffected and keeps
its existing ambiguous/fuzzy handling. Covered by
`tests/test_team_normalizer.py`.

This was an **active, ongoing** bug, not just legacy pre-audit-trail
data -- several of the corrupted mappings found below had a non-NULL
`resolution_method` of `"fuzzy"`, meaning they were produced by ordinary
runs using the buggy scorer right up until this fix landed, not just
inherited from before the audit-trail column existed.

## Data fix

Same two-layer pattern as the team cleanup: fixed the
`source_competition_mappings` cache row for every confirmed-bad raw
league name (21 total, across api-football/meridianbet/mozzart), and
separately, for each event currently sitting under the wrong competition
bucket, confirmed via `raw_payloads` (matched on the event's own
`start_time` plus its *already-correct* team names, which pins the
search precisely enough that no ambiguity remained) which raw league
string that specific sighting actually used, then repointed
`events.competition_id`.

21 new competitions created; 81 events repointed. No merges into an
*existing* competition were needed here (every corrupted raw name's real
identity was previously uncatalogued), so unlike the team cleanup there
was zero duplicate-event risk from this round -- a brand-new
`competition_id` can't already be shared by another event.

Fixed raw names -> real identity:

- api-football: `Erovnuli Liga 2`, `Second League - Group 1`, `Second
  League - Group 3`, `3. Division - Girone 3/4/5/6`
- meridianbet: `La Liga 2`, `2. Bundesliga`, `Divizije 1 Play-Off`, `U21
  Liga`
- mozzart: `Engleska 3/4/5`, `Francuska 3`, `Izrael 3`, `Meksiko 2 -
  Apertura`, `Spanija 2/3`, `Severna Irska 2`, `Srbija 2 MozzartBet`

Three of these (`Spanija 2`, `Spanija 3`, `Srbija 2 MozzartBet`) had zero
events in the database matching them by direct forensic evidence at fix
time -- their cache row was still corrected (so a *future* sighting
resolves correctly), but no `events` row needed repointing.

## Round 2: single-letter suffix collisions (Serie A/B, Liga nacija A-D, ...)

Same `token_sort_ratio` weakness, but triggered by a single **letter**
instead of a digit (e.g. `"Serie A"` vs `"Serie B"` scores 85.7 --
verified live). `_digit_tokens()`'s regex only catches digit runs, so
this needed a separate forensic pass; it was **not** fixed at the code
level.

**Why not the same code fix as digits**: a single letter is not always a
meaningful identifier the way a tier/group number always is. `"France
M21"` (mozzart, Serbian "muska"/men's) and `"France U21"` (English
"Under") are the *same real entity* under two languages' abbreviation
conventions -- 22 such team-level cases were found (`M21`/`U21` national
youth sides) and are **already correctly merged**, relying on this exact
fuzzy mechanism. A blanket "digit-style" letter-equality requirement
would have un-merged all 22 of them -- a regression, not a fix. Letters
need case-by-case judgment; digits don't.

Confirmed via forensic evidence (12 raw names, 102 events, same
start_time + already-correct-team-names matching method as Round 1):

- api-football: `Serie A` (Italy's *top* division) was merged into `Serie
  B` (second division) -- the single api-football-sourced fix in this
  round; everything else here is meridianbet/mozzart.
- meridianbet: `Serija B`, `Serija C`, `Serija C Grupa B`, `Serija C
  Grupa C`, `Primera Serija B`
- mozzart: `Liga nacija (B/C/D) - Evropa` (UEFA Nations League leagues B,
  C, D all merged into league A), `Italija 3 B`, `Italija 3 C`, `Azijske
  Igre W` (the Asian Games *women's* tournament, merged into the men's
  U23 one -- found while investigating this round, see below)

**One pre-existing merge confirmed correct, not touched**: `Azijske Igre
U23` (meridianbet) and `Azijske igre M23` (mozzart, the older/legacy row
that happens to hold the canonical spelling) are the same real
tournament under the M/U convention difference described above --
already fuzzy-merged correctly (score 93.75), left as-is. Only the
*women's* variant sharing that same bucket was the actual bug.

## Round 3: api-football's league name had no country in it at all

A third, distinct root cause, found while trying to build the actual
`LEAGUE_ALIASES` synonym table this whole cleanup was originally for:
`api_football_collector.py` only ever extracted `league.name`, discarding
the `.country` field api-football's real response always includes
alongside it. Many countries name their top flight the exact same
generic thing, so this wasn't a fuzzy-matching problem at all -- it was
several countries' fixtures sharing one bucket *by construction*, no
matter how good the scorer is. Verified live: api-football's own bare
"Premier League" bucket held only Kazakhstan and Ghana fixtures (zero
England ones), and several other generic names turned out to mix three
or more countries under one bucket:

| Old bucket | Real raw league name(s) | Countries mixed in |
|---|---|---|
| "Premier League" | `Premier League` | Kazakhstan, Ghana |
| "Serie A" / "Serie B" | `Serie A` / `Serie B` | Brazil only (not Italy) |
| "Cup" | `Cup`, `US Open Cup` | Uzbekistan, Russia, USA |
| "Championship" | `Championship`, `USL Championship` | England, Scotland, USA |
| "Nacionalna Liga" | `Liga Nacional` (+ a "... Jug" sub-division) | England (2 divisions), France, Guatemala |
| "Major League Soccer" | `Major League Soccer`, `Super League` | USA/Canada (genuine), Uzbekistan, Switzerland |

**Code fix**: `api_football_collector._qualified_league_name()` now
prefixes `f"{country} - {name}"` whenever api-football reports a country
(falls back to the bare name for the rare case it doesn't -- some
international competitions). Covered by
`test_same_league_name_in_two_countries_does_not_collide` and
`test_missing_country_falls_back_to_the_bare_league_name` in
`tests/test_api_football_collector.py`. This only affects *future*
polls; it doesn't retroactively touch anything already ingested.

**Data fix**: for the six mixed buckets above, every event was
classified by its own two teams' real country (Serie A's Brazilian clubs
needed no forensics -- team names alone settled it; the others were
confirmed via the same `raw_payloads` start_time+team match as every
other round, this time reading the actual `league` string api-football
sent per event rather than assuming one). 105 events repointed into 12
new, correctly country-qualified competitions
(`England - Championship`, `USA - Championship` under its own already-
unambiguous `USL Championship` name, `Scotland - Championship`,
`England - Liga Nacional`, `England - Liga Nacional Jug` (the National
League's South division -- a genuinely different real division, kept
separate rather than folded into the main one), `France - Liga
Nacional`, `Guatemala - Liga Nacional`, `Russia - Cup`, `Uzbekistan -
Cup`, `US Open Cup` (already unambiguous, just unmerged from bare
`Cup`), `Uzbekistan - Super League`, `Switzerland - Super League`). The
genuine 44-event `Major League Soccer` bucket was left as-is, not
renamed -- only the 10 contaminating events were moved out.

**LEAGUE_ALIASES correction**: the `Premier Liga`/`Engleska 1` -> `EPL`
and `Serija A`/`Italija 1` -> `Serie A` entries added in Round 2's
aftermath were themselves wrong for the same reason discovered here
(api-football's bare bucket wasn't the right country) -- caught and fixed
in the same sitting, before anything downstream depended on it. They now
alias to the-odds-api's own clean `EPL` and Meridianbet's own `Serija A`
(34 events, picked over Mozzart's 10 by size) instead.

**Retroactive event merge**: only Serie A's 10 Mozzart events were
merged into their matching Meridianbet event (odds_snapshots migrated,
`source_event_mappings` repointed too -- a second FK this needed beyond
what earlier rounds' merges touched -- duplicate event row deleted). EPL
and MLS were **not** merged yet: while checking match candidates for EPL,
Meridianbet's own "Premier Liga" bucket turned out to have the *exact
same* country-blindness problem, just on Meridianbet's side instead of
api-football's -- it also holds Scotland, Wales, Russia, Canada, and
Dominican Republic fixtures alongside genuine EPL ones. Merging into it
now would have propagated that contamination into the EPL anchor.
Deliberately stopped here rather than compounding it -- see below.

## Round 4: Meridianbet had the identical bug, plus the EPL/MLS merges completed

Confirmed straight after Round 3: `meridianbet_file_collector.py` only
ever extracted `header.league.name`, discarding `header.region.name` --
present on every real captured event alongside it (`{"regionId": 14,
"regionName": "Argentina", ...}` at the league-group level, and
`header.region: {"name": "Argentina", ...}` on the event itself). Same
consequence as api-football: `Premier Liga`'s 62 events mixed England
(20), Scotland (12), Wales (8), Russia (16), Canada (5), and the
Dominican Republic (1) under one bucket, all because those countries
also call their own top flight some "Premier League" equivalent.

**Code fix**: `meridianbet_file_collector._qualified_league_name()`
prefixes `f"{region} - {name}"` the same way the api-football fix does.
Covered by `test_same_league_name_in_two_regions_does_not_collide` and
`test_missing_region_falls_back_to_the_bare_league_name`.

**Data fix**: `MLS Liga` (meridianbet's own MLS bucket) was checked the
same way and found already clean -- all 33 events genuinely MLS/USL
Championship-adjacent, no contamination. `Premier Liga`'s 42 non-England
events were split into five new competitions (`Scotland - Premier Liga`,
`Wales - Premier Liga`, `Russia - Premier Liga`, `Canada - Premier
Liga`, `Dominican Republic - Premier Liga`); its 20 England events were
matched against `EPL` by start_time + team identity (all 20 had an
unambiguous match) and merged in directly, closing out the retroactive
merge Round 2 had deliberately deferred.

**EPL and MLS retroactive merges, completed**: EPL's 20 Meridianbet
events merged into `EPL` (the-odds-api's own bucket, unchanged 30-event
count since merging adds odds to existing events rather than creating
new ones). MLS Liga's 33 events split into 1 genuine duplicate (merged)
and 32 distinct fixtures the anchor didn't have yet (plain competition_id
repoint, no event deleted) -- `Major League Soccer` grew from 44 to 76
events. Both merges used the same `odds_snapshots` dedup + `events`
delete method as Serie A's Round 3 merge, plus repointing
`source_event_mappings` (the fast-path source_event_id cache -- a second
foreign key this kind of merge always needs beyond what a first glance
suggests, easy to forget).

## Left unresolved

**78 events** across the same buckets showed `UNKNOWN` in the forensic
sweep -- no `raw_payloads` row was found whose `start_time` and team
names (both sides, by comparison key) matched the event exactly. This
mostly means the event predates `collector_run_id` tracking, or its
payload has since been pruned/archived past what a single forensic pass
covers, not that they're confirmed-genuine or confirmed-bad. Left
untouched -- no fix without evidence, same standing rule as the team
cleanup.

## Still not addressed at all

- **The original 5 non-digit competition suspects**, already documented
  as deferred in `team-identity-mapping-decisions.md`'s scope note
  (`Regionalliga - SudWest`, `Super League`, `USL League One`, `USL
  Championship`, `US Open Cup`) -- unrelated to the digit bug, still
  open.
- **Cross-language league synonyms** (`EPL` = `Engleska 1` = `Engleska
  Premier Liga`, etc.) -- this fix stopped *wrong* merges; it does not
  create the *correct* ones. A `LEAGUE_ALIASES` table mapping each
  Meridianbet/Mozzart Serbian league name to its api-football English
  equivalent is still a separate, not-yet-started piece of work (see
  `docs/manual-capture-sources.md` and the "Other spelling-pair
  duplicates" section of `team-identity-mapping-decisions.md` for
  concrete examples of events split this way, e.g. Arsenal vs Leeds
  United/Leeds under `EPL` vs `Engleska 1`). `docs/league-names-by-source.md`
  has the full raw-name inventory per source to build it from.
