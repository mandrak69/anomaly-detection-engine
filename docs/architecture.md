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

Persists historical observations and future operational metadata.

Current implementation:

```text
SQLite
OddsRepository
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
surebet
freshness
rapid movement
```

Future modules:

```text
outlier detection
bookmaker lag
cross-market anomaly detection
```

---

## Collector Contract

All collectors must ultimately produce:

```text
RawEventOdds
```

The collector may internally use any acquisition method.

Example:

```text
MozzartCollector
    ↓
internal endpoint / HTML / browser
    ↓
RawEventOdds
```

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

Future tables may include:

```text
events
teams
competitions
markets
bookmakers
source_event_mappings
odds_snapshots
raw_payloads
collector_runs
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
DATA_QUALITY_ANOMALY
MARKET_ANOMALY
ARBITRAGE_SIGNAL
SYSTEM_ANOMALY
```

This prevents parser errors and source failures from being misclassified as market opportunities.

---

## Next Architectural Step

Introduce an orchestration / ingestion service.

Suggested responsibility:

```text
Collector
    ↓
validate
    ↓
normalize
    ↓
match
    ↓
snapshot
    ↓
persist
    ↓
freshness
    ↓
analyze
    ↓
CollectorRun result
```

Possible class:

```text
OddsIngestionService
```

or:

```text
OddsAnalysisService
```

The preferred design is to keep ingestion orchestration separate from pure analysis logic.
