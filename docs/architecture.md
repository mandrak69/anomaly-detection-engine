# Architecture

## Purpose

This document describes the current architecture of the Anomaly Detection Engine and acts as the technical source of truth for the processing pipeline.

The first use case is sports odds analysis, but the architecture is intentionally source-independent and domain-oriented.

---

## Current Pipeline

```text
External Source
      ↓
Collector
      ↓
CollectorRun
      ↓
Raw Payload
      ↓
Source Adapter / Parser
      ↓
RawEventOdds
      ↓
Structural Validation
      ↓
DataValidationResult
      ↓
Normalization
      ↓
Event Matching
      ↓
MarketIdentity
      ↓
Semantic / Identity Validation
      ↓
OddsSnapshot
      ↓
Storage
      ↓
Freshness / Temporal Validation
      ↓
Analysis Engine
      ↓
Detection (unconditional candidates)
      ↓
      ├──→ Storage (SignalRepository / MovementRepository)
      └──→ Anomaly Classification → Reporting
```

---

## Architectural Boundaries

### Acquisition Layer

Responsible for obtaining data from external systems.

Possible mechanisms:

```text
API
internal HTTP/JSON endpoint
HTML scraping
browser automation
file input
```

This layer must not leak source-specific models into the core engine.

### Raw Data Layer

Represents the source-independent but not yet fully trusted observation.

Primary model:

```text
RawEventOdds
```

### Validation Layer

Responsible for deciding whether data is structurally and semantically acceptable.

Primary models:

```text
DataValidationResult
ValidationIssue
ValidationStage
```

### Normalization Layer

Maps external entity representations to canonical internal representations.

Examples:

```text
Man Utd → Manchester United
ENG PL → Premier League
```

### Matching Layer

Resolves raw observations to canonical events and markets.

Matching must consider:

```text
sport
competition
home team
away team
start time
market semantics
```

Two implementations of the same `EventResolver` Protocol
(`matching/event_matcher.py`: `match(...) -> EventMatchResult`):

```text
EventMatcher      fixed, in-memory candidate list -- rejects anything
                  it doesn't already know
FixtureCatalog     persistent (teams/events/competitions/
                   source_team_mappings/source_competition_mappings
                   tables) -- resolves a never-seen team, competition,
                   or event by creating a canonical row instead of
                   rejecting it, and remembers every (provider_id,
                   sport, raw name) resolution permanently
```

`OddsIngestionService` type-hints its `matcher` parameter as
`EventResolver`, not a concrete `EventMatcher` -- every real caller
(`pipeline.run_ingestion`) actually passes a `FixtureCatalog`, which
previously only duck-typed the same shape rather than one declaring it.

`FixtureCatalog` reuses `TeamNormalizer` (the same exact/alias/fuzzy
logic `EventMatcher` uses) internally for both teams and competitions,
run against the durable `teams`/`competitions` tables instead of a
candidate list scoped to one run. The permanent mapping cache is what
lets two different sources report the same real match under different
team-name spellings ("Man Utd" vs "Manchester United") or league names
("Premier League" vs "England Premier League") and still resolve to one
shared canonical event -- once a spelling has been resolved, it never
needs to be re-solved. `pipeline.py` uses `FixtureCatalog` for every
source; `EventMatcher` remains available (and tested) for callers that
genuinely want a fixed, non-growing candidate list.

`FixtureCatalog` is constructed with a `provider_id` (the real-world
data provider, e.g. `"the-odds-api"`), not the collector's own
`OddsCollector.source` (a per-collector-instance label that also
encodes acquisition method/sport key, e.g.
`"the-odds-api-manual:soccer_epl"`) -- an auto and a manual collector
for the same provider must share one mapping cache, which keying on
`source` would silently defeat.

Competition resolution (`_resolve_competition`) mirrors team resolution
exactly, one level up: without it, league being part of an event's
identity (below) would work against cross-source matching instead of
for it -- the same real match reported under different league spellings
by different sources would resolve to two canonical events that could
never be compared, silently defeating the entire point of tracking
league at all. `Event.league` stays a plain string (the canonical
display name), but event *identity* is keyed on `Event.competition_id`
(a stable FK into `competitions`), not on this string -- a future rename
of a canonical competition can't silently detach existing events from
later matches under the new spelling. `competitions`/
`source_competition_mappings` otherwise exist purely as this
resolution's persistent memory. A separate `league_aliases` constructor
param (distinct from `aliases`, team-name only) seeds known league-name
variants.

`FixtureCatalog.match()` wraps its whole read-then-maybe-create
resolution (team, competition, event) in one `BEGIN IMMEDIATE`
transaction -- this project explicitly supports multiple concurrent
`watch_capture.py`-spawned processes sharing one database file, and the
straightforward SELECT-then-INSERT pattern has a real race window
between them without it. A second connection attempting the same
resolution blocks on SQLite's writer lock instead of racing it.

`TeamNormalizer` also guards against a false-confidence fuzzy match: if
the top two candidates score within `ambiguity_margin` (default 5
points) of each other, the result is `"ambiguous"` rather than a
confident `"fuzzy"` pick, even when the top score alone would clear
`fuzzy_threshold` -- e.g. "Man United" scoring ~85.5 against both
"Manchester United" and "Manchester United U21" is a near-tie, not a
clear winner. `FixtureCatalog` treats `"ambiguous"` the same
conservative way as `"unknown"`: create a new canonical team rather than
guess. Event resolution additionally scopes by `league`, not just team
pair + start-time tolerance -- the same two teams can play each other in
more than one competition (league + cup, or two age groups) within the
tolerance window, and those must stay separate canonical events.
`start_time` itself is normalized to UTC (`storage.time_utils.
to_utc_iso`, the same helper `OddsRepository` uses) before being stored
or compared, so two sources reporting the same real kickoff under
different but equally valid offsets still resolve to one event.

### Domain Layer

Contains canonical models such as:

```text
Event
Team
Bookmaker
MarketIdentity
OddsSnapshot
CollectorRun
```

`Event` and `Team` are now persisted (via `FixtureCatalog`, not a
dedicated repository of their own -- see Matching Layer above and
Storage Strategy below), so they are no longer purely in-memory
runtime objects the way `Bookmaker` still is.

### Storage Layer

Persists historical observations and operational metadata.

Current implementation:

```text
SQLite (persistent file, see Storage Strategy -> DB_PATH)
storage.migrations    schema versioning (PRAGMA user_version), see
                      Storage Strategy -> Schema migrations
OddsRepository
CollectorRunRepository
RawPayloadRepository
SignalRepository      stateful SUREBET/VALUE_GAP signals
MovementRepository    append-only price-change transitions
```

Every connection is configured by `configure_connection()` (row access
by column name, `PRAGMA foreign_keys = ON`) -- SQLite does not enable
foreign-key enforcement by default even though the schema declares
`REFERENCES`, so this has to be an explicit per-connection step, not
just a schema-level declaration.

Potential future implementation:

```text
PostgreSQL
```

### Analysis Layer

Consumes already validated and semantically comparable observations.

Current modules:

```text
best odds
surebet (arbitrage)
freshness
rapid movement
outlier detection
bookmaker lag
```

Future modules:

```text
cross-market anomaly detection
```

### Detection Layer

`analysis.opportunity_detection` (`detect_surebet_candidates`,
`detect_value_gap_candidates`) and `analysis.movement_detection`
(`detect_movements`) sit between the primitive Analysis Layer modules
(`calculate_arbitrage`, `detect_outliers`, `detect_rapid_movement`) and
everything downstream. They answer "what is currently true" as
structured candidates (`SurebetCandidate` with all three legs grouped
together, not flattened; `ValueGapCandidate`; `MovementCandidate`) with
no "worth telling someone" threshold baked in (SUREBET has none at all;
VALUE_GAP keeps `detect_outliers`' own threshold, since that is part of
what counts as an outlier, not a presentation filter). Both the
Reporting Layer and the Storage Layer's `SignalRepository`/
`MovementRepository` consume the same candidates, so detection logic
exists in exactly one place regardless of what eventually happens to
the result.

### Reporting Layer

Consumes the Detection Layer's candidates and applies an "is this worth
a line in the report" threshold on top -- detection answers whether a
condition holds, reporting decides whether it clears the bar to be
worth a human's attention right now. This separation matters because the
two questions have different answers for the same data: a mathematically
real 0.05% surebet is still a real surebet, but noise for reporting
purposes.

Current reports:

```text
opportunity_report   SUREBET + VALUE_GAP, sorted by edge, largest first
movement_report       significant change between an outcome's last two
                       readings, independent of how far apart they were
```

Future:

```text
web dashboard / notifications reading from SignalRepository /
MovementRepository instead of recomputing candidates on demand
(currently only the text reports above consume the Detection Layer;
the persisted signals/movements tables have no presentation layer yet)
```

### Observability Layer

Cross-cutting, not a pipeline stage: every layer above can emit into it.

```text
structured logging   JSON lines via the standard `logging` module
                      (observability.logging_config), not print() --
                      ingestion run start/completion, per-record
                      rejection reasons, collector failures
in-process metrics    observability.metrics.IngestionMetrics, an
                      accumulator (not an exporter) a long-lived caller
                      can pass into OddsIngestionService to track totals
                      across repeated runs
```

No metrics backend (StatsD/Prometheus/CloudWatch) is wired up; the
structured log lines and the metrics accumulator's `snapshot()` are what
a real deployment would ship to one.

---

## Collector Contract

`collect()` returns a `CollectionResult`:

```text
source_payload   the exact source response (decoded to text), or None
                 if there was genuinely nothing to collect this cycle
                 (e.g. a manual-capture drop file isn't there yet) --
                 never a substitute for a parse failure, which still
                 raises and propagates unchanged
records          list[RawEventOdds], same contract as before
```

Every `OddsCollector` also exposes `source` (this collector instance's
label, for `CollectorRun`/logging), `provider_id` (the real-world data
provider, see Matching Layer), and `parser_version` (its own parsing
logic's version, distinct from `collector_version` -- see CollectorRun
below). `source_payload` plus these three are what a future reprocessing
script needs: which parser produced a given historical response, and
which real provider it came from.

The collector may internally use any acquisition method:

```text
JsonOddsCollector           local file, source-independent demo format
TheOddsApiCollector          documented public API, HTTP GET
ApiFootballCollector          documented public API, two HTTP GETs
                              joined locally (see below)
ManualCaptureCollector        generic: watches a fixed drop file, hands
                              its contents to an injected parse
                              function, archives it -- fetches nothing
                              itself
  MozzartFileCollector          parse_mozzart_response
  TheOddsApiManualCollector     parse_the_odds_api_response (the same
                                 function TheOddsApiCollector's HTTP
                                 path uses)
```

`ApiFootballCollector` (api-football.com/api-sports.io) is a second
real pre-match provider, added to prove cross-provider matching against
real data rather than only this project's own fixtures. Its `/odds` and
`/fixtures` endpoints are date-scoped (`?date=YYYY-MM-DD`), not
sport/competition-scoped the way `TheOddsApiCollector`'s `sport_key` is,
and `/odds` identifies each entry only by `fixture.id` -- team names
live on the separate `/fixtures` response for the same date. So
`collect()` makes two HTTP calls (both for the same date) and joins
them locally by `fixture.id` (`parse_api_football_response`), unlike
every other collector here, which parses one self-contained response. A
fixture present in the odds response but missing from the fixtures
response is skipped, not an error -- there is no home/away team name to
build a `RawEventOdds` from. Odds come from the `"Match Winner"` bet
(`"Home"`/`"Draw"`/`"Away"`, mapped onto `"1"`/`"X"`/`"2"`); auth is a
request header (`x-apisports-key`), not a URL query param.

`ManualCaptureCollector` separates *acquisition* (watch a drop file,
archive after reading) from *parsing* (source-specific, injected as a
plain function) so "capture manually instead of fetching" is a mode any
source can have, not a bespoke rewrite per source. Two reasons a source
ends up in manual mode, both legitimate and permanent (not a workaround
to be replaced later):

```text
no automatic mode exists      MozzartFileCollector: mozzartbet.com sits
                               behind Cloudflare bot-management
                               (cf_clearance/__cf_bm cookies observed on
                               their /live/matches request); scripting
                               around that would mean bypassing active
                               bot-detection, which this project does
                               not do regardless of technical
                               feasibility.
automatic mode is unavailable   TheOddsApiManualCollector: the API
right now                      normally works fine, but a rate limit,
                               an outage, or an exhausted quota can make
                               a manual fallback useful sometimes even
                               for a source that can be automatic.
```

Which mode a source runs in is an explicit flag, resolved in
`config.AppConfig` and read by `pipeline.py`'s collector construction
(`ODDS_API_MODE`, `MOZZART_MODE`), not inferred from which env vars
happen to be set -- an unsupported mode value fails loudly rather than
silently falling back to something unintended.

The rest of the system does not need to know how the data was obtained.

---

## CollectorRun

Every ingestion cycle should be represented by a `CollectorRun`.

Responsibilities:

```text
track source
track start/end time
track status
track accepted/rejected records
track collector version
track errors
track provider_id/parser_version/source_payload -- the exact response
    this run's records were parsed from, plus which real provider and
    which parsing-logic version produced it (see Collector Contract) --
    None when collect() itself raised (nothing was ever fetched/read)
```

Statuses:

```text
SUCCESS
PARTIAL
FAILED
```

This gives the system source-coverage and ingestion-quality context.

A `CollectorRun` is guaranteed to be recorded even if an individual
record's processing raises unexpectedly (a validator/matcher/
`FixtureCatalog`/`OddsRepository` bug or transient error, not just an
ordinary rejection) -- `OddsIngestionService._ingest_one` catches
per-record failures and reports them as a rejected record
(`processing-error: <ExceptionType>: <message>`) instead of letting them
abort the run. Losing the `CollectorRun` itself would hide exactly the
kind of failure this record exists to surface.

Status is not purely a function of accept/reject counts:

```text
some rejected, some accepted    PARTIAL
some rejected, none accepted    FAILED
raw-payload audit write failed for any record,
even if every record was otherwise accepted     PARTIAL
run() itself aborted mid-iteration (an exception
genuinely escaped the loop, not one record's own
processing)                                     PARTIAL if anything
                                                 accepted first, else
                                                 FAILED; error_type/
                                                 error_message set
none of the above                               SUCCESS
```

Raw payload retention is part of this system's auditability, not a
nicety a run can silently lose while still reporting `SUCCESS`.
Likewise, a run that aborted mid-loop is never `SUCCESS` even if every
record processed before the abort happened to succeed -- the loop not
finishing is itself the failure being reported, distinct from any
individual record's own outcome.

---

## MarketIdentity

Market comparison is based on semantic equivalence, not display names.

Identity:

```text
market_type
period
phase       (MarketPhase: PRE_MATCH or LIVE -- required, no default)
line
rules
specifier
```

Examples:

```text
THREE_WAY + FULL_TIME + PRE_MATCH
THREE_WAY + FULL_TIME + LIVE
TOTALS + FULL_TIME + PRE_MATCH + 2.5
HANDICAP + FULL_TIME + PRE_MATCH + -1
```

Observations with different identities must not be compared -- enforced
end to end: `odds_snapshots`' dedupe index, `OddsRepository.find_latest`/
`find_last_two`/`find_latest_for_market`, and the `signals`/`movements`
identity keys all filter/group on the full tuple above, not just
`market_type`/`period`/`line`. Two snapshots differing only in
`rules`/`specifier` are a different market -- and so are two differing
only in `phase`: a pre-match price and a live, in-play price for the
same event/market/outcome were never simultaneously valid, so comparing
them is comparing two different moments in the match, not a genuine
cross-bookmaker discrepancy. `phase` has no default -- every collector
declares which phase it produces explicitly (`DEFAULT_MARKET` for
pre-match sources; `MozzartFileCollector`'s `/live/matches` data uses
`LIVE_MARKET` instead).

`TOTALS + FULL_TIME + PRE_MATCH + 2.5` and `HANDICAP + FULL_TIME +
PRE_MATCH + -1` above are real constants (`TOTALS_2_5_MARKET`,
`HANDICAP_MINUS_1_MARKET`), not just examples -- `ApiFootballCollector`
produces both alongside `DEFAULT_MARKET`, the second and third market
types this project actually detects. `models.market.
required_outcomes(market_type)` is what a market type's outcome set
actually *is* (`("1", "X", "2")` for THREE_WAY and HANDICAP -- the same
codes, since `HANDICAP_MINUS_1_MARKET` is the *3-way* "Handicap Result"
flavor, not 2-way Asian Handicap, deliberately deferred; `("OVER",
"UNDER")` for TOTALS) -- `analysis.opportunity_detection.
detect_surebet_candidates` uses it (checking `set(best) ==
set(required_outcomes)`, not a hardcoded outcome count) instead of
assuming three outcomes, and feeds it to `analysis.arbitrage.
calculate_arbitrage`'s own `required_outcomes` parameter, which already
summed a generic margin over however many outcomes it was given.
HANDICAP and THREE_WAY sharing outcome codes is harmless -- `market_type`
is itself part of `MarketIdentity`'s equality, so the two are never
compared or merged despite the identical `"1"`/`"X"`/`"2"` labels.

`line`/`rules`/`specifier` are canonicalized in `__post_init__`, not by
every caller: `line` goes through `canonical_decimal()`
(`Decimal(format(value.normalize(), "f"))`), which strips formatting
differences (`Decimal("2.50")` -> `Decimal("2.5")`) without producing
`Decimal.normalize()`'s exponential form for a round number
(`Decimal("100")` alone would normalize to `Decimal("1E+2")`); `rules=""`/
`specifier=""` become `None`. Python's own `MarketIdentity.__eq__`
already treated `Decimal("2.50")` and `Decimal("2.5")` as equal
(`Decimal.__eq__` compares value, not formatting) -- but every SQL
identity/dedupe key stores and compares `str(line)` as plain text, where
"2.50" and "2.5" would not have matched. Canonicalizing at construction
closes that gap before TOTALS/HANDICAP (the first market types where
`line` is actually populated) makes it a real, not just theoretical, bug.

---

## Validation Stages

### STRUCTURAL

Checks shape and parseability.

### SEMANTIC

Checks whether values make domain sense: odds are a finite `Decimal`
greater than 1.0 (`Decimal("NaN")`/`Decimal("Infinity")` are rejected
explicitly, not left to raise `decimal.InvalidOperation` or slip through
as merely "suspiciously high"), a `THREE_WAY` market has exactly outcomes
`{"1", "X", "2"}`, home/away team names are compared case-insensitively
(not just by whitespace), and every timestamp is timezone-aware.

### IDENTITY

Checks event and market resolution.

### TEMPORAL

Checks freshness and temporal coherence.

The analysis layer should receive only observations that passed all required stages for the requested analysis.

---

## Time Model

Internal runtime timestamps are UTC and timezone-aware -- enforced at
one boundary, `OddsRepository.save()`/`save_all()`, which convert
`observed_at`/`source_timestamp` to UTC before storing them as text. A
source is free to report whatever offset it wants; two equally-valid but
differently-offset timestamps for the same instant would otherwise sort
incorrectly against each other under plain lexicographic `ORDER BY`.

Important concepts:

```text
event_start_time
observed_at
source_timestamp
quote_time
analysis_time
```

`quote_time` (`OddsSnapshot.quote_time`, a property, not a stored
column) is `source_timestamp` if the source provided one, else
`observed_at` -- freshness is measured against this, not `observed_at`
directly. `observed_at` is only "when our poll happened", which can look
arbitrarily fresh even for a price the source itself computed or cached
well before the request landed; a source that timestamps its own prices
(the-odds-api's per-bookmaker `last_update`) is reporting something
age-relevant a poll timestamp alone can't capture. A source with no such
timestamp (the JSON demo, Mozzart) falls back to `observed_at`, the same
behavior as before `quote_time` existed.

`quote_time` is not only a Freshness concern: `detect_bookmaker_lag` and
`detect_rapid_movement` also compare across snapshots, and both use
`quote_time` for the same reason -- one poll gives every bookmaker in
that response the same `observed_at`, hiding real per-bookmaker
staleness/movement that only shows up in each one's own
`source_timestamp`. `detect_rapid_movement` additionally treats a
`quote_time` regression (a source's own clock running backward, despite
correctly-ordered `observed_at` values) as a flagged data anomaly
(`MovementResult(detected=False, clock_regression=True, ...)`), not
something that raises -- a genuine `observed_at` ordering violation
(the caller's own contract) still raises `ValueError`.

`analysis_time` is created once per analysis execution and passed
through as an explicit, required parameter
(`detect_surebet_candidates`, `detect_value_gap_candidates`,
`build_opportunity_report`) -- never derived from the observations being
analyzed (e.g. their own newest `observed_at`/`quote_time`). Deriving it
that way would make a batch of uniformly old-but-mutually-close
snapshots look "fresh" relative to itself regardless of how much real
time has passed, defeating the freshness check it feeds. `pipeline.py`'s
demo path is the one sanctioned exception: its fixed calendar timestamps
would otherwise always register as ancient, so it explicitly computes a
stand-in "now" (the newest `quote_time` across everything ingested that
run) and passes it in like any other caller would -- the workaround
lives in `pipeline.py`, never as a fallback inside the detection
functions themselves.

---

## Storage Strategy

Historical observations are required for:

```text
rapid movement
bookmaker lag
volatility
historical analysis
reprocessing
```

Backed by a persistent SQLite file (`DB_PATH` env var, default
`data/anomaly_detection.db`; `:memory:` still works for tests). The
connection is opened with a 30s busy-timeout specifically because
`scripts/watch_capture.py` can spawn concurrent `app.py` processes
against the same file.

### Schema migrations

Because the database is a real persistent file, `initialize_database()`
cannot just be a `CREATE TABLE IF NOT EXISTS` script and call it
done -- that only ever adds new tables/indexes, it never changes an
*existing* table already on an older shape. `storage.migrations` tracks
schema version via `PRAGMA user_version` and a `MIGRATIONS` list, each
applied at most once (guarded by the version check):

```text
migrate(connection):
    current = PRAGMA user_version
    for version, migration in enumerate(MIGRATIONS, start=1):
        if version <= current: continue
        migration(connection)
        PRAGMA user_version = version
```

`initialize_database()` calls `migrate()` on every startup: a brand-new
database runs every migration, an up-to-date one runs none, and an older
persistent file (e.g. `signals`/`movements` from before they had
`market_rules`/`market_specifier`, or before `competitions`/
`source_competition_mappings`/`market_phase`/`events.competition_id`/
`collector_runs.provider_id`/`parser_version`/`source_payload` existed)
is upgraded in place without losing what it already had. Once
shipped, a migration's SQL is immutable -- a database that recorded it
as applied never runs it again, so a correction is a new migration, not
an edit to an old one.

This is not wrapped in a transaction with its `PRAGMA user_version`
write, and deliberately so: verified directly that Python's sqlite3
module (legacy `isolation_level`-based transaction handling) only
auto-opens an implicit transaction before DML, never before DDL
(`CREATE`/`ALTER`/`DROP`) or `PRAGMA`, so a bare `execute()` of one of
those runs in autocommit mode and is not rolled back the way plain DML
is, even inside `with connection:` (a `CREATE TABLE` survives an
exception raised right after it in the same `with` block; an `INSERT`
in the same position does not) -- even though SQLite itself can
genuinely roll back DDL. An explicit `BEGIN` first (the same technique
`FixtureCatalog.match()` uses for its own writes) *would* make DDL
transactional here -- Python 3.12's `autocommit=False` is not actually
required for that. The real obstacle is `executescript()` (used by
migrations 1 and 3): it always commits any pending transaction before
running the script, defeating a manual `BEGIN` before it even starts.
So each migration is made idempotent instead: `_add_column_if_missing()`
only `ALTER`s a column that isn't already there, and every index rebuild
`DROP`s (`IF EXISTS`) immediately before recreating it -- a retry after
a crash between any
two statements, including one between a migration finishing and its
`PRAGMA user_version` write landing, converges to the same end state
rather than erroring on "duplicate column name" or similar.

Current tables:

```text
odds_snapshots           unique-indexed on (event, bookmaker, full
                          MarketIdentity including market_phase,
                          outcome, observed_at); re-saving an identical
                          snapshot is a no-op rather than a duplicate row
collector_runs            includes provider_id/parser_version/
                          source_payload (see CollectorRun/Collector
                          Contract above) -- the exact response this
                          run's records were parsed from, once per run
raw_payloads              every ingested RawEventOdds, accepted or
                          rejected, with its rejection reason, linked to
                          its CollectorRun
teams                     canonical team registry, unique per
                          (canonical_name, sport)
events                    canonical event registry: sport, league,
                          competition_id, home_team_id, away_team_id,
                          start_time. competition_id (FK into
                          competitions) is an event's real identity key
                          for competition -- league is kept as the
                          canonical display string, but matching no
                          longer depends on that string staying the
                          same
source_team_mappings      (provider_id, sport, raw team name) ->
                          team_id, the permanent memory behind
                          FixtureCatalog's cross-source team matching
                          (column is still named `source` at the SQL
                          level; see Matching Layer above for
                          provider_id vs OddsCollector.source)
competitions              canonical competition/league registry, unique
                          per (canonical_name, sport) -- same shape as
                          teams, one level up
source_competition_mappings  (provider_id, sport, raw league name) ->
                          competition_id, the permanent memory behind
                          FixtureCatalog's cross-source league matching
                          (column also still named `source`)
signals                   stateful (SUREBET/VALUE_GAP): status ACTIVE/
                          RESOLVED/EXPIRED, first_seen_at/last_seen_at/
                          resolved_at, unique-indexed on
                          (signal_type, event, full MarketIdentity
                          including market_phase, outcome) so a
                          detection is upserted (reconciled) rather than
                          duplicated across poll cycles. reconcile()
                          only resolves a signal whose own (event,
                          market, outcome) identity is in evaluated_keys
                          -- something this sweep couldn't evaluate
                          (stale/missing data, or too few bookmakers for
                          that specific outcome) must not be silently
                          resolved just because it produced no candidate.
                          EXPIRED is a separate lifecycle state
                          (SignalRepository.expire_active_signals, see
                          Next Architectural Step below) for a signal
                          whose event fell out of touched_events scope
                          entirely -- reconcile() can never resolve
                          that, since it is simply never evaluated again
movements                 append-only point-in-time transitions,
                          unique-indexed on the full transition
                          (including full MarketIdentity with
                          market_phase) so a re-run detection sweep
                          can't duplicate one
bookmakers                canonical bookmaker registry (migration 7),
                          unique per normalized_name (case/whitespace/
                          punctuation-only normalization -- no fuzzy
                          matching, see BookmakerCatalog below)
source_bookmaker_mappings  (provider_id, source_bookmaker_id) ->
                          bookmaker_id, the permanent memory behind
                          BookmakerCatalog's cross-provider bookmaker
                          matching -- source_bookmaker_id is the
                          provider's own stable id when it has one
                          (e.g. api-football's "8"), or
                          normalize_bookmaker_name(source_name)
                          otherwise, for providers with no stable
                          per-bookmaker id (JSON demo, Mozzart)
```

`OddsRepository.save_all()` persists every outcome of one raw ingested
record in a single transaction, so a failure partway through never
leaves a half-written market snapshot. `find_latest_for_market` selects
the latest snapshot per (bookmaker, outcome) via a `ROW_NUMBER() OVER
(PARTITION BY ... ORDER BY observed_at DESC, id DESC)` window query, not
a join between separate `MAX(observed_at)`/`MAX(id)` subqueries -- the
two maxima are not guaranteed to come from the same row (a
later-arriving snapshot can report an *older* `observed_at` than one
already stored), so that join could silently drop a (bookmaker, outcome)
out of the result entirely.

Both `OddsRepository.save`/`save_all` and `SignalRepository.reconcile()`
use `with self._connection:` (Python's sqlite3 commits on success, rolls
back on any exception) rather than a trailing `commit()` -- a genuine
transaction, not just a batch of statements that happen to be followed
by one commit call. A `reconcile()` call upserting several candidates
and then resolving stale ones either lands entirely or not at all; there
is no state where some signals reflect this sweep and others still
reflect the previous one.

Competitions/leagues now have their own canonical registry
(`competitions`/`source_competition_mappings`, see Storage Strategy
above) the same way teams do. Bookmakers now do too
(`bookmakers`/`source_bookmaker_mappings`, migration 7,
`storage.bookmaker_catalog.BookmakerCatalog`) -- deliberately without
`FixtureCatalog`'s fuzzy matching, since bookmaker brand names are a
small, stable set where a wrong auto-merge would permanently corrupt
consensus/outlier math, unlike a harmless duplicate identity; see the
eleventh round below for the full rationale.

---

## Current vs Historical Queries

Current-market analysis requires the latest relevant observation per:

```text
event
bookmaker
market
outcome
```

Historical analysis requires explicit timestamp ordering.

Database insertion order must never be treated as observation order.

---

## Error and Anomaly Classes

The architecture distinguishes:

```text
DATA_QUALITY_ANOMALY   caught by validation (structural/semantic errors,
                       rejected before reaching analysis)
MARKET_ANOMALY         outlier_detector, bookmaker_lag, movement_detector
                       -- VALUE_GAP in the opportunity report is this
                       class, not a guarantee
ARBITRAGE_SIGNAL       arbitrage.calculate_arbitrage -- SUREBET in the
                       opportunity report
SYSTEM_ANOMALY         collector failures (FAILED CollectorRun)
```

This prevents parser errors and source failures from being misclassified as market opportunities.

A large VALUE_GAP does not distinguish "bookmaker genuinely mispriced
this" from "their line is simply wrong" (a DATA_QUALITY_ANOMALY that
happened not to get caught by structural/semantic validation because the
value itself is well-formed, just off). Unlike SUREBET, which is
risk-free by construction, VALUE_GAP is a single directional bet on
which of those two explanations is true.

---

## Next Architectural Step

**Resolved:** `OddsIngestionService` implements collect → validate → match
→ persist → `CollectorRun`, with `RawPayloadRepository` alongside it for
traceability. Freshness and analysis stay outside the service (evaluated
by the caller against the repository's stored snapshots) per the
Guiding Principle below -- ingestion orchestration stays separate from
pure analysis logic, as originally intended here.

**Resolved:** the persistent event/fixtures catalog. `FixtureCatalog`
(see Matching Layer above) replaced `build_events_from_raw()` and
`build_demo_events()` -- every source now resolves teams/events against
the durable `teams`/`events`/`source_team_mappings` tables, and
`MozzartFileCollector` is wired into `app.py` as a supplemental
collector sharing that same catalog. Verified end-to-end: a Mozzart
capture reporting the same match as the JSON demo data ("Manchester
United vs Liverpool") resolved to one shared canonical event, and the
opportunity report found a real surebet combining a Mozzart leg with a
JSON-demo-bookmaker leg -- cross-source matching that was not possible
before this.

**Resolved:** that work surfaced a gap -- `build_opportunity_report`
compared odds across bookmakers without checking whether they were ever
actually simultaneously valid, the way `main()`'s per-event display loop
already did via `validate_freshness`. Fixed by making
`freshness_policy: FreshnessPolicy` a required parameter of
`build_opportunity_report`, applied per event before computing best
odds / arbitrage / outliers -- required rather than defaulted, since the
right window depends on real polling frequency and there's no sensible
one-size-fits-all value. Confirmed against the exact case that surfaced
it: re-running the Mozzart-merges-into-the-JSON-demo-fixture scenario
now correctly reports no opportunities instead of the cross-source
SUREBET/VALUE_GAP rows it produced before the fix.
`build_movement_report` did not need the same change: it always compares
a bookmaker against its own earlier reading rather than across
bookmakers at a point in time, and its own `max_window` parameter
already bounds how far apart those two readings can be.

**Resolved:** where and how to persist derived information, not just raw
odds. The Detection Layer's candidates (see above) are now the shared
input to both Storage and Reporting instead of Reporting recomputing
everything itself. `SignalRepository` gives SUREBET/VALUE_GAP a stateful
lifecycle (`reconcile()`: ACTIVE -> still-ACTIVE -> RESOLVED when absent
from a sweep -> reactivated if it reappears, without losing the original
`first_seen_at`), while `MovementRepository` is append-only and
deduplicates on the full transition since a price change has no
lifecycle to track. Detection itself stays unconditional -- "worth
reporting" thresholds are applied only at read time (Reporting Layer),
so tightening or loosening them later never requires having discarded
data. The one deliberate exception is VALUE_GAP's threshold, which is
part of what defines an outlier, not a presentation filter, so the same
`MIN_VALUE_GAP_PERCENT` value drives both detection and reporting.
Verified end-to-end: a real 3-way surebet was persisted as ACTIVE, then
correctly resolved (`status=RESOLVED`, `resolved_at` set) once a later
odds change killed the arbitrage -- and that same price change was
independently recorded in `movements`.

**Resolved:** a round of correctness fixes made ahead of adding more
bookmakers/markets, so growth doesn't compound existing bugs rather than
add to the open item below. `MarketIdentity` is now honored end to end
(every dedupe/lookup key includes `rules`/`specifier`, not just
`type`/`period`/`line`) across `odds_snapshots`, `signals`, and
`movements` -- see MarketIdentity and Storage Strategy above.
`find_latest_for_market`'s `MAX(observed_at)`/`MAX(id)` join, which could
silently drop a (bookmaker, outcome) out of the result when a
later-arriving snapshot reported an older `observed_at` than one already
stored, is now a `ROW_NUMBER()` window query. `analysis_time` is now an
explicit, required parameter everywhere freshness is checked instead of
`max(observed_at)` within the batch itself -- see Time Model above for
why that computation hid genuine staleness. `OddsIngestionService` now
persists one raw record's outcomes in a single transaction
(`OddsRepository.save_all`) and guarantees a `CollectorRun` is always
recorded even if a record's processing raises unexpectedly -- see
CollectorRun above. `FixtureCatalog`/`TeamNormalizer` no longer merge a
fuzzy match when the top two candidates are nearly tied, and event
resolution now scopes by league -- see Matching Layer above. `app.py`
was split into `AppConfig`/`build_runtime`/`run_ingestion`/
`run_analysis` (still plain functions/dataclasses, no framework/DI
container), and `ODDS_SOURCE` now fails fast on an unrecognized value
instead of silently falling back to demo data.

A web dashboard / notification layer reading from `SignalRepository` /
`MovementRepository` (see Reporting Layer) remains the next open item --
the persisted tables exist but have no presentation layer of their own
yet; only the text reports recompute candidates on demand.

**Resolved:** a second round of correctness fixes, this time about the
persistent database itself rather than the analysis it stores. A real
`CREATE TABLE IF NOT EXISTS` script only ever helps a brand-new
database, never an existing one already on an older schema shape --
`storage.migrations` (`PRAGMA user_version`) closes that gap, see
Storage Strategy above. `OddsRepository.save`/`save_all` and
`SignalRepository.reconcile()` now use `with self._connection:` instead
of a trailing `commit()`, so an exception partway through rolls back
everything already executed rather than leaving it committed.
`reconcile()` also gained required `market`/`evaluated_event_ids`
parameters -- without them, a sweep that couldn't evaluate an event
(stale/missing data, distinct from "evaluated, genuinely absent") could
resolve its signal exactly like a confirmed-gone one would, and a sweep
over one market could resolve an ACTIVE signal belonging to a market it
never analyzed. `configure_connection()` now turns on `PRAGMA
foreign_keys` (off by default in SQLite despite the schema's own
`REFERENCES` declarations), and `FixtureCatalog.start_time` is
normalized to UTC the same way `OddsSnapshot` timestamps already were --
see Matching Layer and Time Model above. `AppConfig` was extended to
cover every remaining env var (`ODDS_API_MODE`, capture dirs,
`MOZZART_MODE`), so its own docstring's claim that nothing downstream
reads `os.environ` directly is now actually true.

**Resolved:** a third round, closing gaps the second round's own fixes
surfaced. Freshness compared `analysis_time` against `observed_at`
(when *we* polled) rather than `quote_time` (`source_timestamp` when the
source provides one) -- see Time Model above; a source-reported price
computed hours earlier but fetched by a fast poll looked fresh under the
old comparison. `reconcile()`'s `market`/`evaluated_event_ids` pair (see
the previous entry) was itself still event-granularity, too coarse for
VALUE_GAP, where one outcome can have enough bookmakers to evaluate
while a sibling outcome for the same event does not (`detect_outliers`
already skips under-quoted outcomes) -- both parameters were replaced by
`evaluated_keys: Collection[SignalIdentity]` (`event_id` + `market` +
`outcome`, see Storage Strategy above), strictly more precise since a
`SignalIdentity` already encodes its own market. League/competition
names went through the same canonicalization team names already had
(`FixtureCatalog`, migration 3) -- closing a gap the *previous* round's
own league-scoped event matching had opened: two sources spelling one
competition differently would otherwise resolve to two canonical events
that could never be compared, see Matching Layer above.
`RawEventOdds.source_id` lets `Bookmaker.id` come from a source's own
stable identifier instead of one derived from a display name that can
legitimately change. Migration 2's "wrapped in one transaction" claim
turned out to be false for DDL/PRAGMA under Python's sqlite3 (verified
directly, see Storage Strategy above) -- migrations are idempotent
instead, the guarantee actually achievable here.
`OddsIngestionService.run()`'s status now reflects raw-payload audit
failures and the run's own possible mid-loop abort, not just per-record
accept/reject counts, see CollectorRun above. `app.py` (446 lines) was
split into `config.py` (`AppConfig`/`load_config`), `runtime.py`
(`Runtime`/`build_runtime`), and `pipeline.py` (collector construction,
`run_ingestion`/`run_analysis`/`persist_detected_signals`), leaving
`app.py` as just `main()`.

**Resolved:** a fourth round, split between real correctness bugs and a
scope-tightening pass ahead of adding more market types/sources.
`MarketPhase` (PRE_MATCH/LIVE) is now a required field on
`MarketIdentity` (migration 4) -- Mozzart's `/live/matches` data was
being tagged with the same market identity as every pre-match source,
even though the two were never simultaneously valid, see MarketIdentity
above. The same poll-vs-source-timestamp gap Freshness was already fixed
for in the third round turned out to still be open in
`detect_bookmaker_lag` and `detect_rapid_movement`; both now compare
`quote_time`, and a source clock regression is a flagged
`clock_regression` result rather than a raised exception that would
crash the whole sweep -- see Time Model above. The core pipeline was
split from reporting: `run_analysis` (which mixed persistence with
printing) is gone, replaced by `run_detection` (persist only) and
`reporting.console.print_reports` (every human-facing report, not
imported by the core pipeline, not called by `app.py` by default) --
`min_surebet_profit_percent` came out of `AppConfig` accordingly, since
it was never a detection threshold. `FixtureCatalog`'s `provider_id` was
separated from `OddsCollector.source`, and `match()` now wraps its whole
resolve-or-create cycle in one `BEGIN IMMEDIATE` transaction -- see
Matching Layer above for both. `Event.competition_id` (a stable FK into
`competitions`) was added, and event identity is now keyed on it instead
of `league`'s display string, closing the gap where a future competition
rename could have silently detached existing events from later matches
-- see Matching Layer and Storage Strategy above.

Deliberately deferred out of this round: canonicalizing `Decimal` line
representation ahead of TOTALS/HANDICAP, an explicit event lifecycle
(completed/expired, so `list_events()` doesn't re-evaluate long-finished
matches forever), extracting `SignalType`/`SignalIdentity` into their
own module (closing `storage.signal_repository`'s current inverted
dependency on `analysis.opportunity_detection`) plus an `EventResolver`
Protocol, and preserving the original source payload for live sources
alongside the parsed `RawEventOdds`. None of these are correctness bugs;
they are future-growth prep, deliberately left until there are enough
real market types and sources to know what actually needs generalizing.

**Resolved:** a fifth round, picking up the four items the previous round
deliberately deferred. `MarketIdentity.line`/`rules`/`specifier` are now
canonicalized on construction (`canonical_decimal()`, `rules=""`/
`specifier=""` -> `None`) -- see MarketIdentity above; Python's own
`__eq__` already treated differently-formatted equal Decimals as equal,
but every SQL identity/dedupe key compared their `str()` form as plain
text, where they would not have matched. `run_ingestion()` no longer
hands `run_detection` every event `FixtureCatalog.list_events()` has ever
created; each `OddsIngestionService` now reports its own `touched_events`
(every event a record actually matched against this run), unioned across
the cycle's polls -- a long-finished match simply stops being handed to
detection once nothing reports on it anymore, rather than being
evaluated and reported stale forever with no data source ever saying the
match is over. `SignalType`/`SignalIdentity` moved into a new
`models/signal.py`, closing `storage.signal_repository`'s inverted
dependency on the Analysis Layer for its own persisted-signal identity
concept -- `SurebetCandidate`/`ValueGapCandidate` stay in
`analysis.opportunity_detection` (detection-layer outputs, not shared
domain types), and `signal_repository`'s `from_surebet`/`from_value_gap`
adapters still import them, a normal one-directional "convert this into
what storage needs" dependency, not the same problem. The `EventResolver`
Protocol (see Matching Layer above) now names the shape
`OddsIngestionService` actually depends on. Collectors now return
`CollectionResult` (`source_payload` plus the parsed records) instead of
a bare list -- see Collector Contract above; `CollectorRun` gained
`provider_id`/`parser_version`/`source_payload` (migration 6) so the
exact response a run's records were parsed from survives once per run,
alongside the already-persisted per-record `RawEventOdds` in
`raw_payloads` -- a parser bug can now be fixed and the original
historical response reprocessed.

**Resolved:** a sixth round, an external review of round five's own
work turning up one real correctness bug and one real gap it opened.
`detect_surebet_candidates` added an event to `evaluated_keys` *before*
checking whether all three outcomes were actually priced this sweep --
the same "couldn't tell this sweep" vs "confirmed gone" distinction
`evaluated_keys` exists to protect (see Storage Strategy above), except
this time SUREBET's own missing-outcome case was the gap, not
missing/stale data. Fixed by moving `evaluated_keys.add(...)` after the
`len(best) != 3` check, mirroring the ordering VALUE_GAP's per-outcome
scoping already had. Separately, round five's `touched_events` scoping
fixed "re-evaluated and permanently stale forever" but opened a
narrower gap: an event that stops being touched can never be resolved
by `reconcile()` either, since it is simply never evaluated again, so
its ACTIVE signal would stay ACTIVE forever with no mechanism to say
otherwise -- "not evaluated" was never the same claim as "confirmed
gone." `SignalRepository.expire_active_signals`, a new `EXPIRED` status
distinct from `RESOLVED` (see Storage Strategy above), and
`AppConfig.signal_ttl` close that gap: a separate lifecycle step, not
folded into `reconcile()`, keyed on `event.start_time` (not the last
quote time -- that would make the same event's effective lifecycle
depend on how long a particular provider happened to keep reporting it,
rather than a stable domain fact) plus the configured TTL, and guarded
against expiring a signal reconfirmed the very same detection cycle it
runs in (`last_seen_at < expired_at`, with both calls in
`run_detection` sharing one `now`).

**Resolved:** a seventh round, a further external review of round six's
own work. `detect_surebet_candidates` had the exact same
evaluated_keys-ordering bug round six fixed for a different reason:
`evaluated_keys.add(...)` ran before checking all three outcomes were
priced, so a surebet genuinely missing one leg this sweep still counted
as "evaluated" and could be incorrectly resolved -- fixed the same way
VALUE_GAP's own per-outcome scoping already was. `FreshnessPolicy`
gained `allowed_future_skew` (default 10s) so ordinary clock skew a few
seconds ahead of `analysis_time` is no longer rejected as
`snapshot-from-future`. The migration idempotency comments (Storage
Strategy above) overstated what Python 3.12 is actually needed for --
verified directly: Python's sqlite3 only auto-opens an implicit
transaction before DML, not DDL/PRAGMA, so DDL *can* be made
transactional with an explicit `BEGIN`; `autocommit=False` was never
actually required, the real obstacle is `executescript()` (migrations 1
and 3) always committing any pending transaction before running.
`from_surebet`/`from_value_gap` moved from `storage.signal_repository`
into `pipeline.py` -- the previous round's domain-type extraction closed
the inverted dependency for `SignalType`/`SignalIdentity` but left
storage still importing `SurebetCandidate`/`ValueGapCandidate` from the
Analysis Layer just for these two adapters; storage now imports nothing
from `analysis` at all. `run_detection()` no longer prints anything --
it returns its summary and `app.py`'s `main()` prints it, so the core
pipeline stays usable by any caller that wants detection with no console
output as a side effect. Deliberately left alone: core detection still
only analyzes `DEFAULT_MARKET` (pre-match), matching this project's
stated MVP scope -- `LIVE_MARKET` detection later means a sweep
parameterized by explicit `MarketIdentity` values, not swapping which
single constant `run_detection` hardcodes.

**Resolved:** an eighth round, shifting from architectural cleanup to
proof -- a second real pre-match source, `ApiFootballCollector` (see
Collector Contract above), wired in as an opt-in supplemental collector
(`API_FOOTBALL_KEY`), the same shape Mozzart already uses. Against a
genuinely different real provider (not another fixture), this exercised
`provider_id`, team normalization, competition mapping, fixture
matching, UTC normalization, raw payload retention, bookmaker identity,
and freshness all at once, rather than any one of them in isolation.
Verified against a real captured response before writing test fixtures.

**Resolved:** a ninth round, the second half of the same "prove it"
push -- a second market type, TOTALS 2.5 (see MarketIdentity above),
produced end-to-end by `ApiFootballCollector` from a real `"Goals
Over/Under"` bet. This immediately surfaced the exact gap the previous
round's own review had already flagged in passing:
`detect_surebet_candidates` checked `len(best) != 3`, hardcoding
THREE_WAY's outcome count into what should have been a market-agnostic
check -- fixed with `required_outcomes(market_type)` (see MarketIdentity
above). `find_best_odds` and `detect_outliers`/VALUE_GAP needed no
changes at all -- both already group by whatever `outcome` string is
present, with no THREE_WAY-specific assumption baked in to begin with.
Deliberately not wired into `run_detection`'s core sweep (still
`market=DEFAULT_MARKET` only) -- the same boundary `LIVE_MARKET` drew
before it: proving the detection layer generalizes is a different
question from deciding `run_detection` should sweep multiple markets,
left open rather than decided implicitly here.

**Resolved:** a tenth round, closing out the roadmap's "prove it" trio
with a third market type -- HANDICAP -1 (see MarketIdentity above),
produced end-to-end by `ApiFootballCollector` from a real `"Handicap
Result"` bet, the same bundled-many-lines-in-one-response shape
`"Goals Over/Under"` already had. Deliberately the 3-way flavor, not
2-way Asian Handicap (api-football.com's own "Asian Handicap" bet needs
real home/away line-pairing semantics this round didn't settle) --
"Handicap Result" sidesteps that by reusing THREE_WAY's exact outcome
codes, so `calculate_arbitrage`/`find_best_odds`/`detect_outliers`
needed zero further changes, the same "already generic enough" story
TOTALS proved for the latter two last round. `MarketIdentity` itself
needed no changes either -- `market_type` alone already keeps a
HANDICAP snapshot from ever being compared against a THREE_WAY one for
the same event, even though they share identical outcome codes.

**Resolved:** an eleventh round, closing the gap between "ingested" and
"actually analyzed" left open by rounds nine and ten, plus three smaller
fixes surfaced reviewing that state. `run_detection()` still called
`persist_detected_signals` with `market=DEFAULT_MARKET` only, even though
`ApiFootballCollector` had been ingesting TOTALS/HANDICAP snapshots since
those two rounds -- a real cross-provider TOTALS or HANDICAP arbitrage
would sit in `odds_snapshots` forever, never evaluated.
`pipeline.DETECTED_MARKETS` (`DEFAULT_MARKET`, `TOTALS_2_5_MARKET`,
`HANDICAP_MINUS_1_MARKET`) is now looped over inside `run_detection`, one
`persist_detected_signals` call per market. Looping naively is safe here
because `SignalRepository.reconcile()`'s stale-resolution is already
scoped by `SignalIdentity` (which carries `market`) -- see Storage
Strategy above -- so a TOTALS sweep's `evaluated_keys` can never contain
a HANDICAP or DEFAULT_MARKET identity, and reconciling one market can
never resolve a signal belonging to a market this cycle didn't just
evaluate. `movements_recorded` is summed across the loop, but
`active_surebets`/`active_value_gaps` are read once via
`SignalRepository.find_active()` *after* the whole loop rather than taken
from any single iteration's return value, since `find_active()` counts
across every market at once and the last market processed would
otherwise silently clobber the true cross-market total.

Second, `parse_api_football_response` now reads each fixture's own
`"update"` field into `RawEventOdds.source_timestamp` -- the same
freshness-timestamp gap already closed for the-odds-api's `last_update`
in an earlier round, just missed for this collector at the time. Read
once per fixture item (outside the bookmaker loop), since
api-football.com reports one `"update"` per odds entry, not one per
bookmaker the way the-odds-api's `last_update` is.

Third, and the largest piece: a canonical bookmaker registry
(`BookmakerCatalog`, `bookmakers` + `source_bookmaker_mappings`,
migration 7). Before this, ingestion built `Bookmaker(raw.source_id or
raw.source.lower(), raw.source)` directly from whatever a provider
reported -- the same real Bet365 became two different `Bookmaker`
identities (the-odds-api's `"bet365"`, api-football's `"8"`), silently
corrupting every downstream `min_bookmakers`/consensus/outlier check
that assumes one real bookmaker has one identity. `BookmakerCatalog.
resolve()` follows the same shape `FixtureCatalog` already uses for
teams/competitions (an existing-mapping cache keyed by provider, falling
back to name resolution, falling back to creating a new canonical row),
deliberately without any fuzzy matching: `bookmakers.normalized_name` is
`UNIQUE` at the schema level, so "an existing canonical bookmaker with
this normalized name" is always zero-or-one matches, never a guess
between ambiguous candidates. This is intentionally more conservative
than `FixtureCatalog`'s own team/competition matching -- team names
genuinely vary a lot across sources and fuzzy matching earns its keep
there, but bookmaker brand names are a small, stable set, and a wrong
auto-merge here (deciding two differently-named bookmakers are the same
when they might be genuinely different products/feeds) would permanently
contaminate consensus math, whereas failing to merge two spellings of
the same real bookmaker only costs a harmless, temporary duplicate
identity. The canonical `Bookmaker.id` is always a freshly generated
`bookmaker-<uuid>`, never a provider's own bookmaker id, so this
project's own domain identity never depends on any one provider's
identifiers surviving unchanged -- `"bet365"`/`"8"` are only ever
recorded as mappings. `OddsIngestionService` now takes a
`bookmaker_catalog` constructor parameter and resolves through it;
`run_ingestion()` constructs one `BookmakerCatalog` per collector, scoped
by `provider_id`, the same per-provider cache-sharing `FixtureCatalog`
already does.

Fourth, and explicitly non-blocking: `ApiFootballCollector.collect()`
still only ever fetches page 1 of a date's results -- `paging.total` was
never inspected. `parse_api_football_response` now logs a warning when a
response's `paging.total > 1`, so a paginated day is visible instead of
silently incomplete; actually looping `collect()` over every page is
deferred until continuous/production polling needs it.

**Resolved:** a twelfth round, pivoting deliberately from "correctness/
architecture proof of concept" to "operational MVP" -- a system that
runs unattended over real providers for hours/days and builds up real
historical odds, rather than another market type or abstraction.

First, `ODDS_SOURCE=api-football`: `build_collectors()` gained a branch
building `ApiFootballCollector` directly as the *primary* collector
(config.py's `_VALID_ODDS_SOURCES` gained the value), so a deployment
with only an `API_FOOTBALL_KEY` no longer has to also pull in the JSON
demo's synthetic primary collectors just to run. `_supplemental_
collectors()` now skips building a second `ApiFootballCollector` when
api-football is already the primary source, so it is never polled twice
in one cycle. `run_detection`'s `analysis_time` branch, previously
`odds_source == "the-odds-api"`, is now `odds_source != "demo"` -- every
real source needs real wall-clock time, and this generalizes
automatically to any future real source instead of needing the check
updated by hand each time one is added.

Second, `poller.py` -- the first actual continuous odds-monitoring
process (`app.py`'s single-shot `main()` is unchanged, still useful for
a cron/systemd-timer-driven single poll). `run_forever()` repeats
`run_cycle()` (the same `run_ingestion` -> `run_detection` sequence)
every `POLL_INTERVAL_SECONDS`, catching and logging any exception a
cycle raises rather than ending the process -- the outer safety net on
top of `OddsIngestionService.run()`'s existing per-collector failure
isolation. `main()` handles `SIGINT`/`SIGTERM` via a `threading.Event`
checked between cycles, so an in-progress cycle always finishes cleanly.
`run_ingestion()`'s two `print()` calls became `logger.info(...)` as
part of this -- the same gap `run_detection`'s own `print()` removal
closed for detection several rounds ago, just never revisited for
ingestion until a days-long process made it matter.

Third, real API-Football pagination, replacing the previous round's
warning-only handling: `ApiFootballCollector.collect()` now fetches and
merges every page of a paginated response (`_fetch_all_pages`) before
parsing. `parse_api_football_response` was split into itself (unchanged
public signature) and `_parse_envelopes(fixtures_data, odds_data,
observed_at)` operating on already-loaded dicts, so `collect()` can hand
it merged, multi-page envelopes directly -- each page is parsed twice
from the same fetched bytes (once with `parse_float=Decimal` for
extraction, once as plain JSON for `source_payload`, since a
Decimal-parsed dict cannot be re-serialized by plain `json.dumps()`). A
bounded `_MAX_PAGES` guard raises `ApiFootballError` if `paging.current`
never catches up to `paging.total`, protecting an unattended poller from
an infinite fetch loop.

Fourth, `scripts/inspect_data.py` -- data-sanity tooling, deliberately
not a report: `summary`, `events`/`event <id>` (one event's full odds
history), and `cross-provider` (events whose teams were independently
resolved by 2+ distinct providers via `source_team_mappings`). That last
command works around `odds_snapshots` having no `provider_id`/
`collector_run_id` column of its own -- there is no direct way to say
"this specific snapshot came from provider X", only "this canonical
bookmaker/team has been seen from provider X at some point" -- so
team-mapping provider diversity is the closest already-existing signal
for a genuine cross-provider match, without adding new provenance
columns.

What real, unattended operation over these four pieces still needs to
prove -- a 24-48h soak run, and a genuine cross-provider match verified
via `inspect_data.py cross-provider` -- is explicitly operational, not
something further code changes alone can complete.

**Resolved:** a small follow-up while setting up the first real soak
run. `config.load_dotenv()` loads optional `KEY=value` lines from a
`.env` file at the repo root into `os.environ` (a real env var always
wins, via `os.environ.setdefault`) -- `app.py`/`poller.py`/
`inspect_data.py` call it explicitly before `load_config()`, but
`load_config()` itself deliberately never calls it, so it stays a pure
read of `os.environ` and the existing `monkeypatch.setenv`/`delenv`
test pattern is unaffected by whether a real `.env` happens to exist.
`.env.example` is a new, secret-free committed template. Caught one
real bug while smoke-testing this: reading with plain `encoding="utf-8"`
does not strip a leading BOM, and PowerShell's own `Set-Content
-Encoding utf8` writes one, silently corrupting the first line's key --
fixed by reading with `encoding="utf-8-sig"` instead.

**Resolved:** a thirteenth round, found by external review of the
running system and confirmed against the first real soak run's own
data. `odds_snapshots.collector_run_id` (migration 8) closes the last
gap in the provenance chain -- `collector_runs` already had
`provider_id`/`parser_version`/`source_payload`, but no snapshot pointed
back to which run produced it. No SQL foreign key: `OddsIngestionService
.run()` saves snapshots against its `run_id` before the matching
`collector_runs` row exists (written only at the end, by
`_record_run()`), which a real FK under this project's `PRAGMA
foreign_keys = ON` would reject.

This surfaced a real ordering bug: `find_latest`/`find_last_two`/
`find_latest_for_market` ordered by `observed_at` alone, so a
later-polled but genuinely stale quote from one provider could shadow
an earlier-polled but fresher quote from another reporting the same
canonical bookmaker. All three now order by `quote_time`
(`COALESCE(source_timestamp, observed_at)`), the same concept
`FreshnessPolicy` already used, just never applied to "latest"
selection. Consequently, movement detection could also compare two
readings of the same canonical bookmaker from two *different*
providers as one continuous stream, reporting a "movement" that was
really just two feeds disagreeing -- `OddsRepository.
find_last_two_same_provider()` now requires both compared readings to
share a provider (via `collector_run_id` -> `provider_id`), falling
back to the old cross-provider-tolerant behavior when either reading's
provenance is unknown, so pre-migration-8 data and provenance-free
tests keep behaving exactly as before.

Two smaller fixes from actually running the soak test:
`ApiFootballCollector` now keeps pages already fetched successfully
when a real response has more pages than the free plan allows to fetch
(observed live), logged rather than raised; `load_dotenv()` now treats
a blank `KEY=` line as unset, since `os.environ[key] = ""` was silently
discarding `DB_PATH`'s real default and opening a throwaway temp
database instead. Also, `ODDS_API_KEY` now flows through `AppConfig.
odds_api_key` the same way `API_FOOTBALL_KEY` already did, closing the
config-boundary inconsistency noted a few rounds back.

Decoupling the Analysis Layer from `OddsRepository` (an `OddsReader`
Protocol, or orchestration handing detectors plain snapshot data)
remains deliberately deferred -- the right boundary to draw before this
becomes a generic, non-sports-odds anomaly engine, not before.
