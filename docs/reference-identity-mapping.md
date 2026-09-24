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

## Mapping trust

Every mapping has one of three states:

- `VERIFIED`: safe to use as a fast path.
- `UNVERIFIED`: evidence is not yet strong enough; fuzzy-only mappings stay
  here and are re-resolved on later sightings.
- `SUSPECT`: a previously verified provider id drifted catastrophically or an
  event invariant failed. It cannot silently overwrite the old identity.

Mappings also retain `resolution_method` and `resolver_version`. Important
methods include `manual`, `provider_event_context`, `exact`, `alias`, and
`fuzzy`.

## Learning aliases from a fixture

A verified provider event mapping is stronger evidence than an isolated team
name. If the event id, source team ids, competition id, sport, and country are
compatible, the event teaches the team and competition mappings directly.
This is how a stable spelling difference such as `Sabah` / `Sabah Masazir`
becomes a permanent verified mapping without adding a global normalization
hack.

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
