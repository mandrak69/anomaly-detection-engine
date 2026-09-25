# System Design & Product Specification

**Project:** Anomaly Detection Engine  
**Status:** Living specification  
**Primary use case:** collecting, reconciling, storing, and analysing sports odds

## 1. Purpose

This is the central product and system specification. It defines what the
system must do, its safety guarantees, and the boundaries that keep it
extensible and maintainable.

Implementation details belong in [architecture.md](architecture.md). Provider
capture instructions and forensic identity decisions remain in their dedicated
documents linked at the end.

Any material change to product behaviour, identity rules, trust policy, or a
major system boundary must update this document or an Architecture Decision
Record (ADR).

## 2. Product vision

The system collects odds from multiple providers, determines when their
observations describe the same real-world event and market, stores price
history, and detects useful anomalies such as arbitrage, price gaps, rapid
movements, lagging bookmakers, and outliers.

The core difficulty is identity. Providers may use:

- different languages, alphabets, transliterations, punctuation, or casing;
- abbreviations and different club suffixes;
- generic league names shared by several countries;
- different names for senior, reserve, women's, and age-group teams;
- different time zones or slightly changed kickoff times;
- missing, unstable, or recycled provider identifiers;
- different market terminology and settlement rules.

The central product requirement is:

> Convert heterogeneous observations into trustworthy canonical identities
> without silently merging different real-world entities.

A missed match can be reviewed later. A false merge contaminates event history
and anomaly results. When evidence is insufficient, the system must fail closed
rather than guess.

## 3. Goals

The system must:

1. support providers fetched automatically through approved APIs or HTTP;
2. support providers captured manually through browser tools or files;
3. send both modes through the same source-independent pipeline;
4. retain raw evidence required to reproduce and audit decisions;
5. reconcile provider teams, leagues, fixtures, markets, and bookmakers into
   stable canonical entities;
6. preserve country, league, gender, reserve-team, and age-group context;
7. use a reference provider where strong IDs exist without assuming it is
   universally correct or complete;
8. store immutable time-series odds observations;
9. reject invalid records with explicit, machine-readable reasons;
10. expose uncertain or suspicious mappings for human review;
11. make adding a provider primarily an adapter and configuration task;
12. allow detectors and reporting channels to evolve independently;
13. expose enough logs, metrics, and run metadata to diagnose quality issues.

## 4. Non-goals

The project does not aim to:

- bypass authentication, bot protection, rate limits, or provider terms;
- infer definite identity from name similarity alone;
- guarantee complete worldwide provider coverage;
- automate wagering;
- treat the reference provider as unquestionably correct;
- hide uncertainty by silently dropping or force-matching records;
- build a full administration UI before the review lifecycle is stable;
- optimise prematurely for internet-scale traffic.

## 5. Core principles

### Acquisition is separate from interpretation

API, browser capture, and file import are acquisition choices. After parsing,
all sources produce the same core model and use the same validation, identity,
storage, and analysis stages.

### Raw evidence is immutable

Original payloads and collection metadata are audit evidence. Parsers and
canonical models may evolve, but the original input must remain replayable
according to the retention policy.

### Identity is not a display string

A team, competition, event, or market has a stable internal ID. Names are
attributes and aliases, not database identity.

### Context outranks textual similarity

Matching considers the whole fixture: sport, country, competition, both teams,
kickoff time, gender, reserve/senior status, age group, provider identifiers,
and market semantics.

### False merges are worse than duplicates

Ambiguous cases remain separate or enter quarantine. They are never
force-matched merely to reduce duplicate counts.

### Every mapping has provenance

Record which provider value was mapped, to what canonical entity, by which
method, at what time, with which evidence and trust state.

### Detection consumes comparable data

Anomaly analysis cannot compare observations until event and market identity
are compatible and freshness requirements are satisfied.

## 6. High-level flow

```text
Provider
  -> acquisition (automatic or manual)
  -> raw payload + CollectorRun
  -> provider parser/adapter
  -> RawEventOdds
  -> structural validation
  -> normalization
  -> canonical identity resolution
  -> semantic validation
  -> OddsSnapshot
  -> historical storage
  -> freshness/comparability filters
  -> detection
  -> signals, reports, and future notifications
```

Provider-specific exceptions must not leak into later stages to compensate for
an incorrect earlier boundary.

## 7. Provider and acquisition model

### Provider identity versus collector identity

A **provider** is the real data owner or bookmaker, for example
`api-football`, `meridianbet`, or `mozzart`. A **collector** is one configured
way to acquire data from that provider.

Automatic and manual collectors for the same provider share identity mappings:

```text
provider_id = the-odds-api
source = the-odds-api-auto:soccer_epl

provider_id = the-odds-api
source = the-odds-api-manual:soccer_epl
```

Mapping caches are keyed by `provider_id`, never by the collector label.

### Automatic acquisition

Automatic collectors may use documented APIs, approved HTTP/JSON endpoints, or
scheduled file feeds. They implement timeouts, bounded retries, rate-limit
behaviour, and return the exact response for audit retention. Credentials stay
in runtime configuration and are never committed or logged.

### Manual acquisition

A manual collector reads a known drop file or import bundle captured by a
human. It does not bypass access controls. Successfully consumed input is
archived with provider, capture time, checksum, and parser version.

Manual mode is first-class: it uses the same parser contract and downstream
pipeline as automatic mode. Details belong in
[manual-capture-sources.md](manual-capture-sources.md).

### Collector contract

Every collector supplies:

- `provider_id` and a unique source label;
- collector and parser versions;
- collection timestamp;
- exact payload or an explicit no-data result;
- parsed `RawEventOdds` records.

Wrong-shaped input is a failure, not a successful run with zero records.

### CollectorRun

Every attempt records start/end time, provider, mode, versions, accepted and
rejected counts, error details, and raw-payload storage status. Status is
`SUCCESS`, `PARTIAL`, or `FAILED`. One bad record must not erase run history or
prevent valid sibling records from being processed.

## 8. Source-independent observation model

A provider adapter converts raw input into `RawEventOdds`, containing when
available:

- provider event ID;
- sport and country/region;
- raw league name and provider league ID;
- raw home/away names and provider team IDs;
- kickoff timestamp and source offset;
- bookmaker;
- market, period, phase, line, rules, and specifier;
- outcome and decimal odds;
- provider update and capture timestamps;
- raw-record reference.

Provider response classes stop at this adapter boundary.

## 9. Validation

Structural validation rejects malformed timestamps, missing required fields,
non-positive/non-finite odds, invalid outcome structures, unsupported markets,
decoding failures, and impossible fixture shapes.

After identity resolution, semantic validation checks that:

- participant identities do not conflict;
- sport, country, and league context are compatible;
- women, reserve, youth, and senior qualifiers agree;
- event time is plausible;
- market semantics match;
- live and pre-match prices are not mixed;
- provider IDs do not contradict a verified identity snapshot.

Every rejection records the stage and a machine-readable reason.

## 10. Text normalization

Normalization makes strings comparable; it does not decide identity. Safe
operations include Unicode normalisation, case folding, whitespace and
punctuation cleanup, controlled transliteration, and reviewed token aliases.

Meaningful qualifiers must survive normalization:

- women/female markers;
- reserve/B/II/2 markers;
- U19/U21/U23 and language equivalents;
- division, tier, and group numbers;
- country/region qualifiers.

The original string is always retained alongside its comparison form.

## 11. Canonical identity and trust

Canonical entities include Sport, Country/Region, Competition, Team,
Event/Fixture, Bookmaker, MarketIdentity, Outcome, and OddsSnapshot. Persistent
entities use stable internal IDs.

Provider mappings store provider ID, source entity ID when present, raw and
normalized names, context, canonical target, resolution method, trust state,
timestamps, and the identity snapshot used for compatibility checks.

Trust states include:

- `PROVISIONAL`: newly created identity with limited evidence;
- `UNVERIFIED`: inherited or learned mapping not strongly confirmed;
- `VERIFIED`: compatible mapping confirmed by strong IDs, curated alias, or a
  human decision;
- `SUSPECT`: later evidence conflicts with a trusted mapping;
- `REJECTED` or retired where audit history requires it -- **not yet
  implemented**: no code path sets or reads it today, and the CLI in
  §18/§21 has no verb to produce it. Naming it here is intentional (the
  identity lifecycle needs a terminal "a human looked at this and it was
  wrong" state distinct from `SUSPECT`'s "unresolved, needs a look"), not
  a claim that it exists yet.

`SUSPECT` mappings never silently return to the fast path. Operators must be
able to list, inspect, verify, retarget, or reject them with an audit reason.

API-Football currently acts as the reference namespace where it has reliable
coverage. Its IDs are strong evidence, not absolute truth. Uncovered fixtures
can still receive provisional identities, and recycled or contradictory IDs
become `SUSPECT`. See
[reference-identity-mapping.md](reference-identity-mapping.md).

## 12. Matching policy

### Evidence order

Resolvers prefer evidence in this order:

1. compatible verified provider-ID mapping;
2. compatible manually verified mapping;
3. curated alias in the required context;
4. exact normalized name in context;
5. globally unique exact/alias candidate without conflict;
6. safe fuzzy candidate with a clear margin and matching qualifiers;
7. corroborating whole-fixture context;
8. a provisional entity or quarantine.

### Team matching

Consider sport, provider identity, country, competition context, semantic
qualifiers, curated aliases, and candidate scores. A team in a new competition
may reuse a globally unique exact or curated-alias match. Fuzzy similarity alone
does not permit global cross-competition reuse.

### Competition matching

Competition identity includes sport and country/region. Generic names such as
`Premier League`, `Championship`, `Cup`, and `Super League` are not globally
unique.

- With country present, resolve inside that context.
- Without country, reuse globally only when exactly one compatible candidate
  exists.
- Never choose between same-named competitions by dictionary or query order.
- Never discard country/region supplied by a provider.
- Tier, group, gender, and age markers are identity-bearing.

See
[league-identity-mapping-decisions.md](league-identity-mapping-decisions.md).

### Event matching

A canonical event requires sport, canonical competition, canonical home and
away participants, controlled kickoff tolerance, and compatible semantic
qualifiers. Team pair plus approximate time is insufficient without league
context. Home/away reversal requires an explicit, tested sport/provider rule.

### Market matching

Prices are comparable only when `MarketIdentity` agrees on market type, period,
pre-match/live phase, line, rules, specifier, and outcome semantics. Display
names do not define equality.

### Ambiguity behaviour

When candidates are too close, context conflicts, or evidence is incomplete:

- do not attach odds to an existing trusted event;
- create a provisional identity only when separation is safe;
- otherwise quarantine/reject the observation;
- store candidates, scores, evidence, and reason;
- expose it to the review workflow.

Resolution must be deterministic for the same database state and input.

## 13. Storage and history

The current implementation uses SQLite behind repository/catalog boundaries;
PostgreSQL remains a future option.

Store collector runs, payloads/checksums, canonical entities, mapping history,
events, immutable odds snapshots, validation failures, signals, and movements.

Snapshots are append-only. Deduplication may suppress an exact retry but must
not overwrite price history. Timestamps are stored in UTC; useful original
offsets and provider timestamps remain available for audit.

Schema changes are versioned and tested against empty and representative
production copies. Identity migrations require before/after counts, evidence,
and a rollback plan.

## 14. Detection and reporting

Detection operates only on canonical, valid, fresh, and semantically comparable
snapshots. Source coverage is explicit: a missing bookmaker is not the same as
agreement with the market.

Detection answers what is true. Reporting decides whether the result is worth
showing or notifying. This keeps presentation thresholds out of mathematical
detection.

## 15. Extensibility

### Adding a provider

A new provider requires configuration, a collector/import adapter, a parser to
`RawEventOdds`, fixtures and contract tests, market/outcome mappings, confirmed
aliases with evidence, replay tests, and operational documentation. It should
not require changes to detectors or canonical repositories.

### Adding a sport

Sport-specific participant, event, period, market, and timing behaviour belongs
in explicit strategies/policies, not scattered `if football` branches.

### Adding a detector

A detector consumes canonical snapshots through a stable interface and emits a
structured candidate with evidence. It never fetches, parses, or resolves
identity.

### Aliases and configuration

Configuration is validated on startup and separated by domain. Aliases are
reviewable and tested. As static maps grow, move them toward persistent,
auditable administrative records rather than embedding exceptions throughout
matching code.

## 16. Maintainability rules

- Provider code stays in collectors/parsers/adapters.
- Core models do not import provider response types.
- All acquisition modes use one identity implementation.
- Unsupported modes fail loudly.
- Parser failure never becomes zero records.
- Every alias requires evidence and a regression test.
- Fuzzy-threshold changes require corpus/replay testing.
- Production data repair uses reviewed, scripted migrations.
- Fixing a matching rule is two changes, not one: the code change stops
  the mistake for *future* sightings, but every mapping/event a sighting
  already produced under the old, buggier rule stays wrong until a
  separate, scripted data-repair pass finds and corrects it. Observed
  live more than once (a fuzzy scorer change whose old version had
  already merged reserve/women teams into their senior team; a
  collector fix for a provider silently omitting country, added after
  it had already collapsed several different countries' same-named
  leagues into one bucket) -- treat "the rule is fixed" and "the data
  the old rule already wrote is fixed" as two separate, both-required
  steps of the same change, not one.
- Comments explain invariants; incident narratives belong in decision logs.
- Migrations, parsers, matching, and detectors remain independently testable.

## 17. Testing strategy

Unit tests cover parsers, normalization, semantic qualifiers, validation,
freshness, scoring, and detection mathematics.

Contract tests prove every collector/parser returns the shared model and fails
loudly on incorrect response shapes.

Identity regression tests include:

- aliases across competitions;
- identical league names in different countries;
- missing country with several candidate leagues;
- senior/reserve, men/women, and U19/U21/U23 separation;
- number-bearing league tiers/groups;
- multilingual and transliterated names;
- provider-ID recycling or contradiction;
- league and cup meetings between the same teams;
- kickoff differences and timezone offsets.

Replay tests run retained payloads through parser and matcher changes and
compare accepted/rejected counts, canonical entity counts, trust states, and
anomaly output.

Migration tests run on empty and production-like databases and compare teams,
competitions, events, snapshots, provisional entities, suspect mappings, and
resolution methods.

End-to-end tests cover at least one automatic and one manual source from
collection through audit storage, matching, snapshot storage, and detection.

## 18. Observability and operations

Expose collection duration/status, last success, payload age, record rejection
reasons, payload audit failures, new canonical identities, resolution methods,
trust states, ambiguous/suspect mappings, parser versions, provider coverage,
snapshot freshness, and anomaly counts.

The minimum human identity lifecycle is:

```text
list unresolved/suspect mappings
  -> inspect evidence and candidates
  -> verify, retarget, or reject
  -> record actor, time, and reason
  -> safely reprocess affected observations
```

`scripts/identity_mapping.py` implements the first three steps for
`team`/`competition` mappings (`suspects` to list, `team`/`competition` to
verify, each recorded with actor-less but timestamped provenance) -- see §21
for what it's still missing (a `reject` verb, and "safely reprocess" staying a
manual replay rather than a supported reprocessing operation).

Alerts distinguish provider outage, parser breakage, identity degradation, and
analysis failure.

## 19. Security and compliance

- Secrets come from environment or a secret manager.
- Tokens are redacted from logs and retained payloads.
- Manual capture never bypasses access controls.
- Provider terms, limits, and retention obligations are documented.
- Imported files are untrusted and have size/format limits.
- Operational writes are auditable.

## 20. Failure behaviour

| Scenario | Required behaviour |
|---|---|
| Provider unavailable | Fail explicitly; preserve last-success time |
| Rate limit exhausted | Use bounded policy; never spin or silently switch mode |
| Manual file absent | Explicit no-data result |
| Wrong manual capture | Named parser/shape failure |
| One invalid record | Reject it and continue the run |
| Raw payload retention fails | Run cannot report full success |
| Ambiguous identity | Separate or quarantine; never arbitrary selection |
| Verified ID conflicts with context | Mark mapping suspect; bypass fast path |
| Duplicate retry | Idempotent ingestion without losing history |
| Concurrent collectors | Transactional identity creation without duplicate race |
| Migration fails | Stop deployment and preserve previous database |
| Detector fails | Stored observations remain intact and replayable |

## 21. Roadmap

### Recently completed

- CLI for listing and verifying (team/competition) `SUSPECT`/`UNVERIFIED`
  mappings (`scripts/identity_mapping.py`) — `suspects`/`team`/`competition`
  only; no `reject` verb yet (see below);
- countryless-competition ambiguity guard: a name shared by several
  countries is never chosen between by dictionary/query order, and is
  logged distinctly when it happens (`fixture_catalog.competition.
  ambiguous_without_country`);
- one full real-data replay (live api-football + retained Meridianbet/
  Mozzart captures against a migrated database copy, before/after counts
  diffed) run once, by hand, ahead of the reference-identity/trust-state
  migration — see "Next priorities" below for turning this into a
  reusable, scripted harness instead of a one-off.

### Next priorities

1. a `reject`/retire verb for `scripts/identity_mapping.py` — trust
   state `REJECTED` is named in §11 but has no write path anywhere yet;
   the only supported human actions today are verify (team/competition)
   and read-only `suspects`;
2. turn the one-off replay above into a reusable, scripted harness
   (`scripts/replay_against_copy.py` or similar) that any future
   matching-rule change re-runs against, not just the identity/trust
   migration;
3. garbage-collect orphaned provisional entities: a team/competition
   created under `ambiguous`/`fuzzy` resolution can become referenced by
   zero events after a later merge/split (observed live while splitting
   Atletico Madrid's reserve team back out) and currently needs a
   by-hand check-then-delete, not a supported operation;
4. provider health/freshness reporting;
5. auditable persistent aliases;
6. scheduled polling with overlap, retry, and back-pressure controls;
7. retention and backup policy.

### Later

- PostgreSQL for larger/concurrent deployments;
- identity-review UI;
- notifications;
- richer market and sport coverage;
- learned match suggestions behind deterministic guards and human review;
- generic anomaly-detection interfaces beyond sports odds.

## 22. Open questions

- Which provider is authoritative by sport, country, and entity type?
- What promotes a provisional mapping without human review?
- When are corrected historical observations replayed automatically?
- What are the payload retention period and archive format?
- Which kickoff tolerances apply by sport/provider?
- How are postponed, abandoned, rescheduled, and neutral-venue events modeled?
- How do non-team participants extend the participant model?
- Which operational metrics require alerts?
- When does SQLite stop meeting concurrency and volume needs?

Open questions do not grant implicit implementation permission. Resolve them
through an ADR or an update to this specification.

## 23. Related documents

- [Architecture](architecture.md)
- [Manual capture sources](manual-capture-sources.md)
- [Reference identity mapping](reference-identity-mapping.md)
- [Team identity decisions](team-identity-mapping-decisions.md)
- [League identity decisions](league-identity-mapping-decisions.md)
- [League names by source](league-names-by-source.md)

## 24. Definition of done for behaviour changes

A collection, identity, storage, or detection change is complete only when:

- expected and failure behaviours are explicit;
- normal and ambiguous cases are tested;
- raw evidence remains auditable;
- migration and rollback implications are documented;
- relevant logs/metrics exist;
- this specification, architecture document, or an ADR is updated;
- representative replay shows no unexplained fragmentation or false merge.
