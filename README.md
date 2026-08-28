# Anomaly Detection Engine

## Overview

**Anomaly Detection Engine** is a generic system for detecting mismatches, outliers, rapid changes, and other anomalies across large and heterogeneous datasets.

The project is intentionally designed around a generic data-analysis problem rather than a single domain.

The first concrete use case is **sports odds analysis**, where the system collects odds from multiple bookmakers, normalizes different representations of the same event and market, stores historical observations, validates data quality and temporal validity, and detects anomalies such as:

- best-odds differences
- arbitrage / surebet signals
- outlier odds
- rapid odds movements
- bookmaker lag
- stale or invalid observations

The long-term goal is to keep the ingestion, validation, matching, storage, and anomaly-detection layers generic enough to support other datasets and domains later.

---

## Current MVP Scope

The initial MVP focuses on:

- football
- pre-match data
- 1X2 markets
- multiple bookmakers / data sources
- source-independent collection
- event normalization and matching
- canonical market identity
- data validation
- historical odds storage
- freshness and temporal-coherence checks
- best-odds analysis
- surebet detection
- rapid movement detection

Real APIs and bookmaker scraping will be introduced after the ingestion and validation pipeline is stable.

---

## Architecture

Current target flow:

```text
External Source
      ↓
Collector
      ↓
CollectorRun
      ↓
Raw Payload / RawEventOdds
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
Semantic Validation
      ↓
OddsSnapshot
      ↓
Storage
      ↓
Freshness / Temporal Coherence
      ↓
Analysis Engine
      ↓
Anomaly Classification
```

The core analysis engine does not depend on how data is acquired.

Possible collectors:

```text
Public API
Internal JSON/HTTP endpoint
HTML scraper
Browser automation
JSON/file input
```

All collectors must produce the same internal model before data enters the core pipeline.

---

## Project Structure

```text
anomaly-detection-engine/
├── data/
│   └── samples/
├── docs/
│   ├── architecture.md
│   ├── data-quality-and-integrity
│   └── project-development-pitfalls
├── src/
│   └── anomaly_detection_engine/
│       ├── analysis/
│       │   ├── arbitrage.py
│       │   ├── best_odds.py
│       │   ├── bookmaker_lag.py
│       │   ├── freshness.py
│       │   ├── movement_detector.py
│       │   └── outlier_detector.py
│       ├── collectors/
│       │   ├── base.py
│       │   ├── json_collector.py
│       │   └── the_odds_api_collector.py
│       ├── ingestion/
│       │   └── service.py
│       ├── matching/
│       │   └── event_matcher.py
│       ├── models/
│       │   ├── collector_run.py
│       │   ├── event.py
│       │   ├── market.py
│       │   ├── odds.py
│       │   ├── raw_odds.py
│       │   └── raw_payload.py
│       ├── normalization/
│       │   └── team_normalizer.py
│       ├── observability/
│       │   ├── logging_config.py
│       │   └── metrics.py
│       ├── reporting/
│       │   ├── movement_report.py
│       │   └── opportunity_report.py
│       ├── storage/
│       │   ├── database.py
│       │   ├── collector_run_repository.py
│       │   ├── odds_repository.py
│       │   └── raw_payload_repository.py
│       ├── validation/
│       │   ├── result.py
│       │   └── raw_odds_validator.py
│       └── app.py
├── tests/
├── pyproject.toml
├── requirements.txt
└── README.md
```

---

## Core Domain Concepts

### Event

Canonical sports event identified using domain information such as:

```text
sport
competition
home team
away team
start time
```

External event IDs are source-specific and must not be treated as global IDs.

### MarketIdentity

`MarketIdentity` defines exactly what market is being compared.

Potential fields include:

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

Only semantically equivalent markets may be compared.

### RawEventOdds

Common source-independent representation produced by collectors.

### OddsSnapshot

Represents one observed odd for a specific:

```text
event
bookmaker
market
outcome
observed_at
```

Snapshots are stored historically so the engine can analyze changes through time.

### CollectorRun

Represents one ingestion cycle.

It tracks:

```text
source
started_at
finished_at
status
records_received
records_accepted
records_rejected
collector_version
errors
```

Possible statuses:

```text
SUCCESS
PARTIAL
FAILED
```

### DataValidationResult

Validation returns structured results instead of only `True` or `False`.

It contains:

```text
valid
validation stage
errors
warnings
```

Validation stages include:

```text
STRUCTURAL
SEMANTIC
IDENTITY
TEMPORAL
```

---

## Data Collection

The project defines a generic `OddsCollector` interface.

Each collector transforms source-specific data into the internal `RawEventOdds` contract.

Current implementations:

```text
JsonOddsCollector
TheOddsApiCollector
```

`TheOddsApiCollector` talks to https://the-odds-api.com's `/v4/sports/{sport}/odds`
endpoint (h2h/1X2 markets, decimal odds) and maps bookmaker outcomes onto
`1`/`X`/`2` by matching outcome names against the event's home/away team
names, skipping any bookmaker whose line is missing an outcome. It needs a
subscription API key: pass `api_key=` or set the `ODDS_API_KEY` environment
variable (never hardcode a real key in source or commit it).

Future implementations may include:

```text
MozzartCollector
MaxBetCollector
SoccerCollector
```

A bookmaker-specific collector may internally use an API, an undocumented frontend endpoint, HTML parsing, or browser automation, but the rest of the system remains unchanged.

---

## Validation

Data must pass validation before entering analysis.

### Structural Validation

Examples:

- required fields exist
- timestamps can be parsed
- market exists
- outcomes exist
- odds can be interpreted

### Semantic Validation

Examples:

- decimal odds are greater than 1.0
- home and away teams are not identical
- timestamps are timezone-aware
- values are within reasonable ranges

### Identity Validation

Examples:

- event match is sufficiently reliable
- competition is correct
- home/away orientation is correct
- market semantics match

### Temporal Validation

Examples:

- snapshot is fresh enough
- observations are close enough in time
- timestamp is not unexpectedly in the future

---

## Time Handling

All internal runtime timestamps are:

```text
timezone-aware
UTC-normalized
```

Important time concepts remain separate:

```text
event_start_time
observed_at
source_timestamp
```

`observed_at` represents when our system saw the data.

`source_timestamp` is optional metadata supplied by the source.

---

## Freshness

A valid observation may still be too old for current comparison.

The engine uses a configurable `FreshnessPolicy`, for example:

```text
maximum snapshot age
maximum observation spread
```

Cross-source analysis must not compare stale or temporally incoherent observations.

---

## Best Odds

The best-odds module selects the highest available odd for each outcome among eligible current snapshots.

Example:

```text
            1      X      2

Book A     2.10   3.40   3.20
Book B     1.95   3.75   3.10
Book C     2.00   3.30   3.60

BEST       2.10   3.75   3.60
```

---

## Surebet Detection

For a three-way market:

```text
margin =
    1 / best_1
  + 1 / best_X
  + 1 / best_2
```

If:

```text
margin < 1
```

the engine reports a mathematical arbitrage signal.

This does not automatically mean the opportunity is executable in practice.

---

## Rapid Movement Detection

Historical snapshots allow the engine to detect large movements over a short time window.

Example:

```text
10:00 odds = 2.20
10:04 odds = 1.90
```

The detector evaluates percentage change and elapsed time.

---

## Reporting

`reporting.opportunity_report` turns the analysis modules into one
noise-filtered, at-a-glance table: who (bookmaker), where (event/outcome),
how much (odds and edge %), sorted by edge descending.

Two signal types, each with its own "is this worth a line in the report"
threshold so ordinary bookmaker-margin spread doesn't flood it:

```text
SUREBET     a real arbitrage (calculate_arbitrage.is_surebet), kept only
            if the theoretical profit clears min_surebet_profit_percent
            (default 1.0%). A mathematically real margin of 0.1-0.2% is
            still noise in practice: odds can move before all legs are
            placed, stakes have to be rounded, and bookmakers actively
            limit accounts suspected of arbitrage betting.

VALUE_GAP   one bookmaker pricing an outcome well above the consensus of
            its peers (detect_outliers, favorable direction only -- an
            outlier priced below consensus is a bad price, not an
            opportunity). Default threshold is 15%, matching
            detect_outliers itself, since with only 3-4 bookmakers a
            lower bar just flags routine price shopping.
```

Both thresholds are overridable per call, and `app.py`'s demo reads them
from `MIN_SUREBET_PROFIT_PERCENT` / `MIN_VALUE_GAP_PERCENT` environment
variables so they can be tuned without editing code:

```bash
MIN_SUREBET_PROFIT_PERCENT=0.1 python -m anomaly_detection_engine.app
```

Example output (default thresholds -- the sample dataset's ~0.12% margin
does not clear the 1.0% bar and correctly produces no rows):

```text
No opportunities above threshold.
```

Lowering `MIN_SUREBET_PROFIT_PERCENT` to `0.1` surfaces it:

```text
SIGNAL     EVENT                            OUT  BOOKMAKER          ODDS   EDGE%
--------------------------------------------------------------------------------
SUREBET    Manchester United vs Liverpool   1    Mozzart            2.15   0.12%
SUREBET    Manchester United vs Liverpool   2    Soccer             3.65   0.12%
SUREBET    Manchester United vs Liverpool   X    MaxBet             3.85   0.12%
```

**SUREBET and VALUE_GAP are not the same kind of signal.** SUREBET is
risk-free by construction: hedge all three outcomes across bookmakers and
you profit no matter what happens. VALUE_GAP is a single, directional bet
with real risk -- it just means one bookmaker's price for one outcome
looks better than its peers' right now, which can mean the bookmaker is
slow to update, or it can mean their line is simply wrong (a data-quality
issue, not a real edge). The report doesn't yet distinguish those two
cases; treat a large VALUE_GAP as "worth a manual look", not as instant
free money the way a SUREBET is.

`reporting.movement_report` is a separate report over the same repository
data: it flags outcomes whose odds moved sharply between their **last two
readings** for the same bookmaker (`analysis.movement_detector`, applied
across every event/bookmaker/outcome instead of one pair you'd pick by
hand). Needs at least two ingestion runs to have anything to compare, so
the JSON demo path in `app.py` runs two polls (`odds_sample.json`, then
`odds_sample_poll2.json` -- a second reading a few minutes later with
mostly small moves and one bookmaker's price nearly halved) instead of
one, so this report has something to show on a single `python -m
anomaly_detection_engine.app` run. The live `the-odds-api` source stays
single-poll (a second real call seconds later would double API credit
usage without the market necessarily having moved).

```text
EVENT                            OUT  BOOKMAKER          FROM     TO  CHANGE%  ELAPSED
----------------------------------------------------------------------------------------
Manchester United vs Liverpool   1    Mozzart            2.15   1.08  -49.77%    4m00s
```

Default threshold is 10% within a 24-hour window between the two readings
(much wider than `detect_rapid_movement`'s own 5-minute default, since
this report cares about any sharp move between successive polls, not
specifically a *fast* one).

A full web dashboard is not built yet -- these are text reports over the
same repository data a dashboard would eventually read from.

---

## Storage

The PoC currently uses SQLite.

The repository supports operations such as:

```text
save
find_by_event
find_latest
find_last_two
find_latest_for_market
```

SQLite is appropriate for the current phase, while the storage layer is kept isolated so a future migration to PostgreSQL remains possible.

---

## Observability

`OddsIngestionService` and the collectors log structured JSON (one object
per line) via the standard `logging` module rather than printing directly,
so ingestion activity is machine-parseable:

```text
ingestion.run.started
ingestion.record.rejected   (WARNING, includes the rejection reason)
ingestion.collector.failed  (ERROR)
ingestion.run.completed     (INFO; carries the same counts as CollectorRun)
```

Call `observability.logging_config.configure_logging()` once at process
start to attach a JSON `StreamHandler` to the `anomaly_detection_engine`
logger.

`observability.metrics.IngestionMetrics` is a small in-process accumulator
that a long-lived caller (e.g. a scheduler polling `service.run()`
periodically) can pass into `OddsIngestionService` to track totals across
runs -- accepted/rejected counts, run status counts, and rejection reasons
grouped by validation stage. It has no exporter built in; `snapshot()`
returns a plain dict, which a real deployment would ship to whatever
backend it uses (StatsD, Prometheus, CloudWatch, ...) rather than this
project taking a dependency on one.

---

## Data Quality Principles

The engine must distinguish:

### DATA_QUALITY_ANOMALY

Examples:

```text
invalid odds
bad timestamp
parser regression
missing outcome
wrong market representation
```

### MARKET_ANOMALY

Examples:

```text
outlier odds
rapid movement
bookmaker lag
market divergence
```

### ARBITRAGE_SIGNAL

Example:

```text
surebet candidate
```

### SYSTEM_ANOMALY

Examples:

```text
collector failure
source latency
stale source
rate limiting
```

---

## Current Development Status

```text
[x] Initial project structure
[x] Canonical Event and Team models
[x] Bookmaker and OddsSnapshot models
[x] RawEventOdds model
[x] Collector abstraction
[x] JSON collector
[x] Team normalization
[x] Alias mapping
[x] Fuzzy matching
[x] Event matching
[x] Best-odds calculation
[x] Surebet detection
[x] SQLite storage
[x] Historical snapshot queries
[x] Rapid movement detector
[x] Freshness policy/result model
[x] MarketIdentity
[x] DataValidationResult
[x] Raw odds validation
[x] CollectorRun
[x] Unit tests for core components
[x] Outlier detector
[x] Bookmaker-lag detector
[x] Ingestion/orchestration service (OddsIngestionService)
[x] CollectorRun persistence (CollectorRunRepository)
[x] Raw payload retention (RawPayloadRepository)
[x] Odds snapshot idempotency (dedupe on save)
[x] Freshness check wired into demo analysis
[x] First real external source (TheOddsApiCollector)
[x] Structured JSON logging (observability.logging_config)
[x] In-process ingestion metrics (observability.metrics.IngestionMetrics)
[x] Noise-filtered opportunity report (reporting.opportunity_report)
[x] Odds movement report (reporting.movement_report)
[x] Dirty-data test fixtures
```

---

## Next Development Steps

```text
[x] Update storage schema for MarketIdentity
[x] Persist source_timestamp consistently
[x] Add CollectorRun persistence
[x] Add raw payload storage / traceability
[x] Build ingestion/orchestration service
[x] Connect collector → validation → matching → storage
[x] Run freshness checks before current-market analysis
[x] Add outlier detector
[x] Add bookmaker-lag detector
[x] Add first real external source (TheOddsApiCollector)
[x] Add dirty-data fixtures
[x] Add structured logging and metrics
[x] Add reporting layer (opportunity report; a dashboard/web UI remains open)
```

---

## Next Architectural Milestone

The next major milestone is an orchestration layer that coordinates:

```text
collect
→ validate
→ normalize
→ match
→ create snapshot
→ persist
→ evaluate freshness
→ analyze
→ record run result
```

This keeps `app.py` small and prevents source-specific or orchestration logic from leaking into analysis modules.

---

## Long-Term Vision

The architecture follows a generic pattern:

```text
heterogeneous sources
        ↓
canonical representation
        ↓
validation
        ↓
entity/market matching
        ↓
historical observations
        ↓
anomaly detection
```

Possible future domains include:

- pricing data
- financial market data
- sensor measurements
- inventory data
- monitoring metrics
- distributed-system observations

Sports odds are the first domain used to validate the architecture.

---

## Guiding Principle

> An anomaly is only meaningful if the observations being compared are valid, semantically equivalent, correctly matched, and temporally coherent.

Correctness of input data has priority over the number of detected signals.
