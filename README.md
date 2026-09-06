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
│       │   ├── movement_detection.py
│       │   ├── movement_detector.py
│       │   ├── opportunity_detection.py
│       │   └── outlier_detector.py
│       ├── collectors/
│       │   ├── base.py
│       │   ├── json_collector.py
│       │   ├── manual_capture_collector.py
│       │   ├── mozzart_file_collector.py
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
│       │   ├── fixture_catalog.py
│       │   ├── migrations.py
│       │   ├── movement_repository.py
│       │   ├── odds_repository.py
│       │   ├── raw_payload_repository.py
│       │   ├── signal_repository.py
│       │   └── time_utils.py
│       ├── validation/
│       │   ├── result.py
│       │   └── raw_odds_validator.py
│       ├── app.py
│       ├── config.py
│       ├── pipeline.py
│       └── runtime.py
├── tests/
├── scripts/
│   └── watch_capture.py
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

Only semantically equivalent markets may be compared -- enforced end to
end: every repository query that groups or deduplicates odds (the
`odds_snapshots` unique index, `OddsRepository.find_latest`/
`find_last_two`/`find_latest_for_market`, and the `signals`/`movements`
identity used by `SignalRepository`/`MovementRepository`) filters/keys on
the full `MarketIdentity` tuple, not just `market_type`/`period`/`line`.
Two snapshots that only differ in `rules`/`specifier` are a different
market and must never be merged together.

### RawEventOdds

Common source-independent representation produced by collectors. Includes
an optional `source_id` -- a source's own stable per-bookmaker identifier
(e.g. the-odds-api's `"bet365"`), distinct from `source` (a display name
like `"Bet365"` that can legitimately change without the underlying
bookmaker changing). `OddsIngestionService` uses `source_id` for
`Bookmaker.id` when a collector provides one, falling back to a
normalized form of `source` otherwise (see Data Collection below).

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

`quote_time` (a property, not a stored column) is `source_timestamp` if
the source provided one, otherwise `observed_at` -- see Freshness below
for why this distinction matters and where it's actually used.

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

`OddsIngestionService.run()` guarantees a `CollectorRun` is always
recorded, even if an individual record's processing raises unexpectedly
(a validator/matcher/`FixtureCatalog`/`OddsRepository` bug or transient
error, not just an ordinary rejection). `_ingest_one` catches per-record
failures and reports them as a rejected record
(`processing-error: <ExceptionType>: <message>`) rather than letting
them abort the whole run -- losing the `CollectorRun` here would hide
exactly the kind of failure observability most needs to see.

Status reflects more than just record counts:

```text
records_rejected > 0 (but some accepted)   PARTIAL
records_rejected > 0 (none accepted)       FAILED
raw-payload audit write failed for any
record, even if every record was
otherwise accepted                         PARTIAL
the run itself aborted mid-iteration
(an exception genuinely escaped the loop,
not just one record's own processing)      PARTIAL if anything was
                                            accepted first, else FAILED
                                            (error_type/error_message set)
neither of the above                       SUCCESS
```

Raw payload retention is part of this system's auditability, not a
nicety -- a run that silently lost part of its audit trail must not
report the same `SUCCESS` it would if nothing had gone wrong. Likewise,
an aborted run is never `SUCCESS` even if every record processed before
the abort happened to succeed: the loop not finishing is itself the
failure being reported.

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
TheOddsApiCollector          + TheOddsApiManualCollector
MozzartFileCollector
ManualCaptureCollector        generic drop-file + archive base the two
                              manual collectors above are built on
```

`TheOddsApiCollector` talks to https://the-odds-api.com's `/v4/sports/{sport}/odds`
endpoint (h2h/1X2 markets, decimal odds) and maps bookmaker outcomes onto
`1`/`X`/`2` by matching outcome names against the event's home/away team
names, skipping any bookmaker whose line is missing an outcome. It needs a
subscription API key: pass `api_key=` or set the `ODDS_API_KEY` environment
variable (never hardcode a real key in source or commit it).

Each parsed bookmaker also carries the-odds-api's own stable `"key"`
(e.g. `"bet365"`) as `RawEventOdds.source_id`, separate from `source`
(the display name, e.g. `"Bet365"`). `OddsIngestionService` uses
`source_id` for `Bookmaker.id` when a collector provides one, instead of
deriving an id from the display name (`raw.source.lower()`) -- a display
name can change (`"Bet365"` -> `"Bet365 UK"`) without the underlying
bookmaker changing, which would otherwise make ingestion treat it as a
brand-new, unrelated bookmaker. Collectors with no such stable identifier
(the JSON demo, Mozzart) leave `source_id` unset and keep the old
derived-from-name behavior.

Which primary source `pipeline.build_collectors()` uses is chosen by
`ODDS_SOURCE`, resolved once in `config.load_config()` and validated
eagerly: `"demo"` (the default) or `"the-odds-api"`, anything else raises
`ValueError` immediately at startup. A typo here
(`ODDS_SOURCE=the-odds-ap1`) must not silently fall back to demo data --
that is exactly the kind of misconfiguration that would go unnoticed
once this runs unattended, the same reasoning `ODDS_API_MODE`/
`MOZZART_MODE` below already followed.

`AppConfig` (`config.py`, built by `load_config()`) is every
environment-variable decision this app makes, resolved exactly once at
startup -- including `ODDS_API_MODE`/`ODDS_API_CAPTURE_DIR`/
`MOZZART_CAPTURE_DIR`/`MOZZART_MODE`, not just `ODDS_SOURCE`/`DB_PATH`/
the threshold env vars. `pipeline._the_odds_api_collector()`/
`_mozzart_collector()` read these off the `AppConfig` they're passed
rather than calling `os.environ.get(...)` themselves, so nothing
downstream of `load_config()` reads `os.environ` directly -- one place
to look for what an env var actually resolves to, and one place a future
config source (a file, a secrets manager) would need to change.

### Manual capture: mode is an explicit flag, not an inferred default

`ManualCaptureCollector` is the shared mechanism behind every
manually-captured source: it fetches nothing itself, just watches a fixed
drop file, hands its contents to a source-specific `parse` function, and
archives it. Two collectors are built on it:

```text
MozzartFileCollector          drop file: <capture_dir>/live.json
TheOddsApiManualCollector      drop file: <capture_dir>/capture.json
```

`MozzartFileCollector` exists because mozzartbet.com sits behind
Cloudflare bot-management (`cf_clearance`/`__cf_bm` cookies observed on
the captured request) -- an automated fetch would mean scripting around
that protection, which this project won't do regardless of technical
feasibility. It has no automatic mode.

`TheOddsApiManualCollector` exists for a different reason: **even a
source with a perfectly good API can need a manual fallback sometimes**
-- a rate limit, an outage, an exhausted quota. It parses the identical
response shape as the live `TheOddsApiCollector` (same function,
`parse_the_odds_api_response`), just acquired from a file you saved by
hand instead of a live HTTP call.

For both, the capture step is manual: in your own browser, open DevTools
-> Network, find the relevant request, save its response body to the
drop file, overwriting the same filename each time you capture a new
reading. Each `collect()` call:

```text
no file waiting     -> returns [] (not an error, just nothing new yet)
file present         -> parses it, then moves it into
                         <capture_dir>/history/ under a timestamped +
                         unique-suffixed name (drop slot freed, raw
                         capture kept for traceability/replay)
parse failure or       -> raises (surfaces as a FAILED CollectorRun) and
wrong response shape      leaves the file in place instead of archiving
                          a capture that couldn't be read
```

"Wrong response shape" is deliberate, not just malformed JSON: both
parsers check the top-level structure they expect (a `dict` with an
`"items"` key for Mozzart, a `list` for the-odds-api.com) and raise
(`MozzartResponseError`, `TheOddsApiError`) if it doesn't match, instead
of silently treating an unrecognized shape as "zero matches this cycle".
That distinction matters: the wrong bookmaker's capture landing in this
collector's drop directory, or an API error body (`{"message": "Invalid
API key"}`) saved where a real response was expected, would otherwise
look identical to a legitimate quiet moment in the logs and
`CollectorRun` -- stopping loudly beats silently ingesting nothing while
believing everything is fine.

**Mode is a visible, explicit flag per source**, not something inferred
from which env vars happen to be set -- a misconfigured mode fails
loudly in `app.py` rather than silently doing the wrong thing:

```text
ODDS_API_MODE=auto|manual      default "auto" (TheOddsApiCollector).
                                "manual" needs ODDS_API_CAPTURE_DIR and
                                uses TheOddsApiManualCollector instead.
MOZZART_MODE=manual            the only value that exists today --
                                Mozzart has no automatic mode yet, but
                                the flag is explicit anyway rather than
                                silently assumed.
```

```bash
# Live API, normal case
ODDS_SOURCE=the-odds-api ODDS_API_KEY=<key> python -m anomaly_detection_engine.app

# Live API forced into manual mode (e.g. rate-limited right now)
ODDS_SOURCE=the-odds-api ODDS_API_MODE=manual ODDS_API_CAPTURE_DIR=./odds-api-capture \
    python -m anomaly_detection_engine.app

# Mozzart as a supplemental source alongside whichever primary is active
MOZZART_CAPTURE_DIR=./mozzart python -m anomaly_detection_engine.app
```

Mozzart (and `TheOddsApiManualCollector` in manual mode) run as
**supplemental** sources alongside whichever primary source is active --
collected in the same cycle and matched against the same `FixtureCatalog`
(see Matching below) as everyone else, so their odds are compared against
everyone else's (best odds, opportunity report, movement report all see
them). Adding another manual-capture source later (MaxBet, Soccer, ...)
is the same shape: its own `parse` function, wrapped in
`ManualCaptureCollector`, its own mode env var, appended in `app.py`'s
`_supplemental_collectors()` -- no other wiring changes.

**What actually binds a drop directory to a bookmaker** is the env var
you point at it (`MOZZART_CAPTURE_DIR=./whatever`), not the directory's
name -- call it `mozzart`, `banana`, doesn't matter. What *does* matter:
the file inside it must be named exactly what that collector expects
(`live.json` for Mozzart, `capture.json` for `TheOddsApiManualCollector`,
both configurable via `filename=` if you construct one directly), and
nothing checks that the *content* actually matches the bookmaker the
directory is "for" -- dropping the wrong file in the wrong place either
raises (a differently-shaped response, per the parse-failure row above)
or, worse, parses under the wrong label if the shapes happen to overlap.
One directory per bookmaker, by convention you maintain yourself.

Because matching goes through `FixtureCatalog`, Mozzart and another
active source reporting the *same* real match under differently-spelled
team names ("Manchester United" vs a hypothetical "Man Utd" from another
source) resolve to **one** shared canonical event once that spelling has
been seen once (exact/alias/fuzzy match, cached permanently) -- verified
during development: a Mozzart capture reporting the demo's "Manchester
United vs Liverpool" merged into the same event as the JSON demo data,
and the opportunity report found a real cross-source surebet combining a
Mozzart leg with a JSON-demo-bookmaker leg.

### Watching a capture directory automatically

`scripts/watch_capture.py` polls a capture directory (default every 5s)
and runs the app itself as soon as a new drop file appears, instead of
you having to re-run it by hand after every capture. Source-agnostic --
it just takes a directory/filename to watch and which env var to expose
that directory as:

```bash
python scripts/watch_capture.py --dir ./mozzart --env MOZZART_CAPTURE_DIR
```

It polls rather than using a filesystem-events library: a human dropping
a file every few minutes at most doesn't need sub-second reaction time,
and polling avoids a new dependency for what is otherwise a
zero-dependency project. Any other env vars the app needs (`ODDS_SOURCE`,
thresholds, ...) should already be set in the shell this runs in, same as
running the app directly.

Future implementations may include:

```text
MaxBetCollector
SoccerCollector
```

A bookmaker-specific collector may internally use an API, an undocumented frontend endpoint, HTML parsing, or browser automation -- but automating around active bot-detection specifically (solving/bypassing challenges, replaying short-lived session tokens) is out of scope for any of them. The rest of the system remains unchanged regardless of which mechanism a given collector uses.

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

## Matching

Two implementations, both producing an `EventMatchResult(event, confidence, reason)`
so `OddsIngestionService` can use either interchangeably:

```text
EventMatcher      fixed, in-memory candidate list -- rejects anything
                  it doesn't already know (no-event-match,
                  team-normalization-failed, ambiguous-event)
FixtureCatalog     persistent, auto-growing -- resolves a never-seen
                   team or event by creating a canonical row instead of
                   rejecting it
```

`app.py`'s demo uses `FixtureCatalog` for every source. It reuses
`TeamNormalizer` internally (same exact/alias/fuzzy resolution as
`EventMatcher`), but runs it against the *durable* `teams` table instead
of a list passed in for one run, and persists every resolution in
`source_team_mappings` (keyed by `(source, sport, raw_name)`) so it's
never re-solved later -- that cache is what lets two different sources
reporting the same real match under different team-name spellings end up
sharing one canonical event, once that particular spelling has been
resolved once, whether just now (exact/alias/fuzzy match against the
existing catalog) or replayed from a previous run's mapping.

Team resolution, per raw name:

```text
1. known mapping for (source, sport, raw_name)? -> use it, done
2. exact/alias/fuzzy match (>= fuzzy_threshold, default 85%) against
   existing teams for this sport? -> use that team
3. otherwise -> create a new canonical team from the raw name
```

A fuzzy score just under the threshold creates a new team rather than
merging into an existing one -- deliberately conservative: a harmless
near-duplicate team is better than silently merging two different real
teams because the threshold was set too loose. The same conservatism
applies when the top two fuzzy candidates score within
`fuzzy_ambiguity_margin` (default 5 points) of each other -- e.g. "Man
United" scoring ~85.5 against both "Manchester United" and "Manchester
United U21" is a near-tie, not a clear winner, so it also creates a new
team rather than guessing between two plausible candidates.

Event resolution reuses the same start-time-tolerance idea as
`EventMatcher` (default 30 minutes): given the two resolved teams *and
the same league*, an existing event for that exact team pair within the
tolerance window is reused; otherwise a new canonical event is created.
League is part of an event's identity, not just descriptive metadata --
the same two teams can play each other in more than one competition
(league + cup, or two age groups) within the tolerance window.
`start_time` is normalized to UTC (`storage.time_utils.to_utc_iso`, the
same helper `OddsRepository` uses) before being stored or compared, so
two sources reporting the same real kickoff under different but equally
valid offsets (`+00:00` vs `+02:00`) still resolve to the same canonical
event instead of being compared as differently-offset text.

The "league" going into that comparison is itself canonicalized first,
the same exact/alias/fuzzy pattern as team names: `FixtureCatalog`
resolves a raw league/competition string (the-odds-api's `"Premier
League"`, another source's `"England Premier League"`, Mozzart's
`"Engleska Premier Liga"`) against a `competitions` table, caching every
`(source, sport, raw league name)` resolution in
`source_competition_mappings` -- without this, the same real match
reported under different league spellings by different sources would
resolve to two different canonical events that never get compared
against each other, defeating the entire cross-source point of league
being part of an event's identity in the first place. `Event.league`
stays a plain string (the canonical name), not a separate object --
`competitions`/`source_competition_mappings` exist purely as this
resolution's persistent memory, the same role `teams`/
`source_team_mappings` play for team names. A second, independent
`league_aliases` constructor param (distinct from `aliases`, which is
team-name-only) lets a caller seed known league-name variants.

`EventMatcher` still exists and is still tested -- it is the right tool
when you genuinely want a fixed, non-growing candidate list (e.g. a
controlled test), not a mistake left behind by the switch to
`FixtureCatalog`.

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

UTC normalization goes through one shared helper,
`storage.time_utils.to_utc_iso()` (`.astimezone(timezone.utc).isoformat()`),
called at every point a timestamp is written to text: `OddsRepository.
save()`/`save_all()` for `observed_at`/`source_timestamp`, and
`FixtureCatalog` for `Event.start_time`. Without this, two equally-valid
but differently-offset timestamps (`+00:00` vs `+02:00` for the same
instant) would sort/compare incorrectly against each other as plain
text -- a source is free to report whatever offset it wants; storage
always normalizes it.

---

## Freshness

A valid observation may still be too old for current comparison.

The engine uses a configurable `FreshnessPolicy`, for example:

```text
maximum snapshot age
maximum observation spread
```

Cross-source analysis must not compare stale or temporally incoherent observations.

`validate_freshness` measures age against an explicit `analysis_time`
parameter, required everywhere it's used (`detect_surebet_candidates`,
`detect_value_gap_candidates`, `build_opportunity_report`) -- callers
must pass their own real notion of "now" (`datetime.now(timezone.utc)`
in production). It must never be derived from the snapshots' own
timestamps (e.g. their newest `observed_at`): a batch where every
snapshot is old but mutually close together would then look "fresh"
relative to itself no matter how much real time has actually passed.
`pipeline.py`'s demo path is the one legitimate exception -- its fixed
calendar timestamps would otherwise always register as ancient, so it
explicitly passes the newest observation across everything ingested that
run as its stand-in "now" (`_demo_analysis_time`), kept local to that
demo-only code path rather than being a default inside the detection
functions themselves.

Age and spread are measured against each snapshot's `quote_time`
(`source_timestamp` if the source provided one, else `observed_at`), not
`observed_at` directly. `observed_at` is only "when our poll happened",
which can look arbitrarily fresh even for a price the source itself
computed or cached well before our request landed --
`the-odds-api`'s per-bookmaker `last_update` fetched by a poll a full two
hours later is a two-hour-old quote regardless of how quickly the poll
itself completed. A source that timestamps its own prices is telling us
something age-relevant that a poll timestamp alone can't; a source with
no such timestamp (the JSON demo, Mozzart) falls back to `observed_at`,
the same behavior as before `quote_time` existed.

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

`build_opportunity_report` requires a `freshness_policy: FreshnessPolicy`
argument (no default -- there's no one sensible window across
deployments) and skips any event that fails `validate_freshness` before
computing anything: this report compares odds *across bookmakers at a
point in time*, which only means something if those odds were actually
simultaneously valid. Without this, an event shared by a fast-moving
source and a source with old/fixed timestamps could produce a SUREBET or
VALUE_GAP built from odds that were never really available together.

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

The PoC currently uses SQLite, and (since this was previously `:memory:`
every run) now persists to a real file by default:
`data/anomaly_detection.db`, overridable via `DB_PATH` (`DB_PATH=:memory:`
still works, for the old throwaway-per-run behavior). The connection uses
a 30-second busy timeout, because `scripts/watch_capture.py` makes
concurrent writers to this same file a real possibility now: two watcher
processes (e.g. Mozzart and a manual `the-odds-api` capture) can each
spawn `app.py` at close to the same moment, and SQLite allows only one
writer at a time -- the timeout makes the second one wait instead of
immediately failing with "database is locked".

`storage.database.configure_connection()` applies every per-connection
setting a connection needs, called by every code path that opens one
(`create_connection()`, and every test helper): row access by column
name, and `PRAGMA foreign_keys = ON`. SQLite does not enable foreign-key
enforcement by default on a new connection even though the schema
declares `REFERENCES` -- without this, an orphaned `events` row
(pointing at a deleted/nonexistent team) could be inserted silently.

### Schema migrations

`data/anomaly_detection.db` is a real persistent file, so
`initialize_database()` can no longer just be a big `CREATE TABLE IF NOT
EXISTS` script and call it done -- that only ever adds new tables/indexes
under new names, it does not change an *existing* table that already has
the old shape. `storage.migrations` tracks schema version via `PRAGMA
user_version` (built into every SQLite file for exactly this) and a
`MIGRATIONS` list of functions, each one applied at most once per
database:

```text
migrate(connection):
    current = PRAGMA user_version
    for version, migration in enumerate(MIGRATIONS, start=1):
        if version <= current: continue
        migration(connection)
        PRAGMA user_version = version
```

`initialize_database()` calls `migrate()` unconditionally on every
startup -- a brand-new database runs every migration; an already
up-to-date one runs none; an older persistent file left over from a
previous version of this schema (e.g. `signals`/`movements` from before
they had `market_rules`/`market_specifier` columns, or before the
`competitions`/`source_competition_mappings` tables existed) is upgraded
in place, preserving whatever it already had. Once a migration has
shipped, its SQL must never be edited -- a database that already
recorded it as applied will never run it again, so a later fix has to be
its own new migration. "Just delete the file" remains fine for a
scratch/test database, but is no longer the architectural answer to a
schema change.

Each migration is idempotent, not wrapped in a transaction with its
`PRAGMA user_version` bump -- verified directly that Python's sqlite3
module does not roll back DDL (`CREATE`/`ALTER`/`DROP`) or `PRAGMA`
statements the way it does plain DML, even inside `with connection:` (a
`CREATE TABLE` survives an exception raised right after it in the same
`with` block, unlike an `INSERT` in the same position). True
cross-statement atomicity for this would need Python 3.12's
`autocommit=False`, which this project can't rely on
(`requires-python >=3.11`). So instead every migration is safe to re-run:
`_add_column_if_missing()` only `ALTER`s a column that isn't already
there (a bare second `ALTER TABLE ADD COLUMN` raises "duplicate column
name"), and every index rebuild `DROP`s (`IF EXISTS`) immediately before
recreating it -- a retry after a crash between any two statements,
including one between a migration finishing and its `PRAGMA
user_version` write landing, converges to the same end state rather than
erroring.

`OddsRepository` supports operations such as:

```text
save
save_all
find_by_event
find_latest
find_last_two
find_latest_for_market
```

`save_all` persists every outcome of a single raw ingested record (a
market's several outcomes) in one transaction, so a failure partway
through never leaves a half-written market snapshot the way calling
`save()` once per outcome could -- `OddsIngestionService` uses it for
exactly this reason. Both `save`/`save_all` use `with self._connection:`
(not a manual `commit()`), which is what makes this a real guarantee:
Python's sqlite3 module commits on success and rolls back everything
executed so far if any iteration raises, rather than committing whatever
happened to succeed before the failure.

`find_latest_for_market` selects the single latest snapshot per
(bookmaker, outcome) using a `ROW_NUMBER() OVER (PARTITION BY ...  ORDER
BY observed_at DESC, id DESC)` window function, not a join between
separate `MAX(observed_at)`/`MAX(id)` subqueries -- the two maxima are
not guaranteed to come from the same row (a later-arriving snapshot can
carry an *older* `observed_at` than one already stored), so that join
could silently return no row at all for a given (bookmaker, outcome),
dropping it out of every downstream detection.

`FixtureCatalog` (see Matching above) owns three more tables --
`teams`, `events`, `source_team_mappings` -- the persistent canonical
registry that replaced the old in-memory fixed/self-bootstrapped event
lists.

### Persisting derived signals, not just raw odds

Reporting (`opportunity_report`/`movement_report`, above) used to be the
only consumer of the analysis modules -- compute, print, discard. Two new
repositories persist what gets *detected*, independent of whether/how
it's ever displayed:

```text
SignalRepository     SUREBET / VALUE_GAP -- conditions that can persist
                      across multiple poll cycles
MovementRepository    odds movements -- point-in-time events, already
                      over the moment they're detected
```

The two need different lifecycles, which is why they're separate tables
with separate repository shapes rather than one generic "detections"
table:

```text
signals table      status ACTIVE/RESOLVED, first_seen_at, last_seen_at,
                    resolved_at. SignalRepository.reconcile(signal_type,
                    candidates, observed_at=..., evaluated_keys=...)
                    upserts every currently-detected candidate of that
                    type (new -> insert ACTIVE; still there -> update
                    last_seen_at/edge/details; previously RESOLVED ->
                    reactivate, keeping the original first_seen_at) and
                    marks an ACTIVE signal as RESOLVED *only if* its own
                    (event, market, outcome) identity is in
                    evaluated_keys and it wasn't seen this sweep.
                    Identity excludes which bookmaker/odds are currently
                    involved -- those are updated in place, not part of
                    what makes two detections "the same" opportunity.
                    The whole reconcile() call is one transaction (rolls
                    back entirely if anything in it raises).

movements table     append-only, no status. MovementRepository.save()
                    per detected transition, deduped on the full
                    (event, bookmaker, market, outcome, both
                    observed_at timestamps) via a unique index -- the
                    same idempotency approach OddsRepository.save uses.
```

`evaluated_keys` (a set of `SignalIdentity(event_id, market, outcome)`)
is required, not defaulted, because getting it wrong means resolving
something that shouldn't be. It replaced an earlier, coarser
`(market, evaluated_event_ids)` pair of parameters -- a real
`SignalIdentity` already carries its own market, so scoping by market is
now just one more thing a key encodes rather than a separate check, and
every candidate's own identity is required to already be a member of
`evaluated_keys` (`reconcile()` raises otherwise: a real candidate can
only exist for something that was, by definition, evaluated). Two
distinct problems this closes:

- **Market scoping.** A sweep over THREE_WAY must never resolve an
  ACTIVE signal for a market it never analyzed (e.g. TOTALS), even
  though both would otherwise match on `(signal_type, event_id)` alone.
- **Per-outcome granularity for VALUE_GAP.** An event passing freshness
  overall doesn't mean every one of its outcomes had enough bookmakers
  to evaluate -- `detect_outliers` itself skips any outcome with fewer
  than `min_bookmakers` snapshots. `evaluated_keys` reflects that
  per-outcome, not just per-event: outcome `"1"` can be genuinely
  evaluated (and resolved if it disappears) while a sibling outcome
  `"X"` for the very same event, with only one bookmaker quoting it,
  stays untouched. Getting this wrong (resolving at event granularity)
  would have silently resolved `"X"`'s signal just because the *event*
  looked fine, even though `"X"` itself was never actually checked.

Either way, "couldn't tell this sweep" is not the same claim as
"confirmed gone", and conflating them would be actively dangerous ahead
of any real alerting on top of this table.

Detection itself lives in `analysis.opportunity_detection`
(`detect_surebet_candidates`, `detect_value_gap_candidates`) and
`analysis.movement_detection` (`detect_movements`) -- the same functions
`opportunity_report`/`movement_report` call to build their display rows,
now shared with `pipeline.py`'s persistence sweep
(`persist_detected_signals`) so detection logic exists in exactly one
place regardless of what eventually consumes the result.
`detect_surebet_candidates`/`detect_value_gap_candidates` return a
`SurebetDetectionSweep`/`ValueGapDetectionSweep` (`.candidates` plus
`.evaluated_keys`, see above); `opportunity_report` only needs
`.candidates` (it recomputes fresh every call, so there's nothing to
reconcile), while `persist_detected_signals` needs both. Persisted
SUREBET candidates carry no minimum-profit threshold (that's a
reporting-only "worth telling a human" decision,
`min_surebet_profit_percent`); VALUE_GAP persists at the same threshold
as the report, since that one is part of the outlier detection itself,
not a presentation-layer filter.

Verified end-to-end (not just unit-tested): a real surebet gets
persisted as `ACTIVE`; once the underlying odds move enough to kill the
arbitrage, the *same* signal row (not a new one) is marked `RESOLVED`,
and the price move that killed it is independently recorded in
`movements`.

There is deliberately no presentation layer reading these tables yet
(dashboard, notifications, dedup-aware alerting) -- see Reporting above
and the Next Development Steps below. This is the "where/how to keep the
data" half of that question, kept separate from "how to show it" on
purpose.

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
[x] Manual-capture collector for a bot-protected source (MozzartFileCollector)
[x] Structured JSON logging (observability.logging_config)
[x] In-process ingestion metrics (observability.metrics.IngestionMetrics)
[x] Noise-filtered opportunity report (reporting.opportunity_report)
[x] Odds movement report (reporting.movement_report)
[x] Dirty-data test fixtures
[x] Persistent event/fixtures catalog (FixtureCatalog)
[x] Manual-capture sources wired in as supplemental collectors
[x] Generalized manual-capture mechanism (ManualCaptureCollector) + explicit per-source mode flags (ODDS_API_MODE, MOZZART_MODE)
[x] Directory watcher for manual captures (scripts/watch_capture.py)
[x] Fail loud on wrong-shaped manual captures (MozzartResponseError, TheOddsApiError)
[x] Persistent runtime database (DB_PATH)
[x] Persisted derived signals (SignalRepository, MovementRepository) -- decoupled from reporting
[x] MarketIdentity enforced everywhere (dedupe index, latest/last-two/latest-for-market queries, signal/movement identity) instead of just type/period/line
[x] Fixed find_latest_for_market's MAX(observed_at)/MAX(id) join bug with a ROW_NUMBER() window query
[x] Explicit, required analysis_time for freshness checks (real wall-clock time in production; no longer silently derivable from the snapshots' own timestamps)
[x] Transactional per-record ingestion (OddsRepository.save_all) + guaranteed CollectorRun finalization even if a record's processing raises unexpectedly
[x] FixtureCatalog hardening: fuzzy-match ambiguity margin, league as part of event identity
[x] app.py split into AppConfig / build_runtime / run_ingestion / run_analysis
[x] ODDS_SOURCE fail-fast (explicit "demo"/"the-odds-api", no silent fallback on a typo)
[x] Schema migrations (storage.migrations, PRAGMA user_version) -- a persistent DB no longer needs "delete the file" to pick up a schema change
[x] save/save_all and SignalRepository.reconcile() are real transactions (with self._connection:, roll back entirely on failure, not just a commit() at the end)
[x] Signal resolution scoped to (signal_type, evaluated_keys) -- stale/missing data can no longer cause a false RESOLVED
[x] PRAGMA foreign_keys = ON on every connection (storage.database.configure_connection)
[x] FixtureCatalog.start_time normalized to UTC, same as OddsSnapshot's timestamps
[x] AppConfig covers every env var the app reads (ODDS_API_MODE, capture dirs, MOZZART_MODE included) -- nothing downstream reads os.environ directly
[x] Freshness measured against quote_time (source_timestamp when the source provides one) instead of observed_at
[x] Per-outcome evaluated_keys (SignalIdentity: event + market + outcome) for VALUE_GAP, not just per-event
[x] Canonical competition/league registry (FixtureCatalog + competitions/source_competition_mappings), same pattern as team resolution
[x] Stable bookmaker ids (RawEventOdds.source_id) instead of deriving Bookmaker.id from a display name that can change
[x] Migration idempotency fix: DDL/PRAGMA aren't rolled back by Python's sqlite3, so migrations are made safe to re-run instead of wrapped in a transaction that wouldn't actually protect them
[x] Ingestion failure taxonomy: audit_failures (raw-payload write failures) and an aborted-run's own status/error_type/error_message are now reflected in CollectorRun, not just per-record rejection counts
[x] app.py split into config.py (AppConfig/load_config) / runtime.py (Runtime/build_runtime) / pipeline.py (build_collectors/run_ingestion/run_analysis/persist_detected_signals) / app.py (main() only)
[x] ruff added to the dev/CI pipeline; .idea/ untracked (was committed despite being commented out in .gitignore)
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
[x] Add odds-movement report (reporting.movement_report)
[x] Add a manual-capture collector for a source that can't be fetched automatically (MozzartFileCollector)
[x] Wire manual-capture sources into app.py's demo as supplemental collectors (MOZZART_CAPTURE_DIR)
[x] Persistent event/fixtures catalog (FixtureCatalog: teams, events, source_team_mappings)
[x] Wire freshness checks into build_opportunity_report (required freshness_policy parameter)
[x] Persistent runtime database (DB_PATH, defaults to a real file instead of :memory:)
[x] Persist derived signals, not just raw odds (SignalRepository, MovementRepository)
```

Everything above is done. Genuinely open next:

```text
[ ] Web dashboard / notification layer reading from signals+movements
    (SignalRepository/MovementRepository exist and are populated every
    run now -- nothing presents from them yet besides the existing
    text reports, which still recompute rather than reading back)
[ ] Source-specific validation rules
[ ] Database growth / retention policy for high-frequency polling
    (now doubly relevant: odds_snapshots, signals, and movements can
    all grow indefinitely)
[ ] Market lifecycle states (OPEN/SUSPENDED/CLOSED)
[ ] Decouple analysis from OddsRepository (an OddsReader Protocol, or
    orchestration reads snapshots and hands pure data to the detectors)
    -- worth doing before this becomes a generic (non-sports-odds)
    anomaly engine, not before
[ ] Pyright (or mypy) in CI alongside ruff, now that both are wired in
```

---

## Next Architectural Milestone

**Done:** `OddsIngestionService` coordinates collect → validate → match →
persist → record `CollectorRun`, keeping that orchestration logic out of
the analysis modules. Freshness/analyze/report stay outside it, run by
the caller (`app.py`) against the repository's stored snapshots.

**Done:** `FixtureCatalog` (see Matching above) replaced the in-memory
fixed/self-bootstrapped event lists (`build_demo_events()` and
`build_events_from_raw()`, both removed) with a persistent `teams` /
`events` / `source_team_mappings` registry. Every source now resolves
against the same durable catalog instead of a per-run list, and a team
name resolved once (by exact/alias/fuzzy match) is remembered
permanently -- which is what lets two different sources report the same
real match under different spellings and still land on one shared
canonical event, verified end-to-end with a Mozzart capture merging into
the JSON demo's "Manchester United vs Liverpool" and producing a real
cross-source surebet in the opportunity report.

**Done:** `build_opportunity_report` now takes a required
`freshness_policy: FreshnessPolicy` parameter and skips any event that
fails `validate_freshness` before computing best odds / arbitrage /
outliers -- required rather than defaulted, since there's no one
sensible freshness window across deployments (it depends on real
polling frequency). Confirmed fixed against the exact case that
surfaced it: re-running the Mozzart-merges-into-the-JSON-demo-fixture
scenario above now correctly reports "No opportunities above
threshold" instead of the cross-source SUREBET/VALUE_GAP rows it
produced before this. `build_movement_report` did not need the same
change -- it always compares a bookmaker against its own earlier
reading (never across bookmakers at a point in time), and its own
`max_window` parameter already bounds how far apart those two readings
can be.

**Done:** a round of correctness fixes ahead of adding more bookmakers/
markets, so growth doesn't compound existing bugs. `MarketIdentity` is
now honored end to end (every dedupe/lookup key includes `rules`/
`specifier`, not just `type`/`period`/`line`) across `odds_snapshots`,
`signals`, and `movements`. `find_latest_for_market`'s
`MAX(observed_at)`/`MAX(id)` join -- which could silently drop a
(bookmaker, outcome) out of the result entirely when a later-arriving
snapshot reported an *older* `observed_at` than one already stored -- is
now a `ROW_NUMBER()` window query, always returning a real row.
`analysis_time` is now an explicit, required parameter everywhere
freshness is checked, instead of being derived from `max(observed_at)`
within the batch itself -- that computation made a batch of uniformly
old-but-mutually-close snapshots look "fresh" relative to itself no
matter how much real time had passed, which is precisely the kind of
stale signal freshness exists to catch. `OddsIngestionService` now
saves one raw record's outcomes in a single transaction
(`OddsRepository.save_all`) and guarantees a `CollectorRun` is always
recorded even if a record's processing raises unexpectedly.
`FixtureCatalog` no longer merges a fuzzy match when the top two
candidates are nearly tied (ambiguity margin), and treats league as part
of an event's identity rather than only team pair + kickoff time.
`app.py` was split into `AppConfig`/`build_runtime`/`run_ingestion`/
`run_analysis` (still no framework/DI container), and `ODDS_SOURCE` now
fails fast on an unrecognized value instead of silently falling back to
demo data.

**Done:** a second round of correctness fixes, focused on the persistent
database itself rather than the analysis it stores. `data/*.db` is a
real file now, so `initialize_database()` needed a real migration path
(`storage.migrations`, `PRAGMA user_version`) instead of `CREATE TABLE IF
NOT EXISTS` alone -- that only helps a brand-new database; an existing
one with the old `signals`/`movements` shape (from before they had
`market_rules`/`market_specifier`) would otherwise stay on the old shape
forever, or hit `no such column` once code assumed the new one.
`OddsRepository.save`/`save_all` and `SignalRepository.reconcile()` now
use `with self._connection:` instead of a trailing `commit()`, making
them real transactions: an exception partway through rolls back
everything already executed, rather than leaving whatever had already
run committed. `reconcile()` also gained two required parameters,
`market` and `evaluated_event_ids` -- without them, a sweep that simply
couldn't evaluate an event (stale/missing data) would resolve its
signal exactly the same way a sweep that genuinely found nothing would,
which is a real difference ahead of any alerting built on this table;
and a sweep over one market could resolve an ACTIVE signal for a
completely different market it never analyzed. `configure_connection()`
now turns `PRAGMA foreign_keys = ON` on every connection (off by default
in SQLite even though the schema declares `REFERENCES`), and
`FixtureCatalog.start_time` is normalized to UTC the same way
`OddsSnapshot` timestamps are. `AppConfig` was extended to cover
`ODDS_API_MODE`/capture dirs/`MOZZART_MODE`, so `_the_odds_api_collector`/
`_mozzart_collector` no longer read `os.environ` directly, matching what
`AppConfig`'s own docstring already claimed.

**Done:** a third round, closing gaps the previous one's own fixes
surfaced. Freshness compared `analysis_time` against `observed_at` (when
*we* polled), not `quote_time` (`source_timestamp` if the source
provided one) -- a bookmaker's price computed hours earlier but fetched
by a fast poll looked fresh under the old comparison; `OddsSnapshot.
quote_time` and `validate_freshness` now use the source's own timestamp
when available. `reconcile()`'s `(market, evaluated_event_ids)` pair (see
the previous entry) was still event-granularity, too coarse for
VALUE_GAP: an event passing freshness doesn't mean every outcome had
enough bookmakers to evaluate. Both parameters were replaced by a single
`evaluated_keys: Collection[SignalIdentity]` (`event_id` + `market` +
`outcome`, the same triple a signal's own identity already uses) --
strictly more precise, since a `SignalIdentity` already encodes its own
market, and every candidate is now required to itself be a member of
`evaluated_keys`. League/competition names went through the same
canonicalization team names already had: `FixtureCatalog` now resolves a
raw league string against a `competitions`/`source_competition_mappings`
registry (migration 3), closing a gap the previous round's own
league-scoped event matching had opened -- two sources spelling the same
competition differently would otherwise resolve to two canonical events
that could never be compared. `Bookmaker.id` now prefers a source's own
stable identifier (`RawEventOdds.source_id`, e.g. the-odds-api's `"key"`)
over one derived from its display name, which can legitimately change.
Migration 2's own docstring claim of "wrapped in one transaction" turned
out to be false for DDL/PRAGMA under Python's sqlite3 (verified
directly) -- migrations are now idempotent instead, the actually
achievable guarantee. `OddsIngestionService.run()`'s status now reflects
raw-payload audit failures and the run's own possible mid-loop abort, not
just per-record accept/reject counts. Finally, `app.py` (working out to
446 lines) was split into `config.py`/`runtime.py`/`pipeline.py`, leaving
`app.py` as just `main()`.

Decoupling the analysis layer from `OddsRepository` (an `OddsReader`
Protocol, or orchestration handing detectors plain data) remains
deliberately deferred -- worth doing before this becomes a generic,
non-sports-odds anomaly engine, not before.

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
