# Reference identity mapping

API-Football is the reference namespace for team and competition identity.
That means its stable ids anchor canonical entities when they exist; it does
not mean every API-Football payload is accepted without validation.

## Entity status

- `REFERENCE`: backed by an API-Football team/competition id.
- `PROVISIONAL`: first seen at another provider and not yet linked to an
  API-Football identity.

`teams.reference_provider_id` and
`competitions.reference_provider_id` store the API-Football id. Other
providers point to the same internal entity through the source mapping tables.

Example:

```text
API-Football / 100 / Sabah          -> team-123 (REFERENCE)
Mozzart      / 900 / Sabah Masazir  -> team-123
```

The provider spelling is audit metadata, not the canonical identity key.

Team display names are deliberately **not unique**. Two API-Football ids may
both legitimately be named `United`; provider id is authoritative and
`team_competitions` records where each identity has actually been observed.
Provider name mappings are scoped to a canonical competition, so a bare name
learned in one league is not a global rule for every country. Competition-name
mappings are similarly scoped by a normalized country key.

## Mapping trust

Every mapping has one of three states:

- `VERIFIED`: safe to use as a fast path.
- `UNVERIFIED`: evidence is not yet strong enough; fuzzy-only mappings stay
  here and are re-resolved on later sightings. Their odds stay on a separate
  provisional entity/event; `UNVERIFIED` is not permission to merge data into
  a reference event.
- `SUSPECT`: a previously verified provider id drifted catastrophically or an
  event invariant failed. It cannot silently overwrite the old identity.

Mappings also retain `resolution_method` and `resolver_version`. Important
methods include `manual`, `provider_event_context`, `exact`, `alias`, and
`fuzzy`.

## Fixture-first resolution and learning aliases

Before resolving team names independently, the resolver searches the already
resolved competition and kickoff window and evaluates home+away as one fixture.
A unique candidate with one exact side can identify the differently-spelled
other side. This is the cold-start `Sabah` / `Sabah Masazir` path: it works on
the first sighting from the second provider, not only after that provider's
event id was already cached.

A verified provider event mapping remains an even faster path on later polls.
If the event id, source team ids, competition id, sport, and country are
compatible, it teaches later provider spelling changes directly.

If those invariants do not match, the event mapping becomes `SUSPECT`; the
existing event is not renamed or rescheduled through that fast path.

## Manual corrections

`FixtureCatalog.verify_team_mapping(...)` and
`FixtureCatalog.verify_competition_mapping(...)` persist a human-approved
provider mapping. Use them for cases such as:

```text
Mozzart / 777 / Westham Untd
    -> API-Football / 48 / West Ham United
```

Manual verification can deliberately replace a `SUSPECT` id mapping. This
keeps corrections in data rather than growing a global alias dictionary that
might affect unrelated countries, competitions, age groups, or teams.

## Missing reference data

Ingestion is not blocked when API-Football does not yet contain an entity. A
non-reference provider may create a `PROVISIONAL` entity, which can be linked
to an API-Football identity later through verified event context or manual
mapping.

## Kickoff authority

`events.start_time_provider` records who owns canonical kickoff time. Once an
API-Football fixture id is attached, only API-Football can move that canonical
time; a secondary provider's differing timestamp is retained in its raw/audit
data but cannot make the event oscillate according to collector order.

## Upgrade conflict handling

Migration 20 removes global team-name uniqueness, contextualizes name mapping
keys, and downgrades legacy fuzzy/ambiguous rows to `UNVERIFIED`. If historical
data already points multiple API-Football ids at one team or competition, those
id mappings become `SUSPECT` and the entity becomes `CONFLICT`; the migration
does not guess how to split historical events without evidence.
