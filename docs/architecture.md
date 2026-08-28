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
Anomaly Classification
      ↓
Reporting
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

### Storage Layer

Persists historical observations and operational metadata.

Current implementation:

```text
SQLite
OddsRepository
CollectorRunRepository
RawPayloadRepository
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

### Reporting Layer

Consumes the Analysis Layer's output and applies an "is this worth a
line in the report" threshold on top of it -- the Analysis Layer answers
whether a condition holds (e.g. `is_surebet`, `detected`), the Reporting
Layer decides whether it clears the bar to be worth a human's attention.
This separation matters because the two questions have different
answers for the same data: a mathematically real 0.05% surebet is still
`is_surebet=True`, but noise for reporting purposes.

Current reports:

```text
opportunity_report   SUREBET + VALUE_GAP, sorted by edge, largest first
movement_report       significant change between an outcome's last two
                       readings, independent of how far apart they were
```

Future:

```text
web dashboard (currently text reports over the same repository data)
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
JsonOddsCollector        local file, source-independent demo format
TheOddsApiCollector       documented public API, HTTP GET
MozzartFileCollector      manually-captured response read from a fixed
                          drop file, archived after each read -- no
                          fetching of its own
```

`MozzartFileCollector` exists specifically because not every source can
be fetched automatically. mozzartbet.com sits behind Cloudflare
bot-management (`cf_clearance`/`__cf_bm` cookies observed on their
`/live/matches` request); scripting around that would mean bypassing
active bot-detection, which this project does not do regardless of
technical feasibility. The acquisition step for such a source stays
manual (a human-driven browser session saves the response to disk); only
the parsing/mapping step is automated. This is a legitimate, permanent
collector shape for sources that cannot or should not be fetched
programmatically -- not a workaround to be replaced later.

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

Current storage is optimized for PoC simplicity.

Current tables:

```text
odds_snapshots    unique-indexed on (event, bookmaker, market, outcome,
                   observed_at); re-saving an identical snapshot is a
                   no-op rather than a duplicate row
collector_runs
raw_payloads       every ingested RawEventOdds, accepted or rejected,
                   with its rejection reason, linked to its CollectorRun
```

Events, teams, competitions, markets, and bookmakers are still canonical
Python objects held in memory (`build_demo_events()` / matcher-supplied),
not normalized tables -- there is no persistent event catalog yet. Future
tables may include:

```text
events
teams
competitions
markets
bookmakers
source_event_mappings
anomalies
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

The next open architectural step is a **persistent event/fixtures
catalog**. `build_events_from_raw()` (the live `the-odds-api` demo path)
and `build_demo_events()` (the fixed sample-data path) both construct
canonical `Event` objects in memory at process start -- there is still
no `events` table, so nothing survives a restart and nothing can be
resolved against fixtures that were not already known when the process
began. `MozzartFileCollector` is not wired into `app.py`'s demo at all
yet; a caller using it standalone has to supply its own `EventMatcher`
and canonical events the same way, which is the same gap, not a
different one.

A web dashboard (see Reporting Layer) is a separate, smaller-scoped open
item -- the reports it would serve already exist as text.
