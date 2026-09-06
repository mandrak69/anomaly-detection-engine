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

Two implementations of the same `match(...) -> EventMatchResult`
contract:

```text
EventMatcher      fixed, in-memory candidate list -- rejects anything
                  it doesn't already know
FixtureCatalog     persistent (teams/events/source_team_mappings
                   tables) -- resolves a never-seen team or event by
                   creating a canonical row instead of rejecting it,
                   and remembers every (source, sport, raw name)
                   resolution permanently
```

`FixtureCatalog` reuses `TeamNormalizer` (the same exact/alias/fuzzy
logic `EventMatcher` uses) internally, run against the durable `teams`
table instead of a candidate list scoped to one run. The permanent
mapping cache is what lets two different sources report the same real
match under different team-name spellings and still resolve to one
shared canonical event -- once a spelling has been resolved, it never
needs to be re-solved. `app.py` uses `FixtureCatalog` for every source;
`EventMatcher` remains available (and tested) for callers that
genuinely want a fixed, non-growing candidate list.

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
OddsRepository
CollectorRunRepository
RawPayloadRepository
SignalRepository      stateful SUREBET/VALUE_GAP signals
MovementRepository    append-only price-change transitions
```

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

All collectors must ultimately produce:

```text
RawEventOdds
```

The collector may internally use any acquisition method:

```text
JsonOddsCollector           local file, source-independent demo format
TheOddsApiCollector          documented public API, HTTP GET
ManualCaptureCollector        generic: watches a fixed drop file, hands
                              its contents to an injected parse
                              function, archives it -- fetches nothing
                              itself
  MozzartFileCollector          parse_mozzart_response
  TheOddsApiManualCollector     parse_the_odds_api_response (the same
                                 function TheOddsApiCollector's HTTP
                                 path uses)
```

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

Which mode a source runs in is an explicit flag in `app.py`
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
```

Statuses:

```text
SUCCESS
PARTIAL
FAILED
```

This gives the system source-coverage and ingestion-quality context.

---

## MarketIdentity

Market comparison is based on semantic equivalence, not display names.

Potential identity:

```text
market_type
period
line
rules
specifier
```

Examples:

```text
THREE_WAY + FULL_TIME
TOTALS + FULL_TIME + 2.5
HANDICAP + FULL_TIME + -1.5
```

Observations with different identities must not be compared.

---

## Validation Stages

### STRUCTURAL

Checks shape and parseability.

### SEMANTIC

Checks whether values make domain sense.

### IDENTITY

Checks event and market resolution.

### TEMPORAL

Checks freshness and temporal coherence.

The analysis layer should receive only observations that passed all required stages for the requested analysis.

---

## Time Model

Internal runtime timestamps are UTC and timezone-aware.

Important concepts:

```text
event_start_time
observed_at
source_timestamp
analysis_time
```

`analysis_time` should be created once per analysis execution and passed through the relevant processing steps.

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

Current tables:

```text
odds_snapshots           unique-indexed on (event, bookmaker, market,
                          outcome, observed_at); re-saving an identical
                          snapshot is a no-op rather than a duplicate row
collector_runs
raw_payloads              every ingested RawEventOdds, accepted or
                          rejected, with its rejection reason, linked to
                          its CollectorRun
teams                     canonical team registry, unique per
                          (canonical_name, sport)
events                    canonical event registry: sport, league,
                          home_team_id, away_team_id, start_time
source_team_mappings      (source, sport, raw team name) -> team_id,
                          the permanent memory behind FixtureCatalog's
                          cross-source matching
signals                   stateful (SUREBET/VALUE_GAP): status ACTIVE/
                          RESOLVED, first_seen_at/last_seen_at/
                          resolved_at, unique-indexed on
                          (signal_type, event, market, outcome) so a
                          detection is upserted (reconciled) rather than
                          duplicated across poll cycles
movements                 append-only point-in-time transitions,
                          unique-indexed on the full transition so a
                          re-run detection sweep can't duplicate one
```

Competitions/leagues and bookmakers are still plain strings (`league` on
`events`, `bookmaker_name` on `odds_snapshots`) rather than their own
normalized tables -- a smaller, separate step from the event/team catalog
above. Future tables may include:

```text
competitions
bookmakers
```

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

A web dashboard / notification layer reading from `SignalRepository` /
`MovementRepository` (see Reporting Layer) remains the next open item --
the persisted tables exist but have no presentation layer of their own
yet; only the text reports recompute candidates on demand.
