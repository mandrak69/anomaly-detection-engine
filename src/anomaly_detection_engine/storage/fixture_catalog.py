import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from sqlite3 import Connection, Row
from uuid import uuid4

from anomaly_detection_engine.matching.event_matcher import EventMatchResult
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.normalization.team_normalizer import (
    TeamNormalizer,
    names_are_similar,
)
from anomaly_detection_engine.storage.time_utils import to_utc_iso

logger = logging.getLogger(__name__)

REFERENCE_PROVIDER_ID = "api-football"
RESOLVER_VERSION = 3
TRUST_VERIFIED = "VERIFIED"
TRUST_UNVERIFIED = "UNVERIFIED"
TRUST_SUSPECT = "SUSPECT"

_COUNTRY_ALIASES = {
    "engleska": "England",
    "skotska": "Scotland",
    "škotska": "Scotland",
    "vels": "Wales",
    "italija": "Italy",
    "spanija": "Spain",
    "španija": "Spain",
    "nemacka": "Germany",
    "nemačka": "Germany",
    "francuska": "France",
    "holandija": "Netherlands",
    "portugalija": "Portugal",
    "grcka": "Greece",
    "grčka": "Greece",
    "turska": "Turkey",
    "srbija": "Serbia",
    "hrvatska": "Croatia",
    "madjarska": "Hungary",
    "mađarska": "Hungary",
    "ceska": "Czech Republic",
    "češka": "Czech Republic",
    "sad": "USA",
    "sjedinjene americke drzave": "USA",
    "sjedinjene američke države": "USA",
    "juzna koreja": "South Korea",
    "južna koreja": "South Korea",
    "severna irska": "Northern Ireland",
}


def _canonical_country(country: str | None) -> str | None:
    if country is None or not country.strip():
        return None
    cleaned = " ".join(country.split())
    return _COUNTRY_ALIASES.get(cleaned.casefold(), cleaned)


def _country_key(country: str | None) -> str:
    canonical = _canonical_country(country)
    return canonical.casefold() if canonical is not None else ""


@dataclass(frozen=True)
class _TeamResolution:
    team: Team
    confidence: float
    method: str
    trust_state: str


@dataclass(frozen=True)
class _CompetitionResolution:
    canonical_name: str
    competition_id: str
    confidence: float
    method: str
    trust_state: str


@dataclass(frozen=True)
class _EventMapping:
    event: Event
    row: Row


class FixtureCatalog:
    """Persistent, auto-growing canonical event/team/competition registry.

    Unlike EventMatcher (a fixed, in-memory candidate list that rejects
    anything it doesn't already know), this resolves-or-creates: a team,
    competition, or event it hasn't seen before gets a new canonical row
    instead of being rejected. Every (source, sport, raw team name)
    resolution is recorded in source_team_mappings (and every (source,
    sport, raw league name) resolution in source_competition_mappings)
    with an explicit trust state and resolver version. Only VERIFIED
    mappings are fast paths; fuzzy-only links stay UNVERIFIED and
    catastrophic provider-id drift becomes SUSPECT. That registry is what
    lets two different sources reporting the same
    real match under different team-name spellings ("Man Utd" vs
    "Manchester United") or league names ("Premier League" vs "England
    Premier League") end up sharing one canonical Event, once that
    particular spelling has been resolved once (by exact/alias match,
    joint fixture context, a manually verified mapping, or
    a mapping learned from an already-verified event).

    Implements the same match(...) -> EventMatchResult shape as
    EventMatcher, so OddsIngestionService can use either interchangeably
    -- construct one FixtureCatalog per collector, but keyed by
    provider_id (the underlying data *provider*, e.g. "the-odds-api"),
    not the collector's own OddsCollector.source (e.g.
    "the-odds-api-manual:soccer_epl", which encodes the *acquisition
    method*/sport key too). Two collectors for the same real provider --
    auto vs. manual capture, or two demo JSON polls -- must share one
    provider_id so team/competition mappings resolved by one are
    immediately reused by the other, instead of each acquisition method
    silently building its own separate, redundant mapping cache for the
    same underlying data source.

    (The `source_team_mappings`/`source_competition_mappings`/
    `source_event_mappings` columns are still named `source` at the SQL
    level -- an internal storage detail kept as-is to avoid a schema
    migration for a rename with no external consumer; every
    Python-facing name here is provider_id.)

    Safe for multiple concurrent watch_capture.py-spawned processes to
    share one database file: match() wraps its whole read-then-maybe-
    create resolution (team, competition, event) in a single BEGIN
    IMMEDIATE transaction, so a second process attempting the same
    resolution blocks on SQLite's writer lock instead of racing through
    the same "does this already exist" check and creating a duplicate.

    Trade-off: a raw name supported only by fuzzy similarity becomes a
    provisional canonical team rather than being merged into an existing
    one. Fuzzy scores can rank whole-fixture candidates, but only unique
    competition/time/opponent context may turn that evidence into a
    verified mapping. A harmless near-duplicate is better than silently
    merging two different real teams or senior/reserve squads.
    """

    def __init__(
        self,
        connection: Connection,
        *,
        provider_id: str,
        aliases: dict[str, str] | None = None,
        token_aliases: dict[str, str] | None = None,
        league_aliases: dict[str, str] | None = None,
        fuzzy_threshold: float = 85.0,
        fuzzy_ambiguity_margin: float = 5.0,
        start_time_tolerance: timedelta = timedelta(minutes=30),
    ) -> None:
        self._connection = connection
        self._provider_id = provider_id
        self._aliases = aliases or {}
        self._token_aliases = token_aliases or {}
        self._league_aliases = league_aliases or {}
        self._fuzzy_threshold = fuzzy_threshold
        self._fuzzy_ambiguity_margin = fuzzy_ambiguity_margin
        self._start_time_tolerance = start_time_tolerance

    def match(
        self,
        *,
        sport: str,
        league: str,
        home_team_raw: str,
        away_team_raw: str,
        start_time: datetime,
        source_event_id: str | None = None,
        home_team_source_id: str | None = None,
        away_team_source_id: str | None = None,
        competition_source_id: str | None = None,
        country: str | None = None,
    ) -> EventMatchResult:
        home_raw = home_team_raw.strip()
        away_raw = away_team_raw.strip()
        canonical_country = _canonical_country(country)

        if not home_raw or not away_raw:
            return EventMatchResult(None, 0.0, "missing-team-name")
        if home_raw == away_raw:
            return EventMatchResult(None, 0.0, "home-equals-away")

        # BEGIN IMMEDIATE acquires SQLite's single writer lock up front,
        # before any of the read-then-maybe-create resolution below --
        # this project explicitly supports multiple concurrent
        # watch_capture.py-spawned processes sharing one database file,
        # and without this, two of them could both read "this team/
        # competition/event doesn't exist yet" for the same raw name and
        # both try to create it, racing on the same unique constraint
        # (or, worse, silently creating two rows that should have been
        # one). A second connection attempting the same thing blocks
        # (up to its own busy_timeout) instead of racing; Python's
        # sqlite3 module tracks the real autocommit state under the
        # hood, so once this BEGIN has run it won't also try to open its
        # own implicit transaction for the writes below.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            # Fast path only for a VERIFIED fixture mapping whose stored
            # team/competition/country invariants still match. A recycled
            # provider event id is marked SUSPECT and falls through without
            # mutating the previously mapped event.
            if source_event_id is not None:
                mapping = self._find_event_mapping(source_event_id)
                if mapping is not None and mapping.row["trust_state"] == TRUST_VERIFIED:
                    if self._event_mapping_is_compatible(
                        mapping,
                        sport=sport,
                        league=league.strip(),
                        home_raw=home_raw,
                        away_raw=away_raw,
                        country=canonical_country,
                        home_team_source_id=home_team_source_id,
                        away_team_source_id=away_team_source_id,
                        competition_source_id=competition_source_id,
                    ):
                        # A verified event is the strongest available context for
                        # learning provider team/competition ids.  This is the
                        # safe Sabah/Sabah Masazir path: identity comes from the
                        # already-known fixture, not from fuzzy team-name matching.
                        self._learn_identity_from_event_mapping(
                            mapping.event,
                            sport=sport,
                            league=league.strip(),
                            home_raw=home_raw,
                            away_raw=away_raw,
                            country=canonical_country,
                            home_team_source_id=home_team_source_id,
                            away_team_source_id=away_team_source_id,
                            competition_source_id=competition_source_id,
                        )
                        mapped_event = self._sync_event_start_time(
                            mapping.event,
                            start_time,
                            source_event_id=source_event_id,
                        )
                        self._refresh_event_mapping(
                            source_event_id,
                            sport=sport,
                            league=league.strip(),
                            home_raw=home_raw,
                            away_raw=away_raw,
                            country=canonical_country,
                            home_team_source_id=home_team_source_id,
                            away_team_source_id=away_team_source_id,
                            competition_source_id=competition_source_id,
                        )
                        self._connection.commit()
                        return EventMatchResult(mapped_event, 100.0, "resolved")

                    self._mark_event_mapping_suspect(source_event_id)

            # Competition first: it gives country/league context to the
            # identity pipeline and prevents fuzzy comparison across two
            # different, both-known countries.
            competition = self._resolve_competition(
                raw_league=league.strip(),
                sport=sport,
                source_competition_id=competition_source_id,
                country=canonical_country,
            )

            contextual_match = self._match_existing_event_by_context(
                sport=sport,
                competition=competition,
                home_raw=home_raw,
                away_raw=away_raw,
                start_time=start_time,
                home_team_source_id=home_team_source_id,
                away_team_source_id=away_team_source_id,
            )
            if contextual_match is not None:
                event, confidence = contextual_match
                self._learn_identity_from_event_context(
                    event,
                    sport=sport,
                    competition_id=competition.competition_id,
                    league=league.strip(),
                    home_raw=home_raw,
                    away_raw=away_raw,
                    country=canonical_country,
                    home_team_source_id=home_team_source_id,
                    away_team_source_id=away_team_source_id,
                    competition_source_id=competition_source_id,
                )
                event = self._sync_event_start_time(
                    event,
                    start_time,
                    source_event_id=source_event_id,
                )
                self._promote_event_to_reference(event.id, source_event_id)
                if source_event_id is not None:
                    self._save_event_mapping(
                        source_event_id,
                        event.id,
                        trust_state=TRUST_VERIFIED,
                        resolution_method="fixture_context",
                        sport=sport,
                        league=league.strip(),
                        home_raw=home_raw,
                        away_raw=away_raw,
                        country=canonical_country,
                        home_team_source_id=home_team_source_id,
                        away_team_source_id=away_team_source_id,
                        competition_source_id=competition_source_id,
                    )
                self._connection.commit()
                return EventMatchResult(event, confidence, "resolved-by-fixture-context")

            home = self._resolve_team(
                raw_name=home_raw,
                sport=sport,
                competition_id=competition.competition_id,
                source_team_id=home_team_source_id,
            )
            away = self._resolve_team(
                raw_name=away_raw,
                sport=sport,
                competition_id=competition.competition_id,
                source_team_id=away_team_source_id,
            )
            self._record_team_competition(home.team.id, competition.competition_id)
            self._record_team_competition(away.team.id, competition.competition_id)

            event = self._resolve_event(
                sport=sport,
                league=competition.canonical_name,
                competition_id=competition.competition_id,
                home=home.team,
                away=away.team,
                start_time=start_time,
                source_event_id=source_event_id,
            )

            if source_event_id is not None:
                component_trust = (
                    TRUST_VERIFIED
                    if home.trust_state == TRUST_VERIFIED
                    and away.trust_state == TRUST_VERIFIED
                    and competition.trust_state == TRUST_VERIFIED
                    else TRUST_UNVERIFIED
                )
                self._save_event_mapping(
                    source_event_id,
                    event.id,
                    trust_state=component_trust,
                    resolution_method="component_resolution",
                    sport=sport,
                    league=league.strip(),
                    home_raw=home_raw,
                    away_raw=away_raw,
                    country=canonical_country,
                    home_team_source_id=home_team_source_id,
                    away_team_source_id=away_team_source_id,
                    competition_source_id=competition_source_id,
                )
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

        return EventMatchResult(
            event, min(home.confidence, away.confidence, competition.confidence), "resolved"
        )

    def list_events(self, *, sport: str | None = None) -> list[Event]:
        if sport is None:
            rows = self._connection.execute("SELECT * FROM events").fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE sport = ?", (sport,)
            ).fetchall()
        return [self._map_event_row(row) for row in rows]

    def verify_team_mapping(
        self,
        *,
        sport: str,
        source_name: str,
        canonical_team_id: str,
        source_team_id: str | None = None,
        competition_id: str | None = None,
    ) -> None:
        """Persists an explicit human-approved provider -> canonical mapping.

        This is the operational escape hatch for cases such as Mozzart's
        ``Westham Untd`` or ``Sabah Masazir``: the correction is data, not a
        new global string-normalization rule.  Manual verification is allowed
        to clear a SUSPECT id mapping because a human has deliberately chosen
        the canonical target.
        """
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            team = self._connection.execute(
                "SELECT id FROM teams WHERE id = ? AND sport = ?",
                (canonical_team_id, sport),
            ).fetchone()
            if team is None:
                raise ValueError(
                    f"Unknown canonical team {canonical_team_id!r} for sport {sport!r}"
                )
            now = to_utc_iso(datetime.now(UTC))
            self._connection.execute(
                """
                INSERT INTO source_team_mappings (
                    source, sport, source_team_name, competition_id, team_id,
                    resolution_method, confidence, created_at,
                    trust_state, resolver_version
                ) VALUES (?, ?, ?, ?, ?, 'manual', 100.0, ?, 'VERIFIED', ?)
                ON CONFLICT (source, sport, source_team_name, competition_id)
                DO UPDATE SET
                    team_id = excluded.team_id,
                    resolution_method = 'manual', confidence = 100.0,
                    trust_state = 'VERIFIED', resolver_version = excluded.resolver_version
                """,
                (
                    self._provider_id,
                    sport,
                    source_name.strip(),
                    competition_id or "",
                    canonical_team_id,
                    now,
                    RESOLVER_VERSION,
                ),
            )
            if source_team_id is not None:
                self._save_team_id_mapping(
                    source_team_id,
                    sport,
                    canonical_team_id,
                    source_name.strip(),
                    trust_state=TRUST_VERIFIED,
                    resolution_method="manual",
                    allow_suspect_override=True,
                )
            if competition_id is not None:
                self._record_team_competition(canonical_team_id, competition_id)
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def verify_competition_mapping(
        self,
        *,
        sport: str,
        source_name: str,
        canonical_competition_id: str,
        source_competition_id: str | None = None,
        country: str | None = None,
    ) -> None:
        """Persists a human-approved competition mapping."""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            competition = self._connection.execute(
                "SELECT id FROM competitions WHERE id = ? AND sport = ?",
                (canonical_competition_id, sport),
            ).fetchone()
            if competition is None:
                raise ValueError(
                    "Unknown canonical competition "
                    f"{canonical_competition_id!r} for sport {sport!r}"
                )
            now = to_utc_iso(datetime.now(UTC))
            self._connection.execute(
                """
                INSERT INTO source_competition_mappings (
                    source, sport, source_competition_name, country_key, competition_id,
                    resolution_method, confidence, created_at,
                    trust_state, resolver_version
                ) VALUES (?, ?, ?, ?, ?, 'manual', 100.0, ?, 'VERIFIED', ?)
                ON CONFLICT (source, sport, source_competition_name, country_key)
                DO UPDATE SET
                    competition_id = excluded.competition_id,
                    resolution_method = 'manual', confidence = 100.0,
                    trust_state = 'VERIFIED', resolver_version = excluded.resolver_version
                """,
                (
                    self._provider_id,
                    sport,
                    source_name.strip(),
                    _country_key(country),
                    canonical_competition_id,
                    now,
                    RESOLVER_VERSION,
                ),
            )
            if source_competition_id is not None:
                self._save_competition_id_mapping(
                    source_competition_id,
                    sport,
                    canonical_competition_id,
                    source_name.strip(),
                    trust_state=TRUST_VERIFIED,
                    resolution_method="manual",
                    allow_suspect_override=True,
                )
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    # -- team resolution --------------------------------------------------

    def _resolve_team(
        self,
        *,
        raw_name: str,
        sport: str,
        competition_id: str,
        source_team_id: str | None = None,
    ) -> _TeamResolution:
        source_id_is_suspect = False
        # ID fast path: a provider-supplied team id already resolved once
        # (see source_team_id_mappings, migration 18) skips fuzzy
        # matching entirely on every later sighting, the same way
        # match()'s own source_event_id fast path skips the whole
        # team/competition/event resolution for a fixture. Unlike the
        # name-keyed cache just below, this survives the provider's raw
        # name itself drifting between sightings (a sponsor rename, a
        # transliteration change) -- exactly the case a name-keyed cache
        # can't help with, since a changed raw_name is a cache miss there
        # by construction.
        if source_team_id is not None:
            id_hit = self._find_team_by_source_id(source_team_id, sport)
            if id_hit is not None:
                team, mapping = id_hit
                baseline_name = (
                    mapping["last_verified_raw_name"]
                    or mapping["first_seen_raw_name"]
                )
                if (
                    mapping["trust_state"] == TRUST_VERIFIED
                    and baseline_name is not None
                    and names_are_similar(
                        baseline_name,
                        raw_name,
                        fuzzy_threshold=self._fuzzy_threshold,
                    )
                ):
                    self._save_team_id_mapping(
                        source_team_id,
                        sport,
                        team.id,
                        raw_name,
                        trust_state=TRUST_VERIFIED,
                        resolution_method="provider_id",
                    )
                    return _TeamResolution(
                        team, 100.0, "provider_id", TRUST_VERIFIED
                    )
                if mapping["trust_state"] == TRUST_VERIFIED:
                    logger.warning(
                        "fixture_catalog.team_id.name_drift",
                        extra={
                            "source_team_id": source_team_id,
                            "team_id": team.id,
                            "last_verified_raw_name": baseline_name,
                            "raw_name": raw_name,
                        },
                    )
                    self._mark_team_id_mapping_suspect(
                        source_team_id, sport, raw_name
                    )
                    source_id_is_suspect = True
                elif mapping["trust_state"] == TRUST_SUSPECT:
                    source_id_is_suspect = True

        mapped = self._find_mapping(raw_name, sport, competition_id)
        if (
            mapped is not None
            and self._provider_id == REFERENCE_PROVIDER_ID
            and source_team_id is not None
            and self._team_has_different_reference_id(mapped.id, source_team_id)
        ):
            mapped = None
        if mapped is not None:
            mapped_trust = (
                TRUST_SUSPECT if source_id_is_suspect else TRUST_VERIFIED
            )
            if source_team_id is not None:
                self._save_team_id_mapping(
                    source_team_id,
                    sport,
                    mapped.id,
                    raw_name,
                    trust_state=mapped_trust,
                    resolution_method="verified_name_mapping",
                )
            return _TeamResolution(
                mapped, 100.0, "verified_name_mapping", mapped_trust
            )

        global_match = self._find_global_exact_team(
            raw_name=raw_name,
            sport=sport,
            competition_id=competition_id,
            source_team_id=source_team_id,
        )
        if global_match is not None:
            team, method = global_match
            if self._provider_id == REFERENCE_PROVIDER_ID and source_team_id is not None:
                self._promote_team_to_reference(team.id, sport, source_team_id)
            self._save_mapping(
                raw_name,
                sport,
                competition_id,
                team.id,
                resolution_method=method,
                confidence=100.0,
                trust_state=TRUST_VERIFIED,
            )
            if source_team_id is not None:
                self._save_team_id_mapping(
                    source_team_id,
                    sport,
                    team.id,
                    raw_name,
                    trust_state=TRUST_VERIFIED,
                    resolution_method=method,
                )
            return _TeamResolution(team, 100.0, method, TRUST_VERIFIED)

        existing = self._teams_for_competition(sport, competition_id)
        normalizer = TeamNormalizer(
            existing.keys(),
            aliases=self._aliases,
            token_aliases=self._token_aliases,
            fuzzy_threshold=self._fuzzy_threshold,
            ambiguity_margin=self._fuzzy_ambiguity_margin,
        )
        result = normalizer.normalize(raw_name)

        if result.method in {"ambiguous", "fuzzy"}:
            # Two existing teams scored too close together to safely pick
            # one (see TeamNormalizer) -- the conservative choice is the
            # same as "unknown": a new team under the (token-expanded --
            # see TeamNormalizer.normalize) raw name, not a guessed merge
            # into either candidate. Logged distinctly since this is
            # exactly the kind of borderline call worth a human noticing,
            # unlike a routine first-sighting.
            log = logger.warning if result.method == "ambiguous" else logger.info
            log(
                "fixture_catalog.team.untrusted_similarity",
                extra={"raw_name": raw_name, "sport": sport, "score": result.confidence},
            )
            team = self._create_team(
                canonical_name=result.raw_name,
                sport=sport,
                source_team_id=source_team_id,
            )
            confidence = result.confidence
        elif result.canonical_name is not None:
            # The alias/fuzzy target may not exist as a team row yet (e.g.
            # the first-ever sighting of this team arrives under an alias
            # like "Man Utd" -> "Manchester United") -- create it under
            # that canonical form, not under the raw name, so later exact
            # matches on the canonical name itself resolve correctly too.
            candidate_team = existing.get(result.canonical_name)
            if (
                candidate_team is not None
                and self._provider_id == REFERENCE_PROVIDER_ID
                and source_team_id is not None
                and self._team_has_different_reference_id(
                    candidate_team.id, source_team_id
                )
            ):
                # A reference-provider id is stronger than an identical
                # display name.  Two API ids called "United" are two teams
                # until explicit fixture/manual evidence says otherwise.
                candidate_team = None
            if candidate_team is None:
                candidate_team = self._create_team(
                    canonical_name=result.canonical_name,
                    sport=sport,
                    source_team_id=source_team_id,
                )
            team = candidate_team
            confidence = result.confidence
        else:
            # result.raw_name, not the raw_name argument: token expansion
            # (see TeamNormalizer.normalize) must still apply to a
            # brand-new team's canonical name, or a *later* sighting under
            # a different provider's spelling of the same acronym (e.g.
            # "United Arab Emirates M23") would compare against this row's
            # literal, unexpanded name ("UAE M23") and fail to fuzzy-match
            # it -- the same gap this mechanism exists to close, just
            # hitting whichever provider is seen second instead of first.
            team = self._create_team(
                canonical_name=result.raw_name,
                sport=sport,
                source_team_id=source_team_id,
            )
            confidence = 100.0

        trust_state = (
            TRUST_SUSPECT
            if source_id_is_suspect
            else self._trust_for_method(result.method)
        )
        if (
            self._provider_id == REFERENCE_PROVIDER_ID
            and source_team_id is not None
            and trust_state == TRUST_VERIFIED
        ):
            self._promote_team_to_reference(team.id, sport, source_team_id)

        # result.method -- "exact"/"alias"/"fuzzy"/"ambiguous"/"unknown" --
        # is stored alongside the mapping (migration 12), not just logged
        # for the risky "ambiguous" case above: a *fuzzy* match that
        # happened to score just above fuzzy_threshold is exactly as
        # silently permanent as an ambiguous one once cached here, and
        # without this, reviewing which mappings were confident exact/
        # alias matches versus borderline fuzzy guesses meant re-deriving
        # it by re-running the matcher after the fact.
        self._save_mapping(
            raw_name,
            sport,
            competition_id,
            team.id,
            resolution_method=result.method,
            confidence=confidence,
            trust_state=trust_state,
        )
        if source_team_id is not None:
            self._save_team_id_mapping(
                source_team_id,
                sport,
                team.id,
                raw_name,
                trust_state=trust_state,
                resolution_method=result.method,
            )
        return _TeamResolution(team, confidence, result.method, trust_state)

    def _find_mapping(
        self, raw_name: str, sport: str, competition_id: str
    ) -> Team | None:
        row = self._connection.execute(
            """
            SELECT t.* FROM source_team_mappings m
            JOIN teams t ON t.id = m.team_id
            WHERE m.source = ? AND m.sport = ? AND m.source_team_name = ?
              AND m.trust_state = 'VERIFIED'
              AND (
                    m.competition_id = ?
                    OR (m.competition_id = '' AND m.resolution_method = 'manual')
                  )
            ORDER BY CASE WHEN m.competition_id = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (self._provider_id, sport, raw_name, competition_id, competition_id),
        ).fetchone()
        return self._map_team_row(row) if row else None

    def _save_mapping(
        self,
        raw_name: str,
        sport: str,
        competition_id: str,
        team_id: str,
        *,
        resolution_method: str,
        confidence: float,
        trust_state: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO source_team_mappings
                (source, sport, source_team_name, competition_id, team_id,
                 resolution_method, confidence, created_at,
                 trust_state, resolver_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, sport, source_team_name, competition_id) DO UPDATE SET
                team_id = CASE
                    WHEN source_team_mappings.trust_state = 'VERIFIED'
                    THEN source_team_mappings.team_id ELSE excluded.team_id END,
                resolution_method = CASE
                    WHEN source_team_mappings.trust_state = 'VERIFIED'
                    THEN source_team_mappings.resolution_method
                    ELSE excluded.resolution_method END,
                confidence = CASE
                    WHEN source_team_mappings.trust_state = 'VERIFIED'
                    THEN source_team_mappings.confidence ELSE excluded.confidence END,
                trust_state = CASE
                    WHEN source_team_mappings.trust_state = 'VERIFIED'
                    THEN source_team_mappings.trust_state ELSE excluded.trust_state END,
                resolver_version = excluded.resolver_version
            """,
            (
                self._provider_id,
                sport,
                raw_name,
                competition_id,
                team_id,
                resolution_method,
                confidence,
                to_utc_iso(datetime.now(UTC)),
                trust_state,
                RESOLVER_VERSION,
            ),
        )

    def _teams_for_competition(
        self, sport: str, competition_id: str
    ) -> dict[str, Team]:
        rows = self._connection.execute(
            """
            SELECT t.* FROM teams t
            JOIN team_competitions tc ON tc.team_id = t.id
            WHERE t.sport = ? AND tc.competition_id = ?
            """,
            (sport, competition_id),
        ).fetchall()
        return {row["canonical_name"]: self._map_team_row(row) for row in rows}

    def _find_global_exact_team(
        self,
        *,
        raw_name: str,
        sport: str,
        competition_id: str,
        source_team_id: str | None,
    ) -> tuple[Team, str] | None:
        rows = self._connection.execute(
            "SELECT * FROM teams WHERE sport = ?", (sport,)
        ).fetchall()
        if not rows:
            return None
        normalizer = TeamNormalizer(
            [row["canonical_name"] for row in rows],
            aliases=self._aliases,
            token_aliases=self._token_aliases,
            fuzzy_threshold=self._fuzzy_threshold,
            ambiguity_margin=self._fuzzy_ambiguity_margin,
        )
        result = normalizer.normalize(raw_name)
        # An exact, globally-unique display name may connect the same club
        # across league/cup competitions (subject to country compatibility).
        # A configured alias is only evidence inside the current competition
        # or a unique fixture context; never make it a global identity rule.
        if result.method != "exact" or result.canonical_name is None:
            return None
        target_key = " ".join(result.canonical_name.split()).casefold()
        candidates = [
            row
            for row in rows
            if " ".join(row["canonical_name"].split()).casefold() == target_key
        ]
        if len(candidates) != 1:
            return None
        row = candidates[0]
        if (
            self._provider_id == REFERENCE_PROVIDER_ID
            and source_team_id is not None
            and self._team_has_different_reference_id(row["id"], source_team_id)
        ):
            return None
        if not self._team_country_is_compatible(row["id"], competition_id):
            return None
        return self._map_team_row(row), result.method

    def _team_country_is_compatible(
        self, team_id: str, competition_id: str
    ) -> bool:
        current = self._connection.execute(
            "SELECT country FROM competitions WHERE id = ?", (competition_id,)
        ).fetchone()
        if current is None or current["country"] is None:
            return True
        known_rows = self._connection.execute(
            """
            SELECT DISTINCT c.country
            FROM team_competitions tc
            JOIN competitions c ON c.id = tc.competition_id
            WHERE tc.team_id = ? AND c.country IS NOT NULL
            """,
            (team_id,),
        ).fetchall()
        if not known_rows:
            return True
        current_key = _country_key(current["country"])
        return any(_country_key(row["country"]) == current_key for row in known_rows)

    def _team_has_different_reference_id(
        self, team_id: str, source_team_id: str
    ) -> bool:
        row = self._connection.execute(
            """
            SELECT reference_provider_id FROM teams
            WHERE id = ? AND reference_provider = ?
            """,
            (team_id, REFERENCE_PROVIDER_ID),
        ).fetchone()
        return row is not None and row["reference_provider_id"] != source_team_id

    def _record_team_competition(self, team_id: str, competition_id: str) -> None:
        now = to_utc_iso(datetime.now(UTC))
        self._connection.execute(
            """
            INSERT INTO team_competitions (
                team_id, competition_id, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT (team_id, competition_id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at
            """,
            (team_id, competition_id, now, now),
        )

    def _create_team(
        self, *, canonical_name: str, sport: str, source_team_id: str | None = None
    ) -> Team:
        team = Team(id=f"team-{uuid4().hex[:10]}", canonical_name=canonical_name)
        reference_id_already_used = False
        if source_team_id is not None:
            reference_id_already_used = (
                self._connection.execute(
                    """
                    SELECT 1 FROM teams
                    WHERE reference_provider = ? AND sport = ?
                      AND reference_provider_id = ?
                    """,
                    (REFERENCE_PROVIDER_ID, sport, source_team_id),
                ).fetchone()
                is not None
            )
        is_reference = (
            self._provider_id == REFERENCE_PROVIDER_ID
            and source_team_id is not None
            and not reference_id_already_used
        )
        self._connection.execute(
            """
            INSERT INTO teams (
                id, canonical_name, sport, reference_provider,
                reference_provider_id, identity_status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                team.id,
                team.canonical_name,
                sport,
                REFERENCE_PROVIDER_ID if is_reference else None,
                source_team_id if is_reference else None,
                "REFERENCE" if is_reference else "PROVISIONAL",
            ),
        )
        logger.info(
            "fixture_catalog.team.created",
            extra={"team_id": team.id, "canonical_name": team.canonical_name, "sport": sport},
        )
        return team

    def _find_team_by_source_id(
        self, source_team_id: str, sport: str
    ) -> tuple[Team, Row] | None:
        row = self._connection.execute(
            """
            SELECT t.id AS canonical_team_id, t.canonical_name, m.*
            FROM source_team_id_mappings m
            JOIN teams t ON t.id = m.team_id
            WHERE m.source = ? AND m.sport = ? AND m.source_team_id = ?
            """,
            (self._provider_id, sport, source_team_id),
        ).fetchone()
        if row is None:
            return None
        return Team(row["canonical_team_id"], row["canonical_name"]), row

    def _save_team_id_mapping(
        self,
        source_team_id: str,
        sport: str,
        team_id: str,
        raw_name: str,
        *,
        trust_state: str,
        resolution_method: str,
        allow_suspect_override: bool = False,
    ) -> None:
        now = to_utc_iso(datetime.now(UTC))
        self._connection.execute(
            """
            INSERT INTO source_team_id_mappings
                (source, sport, source_team_id, team_id,
                 first_seen_raw_name, last_seen_raw_name, last_verified_raw_name,
                 trust_state, resolution_method, resolver_version,
                 verified_at, last_checked_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, sport, source_team_id) DO UPDATE SET
                last_seen_raw_name = excluded.last_seen_raw_name,
                last_checked_at = excluded.last_checked_at,
                team_id = CASE
                    WHEN source_team_id_mappings.trust_state = 'SUSPECT' AND NOT ?
                    THEN source_team_id_mappings.team_id ELSE excluded.team_id END,
                trust_state = CASE
                    WHEN source_team_id_mappings.trust_state = 'SUSPECT' AND NOT ?
                    THEN source_team_id_mappings.trust_state ELSE excluded.trust_state END,
                resolution_method = CASE
                    WHEN source_team_id_mappings.trust_state IN ('VERIFIED', 'SUSPECT') AND NOT ?
                    THEN source_team_id_mappings.resolution_method
                    ELSE excluded.resolution_method END,
                resolver_version = excluded.resolver_version,
                last_verified_raw_name = CASE
                    WHEN excluded.trust_state = 'VERIFIED'
                         AND (source_team_id_mappings.trust_state != 'SUSPECT' OR ?)
                    THEN excluded.last_verified_raw_name
                    ELSE source_team_id_mappings.last_verified_raw_name END,
                verified_at = CASE
                    WHEN excluded.trust_state = 'VERIFIED'
                         AND (source_team_id_mappings.trust_state != 'SUSPECT' OR ?)
                    THEN excluded.verified_at
                    ELSE source_team_id_mappings.verified_at END,
                drift_detected_at = CASE WHEN ? THEN NULL
                    ELSE source_team_id_mappings.drift_detected_at END
            """,
            (
                self._provider_id,
                sport,
                source_team_id,
                team_id,
                raw_name,
                raw_name,
                raw_name if trust_state == TRUST_VERIFIED else None,
                trust_state,
                resolution_method,
                RESOLVER_VERSION,
                now if trust_state == TRUST_VERIFIED else None,
                now,
                now,
                allow_suspect_override,
                allow_suspect_override,
                allow_suspect_override,
                allow_suspect_override,
                allow_suspect_override,
                allow_suspect_override,
            ),
        )

    def _mark_team_id_mapping_suspect(
        self, source_team_id: str, sport: str, raw_name: str
    ) -> None:
        now = to_utc_iso(datetime.now(UTC))
        self._connection.execute(
            """
            UPDATE source_team_id_mappings
            SET trust_state = 'SUSPECT', last_seen_raw_name = ?,
                last_checked_at = ?, drift_detected_at = ?
            WHERE source = ? AND sport = ? AND source_team_id = ?
            """,
            (raw_name, now, now, self._provider_id, sport, source_team_id),
        )

    def _promote_team_to_reference(
        self, team_id: str, sport: str, source_team_id: str
    ) -> None:
        self._connection.execute(
            """
            UPDATE teams
            SET reference_provider = ?, reference_provider_id = ?,
                identity_status = 'REFERENCE'
            WHERE id = ? AND sport = ?
              AND (
                    reference_provider IS NULL
                    OR (reference_provider = ? AND reference_provider_id = ?)
                  )
              AND NOT EXISTS (
                  SELECT 1 FROM teams existing
                  WHERE existing.reference_provider = ?
                    AND existing.sport = ?
                    AND existing.reference_provider_id = ?
                    AND existing.id != ?
              )
            """,
            (
                REFERENCE_PROVIDER_ID,
                source_team_id,
                team_id,
                sport,
                REFERENCE_PROVIDER_ID,
                source_team_id,
                REFERENCE_PROVIDER_ID,
                sport,
                source_team_id,
                team_id,
            ),
        )

    # -- competition resolution --------------------------------------------

    def _resolve_competition(
        self,
        *,
        raw_league: str,
        sport: str,
        source_competition_id: str | None = None,
        country: str | None = None,
    ) -> _CompetitionResolution:
        """Resolves a raw league/competition string to its canonical
        name and stable id, the same exact/alias/fuzzy pattern
        _resolve_team uses for team names. Without this, "Premier
        League" (the-odds-api), "England Premier League" (a different
        source), and "Engleska Premier Liga" (Mozzart, in Serbian) would
        each resolve to their own separate canonical event even for the
        exact same real match -- league was made part of an event's
        identity specifically to stop *different* competitions from
        being merged together, which only works if same-competition
        spelling variants first collapse to one canonical name.

        Returns (canonical_name, competition_id). Event.league stays the
        canonical display string, but _resolve_event keys an event's
        identity on competition_id, not this string -- see that
        method's docstring for why.
        """
        country = _canonical_country(country)
        source_id_is_suspect = False

        # ID fast path -- mirrors _resolve_team's own source_team_id fast
        # path above; see that method's comment for the full reasoning
        # (source_competition_id_mappings, migration 18).
        if source_competition_id is not None:
            id_hit = self._find_competition_by_source_id(source_competition_id, sport)
            if id_hit is not None:
                canonical_name, competition_id, mapping = id_hit
                baseline_name = (
                    mapping["last_verified_raw_name"]
                    or mapping["first_seen_raw_name"]
                )
                if (
                    mapping["trust_state"] == TRUST_VERIFIED
                    and baseline_name is not None
                    and names_are_similar(
                        baseline_name,
                        raw_league,
                        fuzzy_threshold=self._fuzzy_threshold,
                    )
                ):
                    self._save_competition_id_mapping(
                        source_competition_id,
                        sport,
                        competition_id,
                        raw_league,
                        trust_state=TRUST_VERIFIED,
                        resolution_method="provider_id",
                    )
                    return _CompetitionResolution(
                        canonical_name,
                        competition_id,
                        100.0,
                        "provider_id",
                        TRUST_VERIFIED,
                    )
                if mapping["trust_state"] == TRUST_VERIFIED:
                    logger.warning(
                        "fixture_catalog.competition_id.name_drift",
                        extra={
                            "source_competition_id": source_competition_id,
                            "competition_id": competition_id,
                            "last_verified_raw_name": baseline_name,
                            "raw_league": raw_league,
                        },
                    )
                    self._mark_competition_id_mapping_suspect(
                        source_competition_id, sport, raw_league
                    )
                    source_id_is_suspect = True
                elif mapping["trust_state"] == TRUST_SUSPECT:
                    source_id_is_suspect = True

        mapped = self._find_competition_mapping(raw_league, sport, country)
        if (
            mapped is not None
            and self._provider_id == REFERENCE_PROVIDER_ID
            and source_competition_id is not None
            and self._competition_has_different_reference_id(
                mapped[1], source_competition_id
            )
        ):
            mapped = None
        if mapped is not None:
            mapped_trust = (
                TRUST_SUSPECT if source_id_is_suspect else TRUST_VERIFIED
            )
            if source_competition_id is not None:
                self._save_competition_id_mapping(
                    source_competition_id,
                    sport,
                    mapped[1],
                    raw_league,
                    trust_state=mapped_trust,
                    resolution_method="verified_name_mapping",
                )
            return _CompetitionResolution(
                mapped[0], mapped[1], 100.0, "verified_name_mapping", mapped_trust
            )

        existing = self._competitions_for_sport(sport, country)
        normalizer = TeamNormalizer(
            existing.keys(),
            aliases=self._league_aliases,
            fuzzy_threshold=self._fuzzy_threshold,
            ambiguity_margin=self._fuzzy_ambiguity_margin,
        )
        result = normalizer.normalize(raw_league)

        if result.method in {"ambiguous", "fuzzy"}:
            log = logger.warning if result.method == "ambiguous" else logger.info
            log(
                "fixture_catalog.competition.untrusted_similarity",
                extra={"raw_league": raw_league, "sport": sport, "score": result.confidence},
            )
            canonical_name = raw_league
            competition_id = self._create_competition(
                canonical_name=canonical_name,
                sport=sport,
                country=country,
                source_competition_id=source_competition_id,
            )
            confidence = result.confidence
        elif result.canonical_name is not None:
            canonical_name = result.canonical_name
            candidate_competition_id = existing.get(canonical_name)
            if (
                candidate_competition_id is not None
                and self._provider_id == REFERENCE_PROVIDER_ID
                and source_competition_id is not None
                and self._competition_has_different_reference_id(
                    candidate_competition_id, source_competition_id
                )
            ):
                candidate_competition_id = None
            if candidate_competition_id is None:
                candidate_competition_id = self._create_competition(
                    canonical_name=canonical_name,
                    sport=sport,
                    country=country,
                    source_competition_id=source_competition_id,
                )
            competition_id = candidate_competition_id
            confidence = result.confidence
        else:
            canonical_name = raw_league
            competition_id = self._create_competition(
                canonical_name=canonical_name,
                sport=sport,
                country=country,
                source_competition_id=source_competition_id,
            )
            confidence = 100.0

        trust_state = (
            TRUST_SUSPECT
            if source_id_is_suspect
            else self._trust_for_method(result.method)
        )
        if (
            self._provider_id == REFERENCE_PROVIDER_ID
            and source_competition_id is not None
            and trust_state == TRUST_VERIFIED
        ):
            self._promote_competition_to_reference(
                competition_id, sport, source_competition_id, country
            )

        # Same audit-trail reasoning as _resolve_team's own _save_mapping
        # call -- see that method's comment.
        self._save_competition_mapping(
            raw_league,
            sport,
            country,
            competition_id,
            resolution_method=result.method,
            confidence=confidence,
            trust_state=trust_state,
        )
        if source_competition_id is not None:
            self._save_competition_id_mapping(
                source_competition_id,
                sport,
                competition_id,
                raw_league,
                trust_state=trust_state,
                resolution_method=result.method,
            )
        return _CompetitionResolution(
            canonical_name, competition_id, confidence, result.method, trust_state
        )

    def _find_competition_mapping(
        self, raw_league: str, sport: str, country: str | None
    ) -> tuple[str, str] | None:
        row = self._connection.execute(
            """
            SELECT c.canonical_name, c.id FROM source_competition_mappings m
            JOIN competitions c ON c.id = m.competition_id
            WHERE m.source = ? AND m.sport = ? AND m.source_competition_name = ?
              AND m.trust_state = 'VERIFIED'
              AND (
                    m.country_key = ?
                    OR (m.country_key = '' AND m.resolution_method = 'manual')
                  )
            ORDER BY CASE WHEN m.country_key = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (
                self._provider_id,
                sport,
                raw_league,
                _country_key(country),
                _country_key(country),
            ),
        ).fetchone()
        return (row["canonical_name"], row["id"]) if row else None

    def _save_competition_mapping(
        self,
        raw_league: str,
        sport: str,
        country: str | None,
        competition_id: str,
        *,
        resolution_method: str,
        confidence: float,
        trust_state: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO source_competition_mappings
                (source, sport, source_competition_name, country_key, competition_id,
                 resolution_method, confidence, created_at,
                 trust_state, resolver_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, sport, source_competition_name, country_key)
            DO UPDATE SET
                competition_id = CASE
                    WHEN source_competition_mappings.trust_state = 'VERIFIED'
                    THEN source_competition_mappings.competition_id
                    ELSE excluded.competition_id END,
                resolution_method = CASE
                    WHEN source_competition_mappings.trust_state = 'VERIFIED'
                    THEN source_competition_mappings.resolution_method
                    ELSE excluded.resolution_method END,
                confidence = CASE
                    WHEN source_competition_mappings.trust_state = 'VERIFIED'
                    THEN source_competition_mappings.confidence ELSE excluded.confidence END,
                trust_state = CASE
                    WHEN source_competition_mappings.trust_state = 'VERIFIED'
                    THEN source_competition_mappings.trust_state ELSE excluded.trust_state END,
                resolver_version = excluded.resolver_version
            """,
            (
                self._provider_id,
                sport,
                raw_league,
                _country_key(country),
                competition_id,
                resolution_method,
                confidence,
                to_utc_iso(datetime.now(UTC)),
                trust_state,
                RESOLVER_VERSION,
            ),
        )

    def _competitions_for_sport(
        self, sport: str, country: str | None
    ) -> dict[str, str]:
        if country is None:
            rows = self._connection.execute(
                "SELECT * FROM competitions WHERE sport = ?", (sport,)
            ).fetchall()
        else:
            rows = self._connection.execute(
                """
                SELECT * FROM competitions
                WHERE sport = ? AND (country IS NULL OR country = ?)
                """,
                (sport, country),
            ).fetchall()
        return {row["canonical_name"]: row["id"] for row in rows}

    def _competition_has_different_reference_id(
        self, competition_id: str, source_competition_id: str
    ) -> bool:
        row = self._connection.execute(
            """
            SELECT reference_provider_id FROM competitions
            WHERE id = ? AND reference_provider = ?
            """,
            (competition_id, REFERENCE_PROVIDER_ID),
        ).fetchone()
        return row is not None and row["reference_provider_id"] != source_competition_id

    def _create_competition(
        self,
        *,
        canonical_name: str,
        sport: str,
        country: str | None,
        source_competition_id: str | None = None,
    ) -> str:
        competition_id = f"competition-{uuid4().hex[:10]}"
        reference_id_already_used = False
        if source_competition_id is not None:
            reference_id_already_used = (
                self._connection.execute(
                    """
                    SELECT 1 FROM competitions
                    WHERE reference_provider = ? AND sport = ?
                      AND reference_provider_id = ?
                    """,
                    (REFERENCE_PROVIDER_ID, sport, source_competition_id),
                ).fetchone()
                is not None
            )
        is_reference = (
            self._provider_id == REFERENCE_PROVIDER_ID
            and source_competition_id is not None
            and not reference_id_already_used
        )
        self._connection.execute(
            """
            INSERT INTO competitions (
                id, canonical_name, sport, country, reference_provider,
                reference_provider_id, identity_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                competition_id,
                canonical_name,
                sport,
                country,
                REFERENCE_PROVIDER_ID if is_reference else None,
                source_competition_id if is_reference else None,
                "REFERENCE" if is_reference else "PROVISIONAL",
            ),
        )
        logger.info(
            "fixture_catalog.competition.created",
            extra={
                "competition_id": competition_id,
                "canonical_name": canonical_name,
                "sport": sport,
            },
        )
        return competition_id

    def _find_competition_by_source_id(
        self, source_competition_id: str, sport: str
    ) -> tuple[str, str, Row] | None:
        row = self._connection.execute(
            """
            SELECT c.canonical_name, c.id AS canonical_competition_id, m.*
            FROM source_competition_id_mappings m
            JOIN competitions c ON c.id = m.competition_id
            WHERE m.source = ? AND m.sport = ? AND m.source_competition_id = ?
            """,
            (self._provider_id, sport, source_competition_id),
        ).fetchone()
        if row is None:
            return None
        return row["canonical_name"], row["canonical_competition_id"], row

    def _save_competition_id_mapping(
        self,
        source_competition_id: str,
        sport: str,
        competition_id: str,
        raw_league: str,
        *,
        trust_state: str,
        resolution_method: str,
        allow_suspect_override: bool = False,
    ) -> None:
        now = to_utc_iso(datetime.now(UTC))
        self._connection.execute(
            """
            INSERT INTO source_competition_id_mappings
                (source, sport, source_competition_id, competition_id,
                 first_seen_raw_name, last_seen_raw_name, last_verified_raw_name,
                 trust_state, resolution_method, resolver_version,
                 verified_at, last_checked_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, sport, source_competition_id) DO UPDATE SET
                last_seen_raw_name = excluded.last_seen_raw_name,
                last_checked_at = excluded.last_checked_at,
                competition_id = CASE
                    WHEN source_competition_id_mappings.trust_state = 'SUSPECT' AND NOT ?
                    THEN source_competition_id_mappings.competition_id
                    ELSE excluded.competition_id END,
                trust_state = CASE
                    WHEN source_competition_id_mappings.trust_state = 'SUSPECT' AND NOT ?
                    THEN source_competition_id_mappings.trust_state
                    ELSE excluded.trust_state END,
                resolution_method = CASE
                    WHEN source_competition_id_mappings.trust_state
                         IN ('VERIFIED', 'SUSPECT') AND NOT ?
                    THEN source_competition_id_mappings.resolution_method
                    ELSE excluded.resolution_method END,
                resolver_version = excluded.resolver_version,
                last_verified_raw_name = CASE
                    WHEN excluded.trust_state = 'VERIFIED'
                         AND (source_competition_id_mappings.trust_state != 'SUSPECT' OR ?)
                    THEN excluded.last_verified_raw_name
                    ELSE source_competition_id_mappings.last_verified_raw_name END,
                verified_at = CASE
                    WHEN excluded.trust_state = 'VERIFIED'
                         AND (source_competition_id_mappings.trust_state != 'SUSPECT' OR ?)
                    THEN excluded.verified_at
                    ELSE source_competition_id_mappings.verified_at END,
                drift_detected_at = CASE WHEN ? THEN NULL
                    ELSE source_competition_id_mappings.drift_detected_at END
            """,
            (
                self._provider_id,
                sport,
                source_competition_id,
                competition_id,
                raw_league,
                raw_league,
                raw_league if trust_state == TRUST_VERIFIED else None,
                trust_state,
                resolution_method,
                RESOLVER_VERSION,
                now if trust_state == TRUST_VERIFIED else None,
                now,
                now,
                allow_suspect_override,
                allow_suspect_override,
                allow_suspect_override,
                allow_suspect_override,
                allow_suspect_override,
                allow_suspect_override,
            ),
        )

    def _mark_competition_id_mapping_suspect(
        self, source_competition_id: str, sport: str, raw_league: str
    ) -> None:
        now = to_utc_iso(datetime.now(UTC))
        self._connection.execute(
            """
            UPDATE source_competition_id_mappings
            SET trust_state = 'SUSPECT', last_seen_raw_name = ?,
                last_checked_at = ?, drift_detected_at = ?
            WHERE source = ? AND sport = ? AND source_competition_id = ?
            """,
            (
                raw_league,
                now,
                now,
                self._provider_id,
                sport,
                source_competition_id,
            ),
        )

    def _promote_competition_to_reference(
        self,
        competition_id: str,
        sport: str,
        source_competition_id: str,
        country: str | None,
    ) -> None:
        self._connection.execute(
            """
            UPDATE competitions
            SET reference_provider = ?, reference_provider_id = ?,
                identity_status = 'REFERENCE', country = COALESCE(?, country)
            WHERE id = ? AND sport = ?
              AND (
                    reference_provider IS NULL
                    OR (reference_provider = ? AND reference_provider_id = ?)
                  )
              AND NOT EXISTS (
                  SELECT 1 FROM competitions existing
                  WHERE existing.reference_provider = ?
                    AND existing.sport = ?
                    AND existing.reference_provider_id = ?
                    AND existing.id != ?
              )
            """,
            (
                REFERENCE_PROVIDER_ID,
                source_competition_id,
                country,
                competition_id,
                sport,
                REFERENCE_PROVIDER_ID,
                source_competition_id,
                REFERENCE_PROVIDER_ID,
                sport,
                source_competition_id,
                competition_id,
            ),
        )

    # -- event resolution --------------------------------------------------

    def _match_existing_event_by_context(
        self,
        *,
        sport: str,
        competition: _CompetitionResolution,
        home_raw: str,
        away_raw: str,
        start_time: datetime,
        home_team_source_id: str | None,
        away_team_source_id: str | None,
    ) -> tuple[Event, float] | None:
        """Matches a fixture as one unit before resolving isolated teams.

        Competition, kickoff, home side and away side are joint evidence.
        This is what makes a first-ever ``Sabah Masazir`` sighting resolvable
        against an existing API fixture without first poisoning a global team
        name cache.  Home/away order is intentional; a reversed feed is a
        conflict to review, not something silently folded together.
        """
        if competition.trust_state != TRUST_VERIFIED:
            return None

        lower = to_utc_iso(start_time - self._start_time_tolerance)
        upper = to_utc_iso(start_time + self._start_time_tolerance)
        rows = self._connection.execute(
            """
            SELECT * FROM events
            WHERE competition_id = ? AND start_time BETWEEN ? AND ?
            ORDER BY ABS(julianday(start_time) - julianday(?))
            """,
            (
                competition.competition_id,
                lower,
                upper,
                to_utc_iso(start_time),
            ),
        ).fetchall()

        scored: list[tuple[float, Event]] = []
        for row in rows:
            event = self._map_event_row(row)
            home_score = self._team_evidence_score(
                raw_name=home_raw,
                candidate=event.home_team,
                sport=sport,
                source_team_id=home_team_source_id,
            )
            away_score = self._team_evidence_score(
                raw_name=away_raw,
                candidate=event.away_team,
                sport=sport,
                source_team_id=away_team_source_id,
            )
            if home_score < 0 or away_score < 0:
                continue
            if home_score == 0.0 and away_score == 0.0:
                continue
            if 0.0 in {home_score, away_score}:
                # One exact side plus competition+kickoff+home/away position
                # identifies the other side of a unique fixture even when its
                # spelling shares no useful characters (Sabah Masazir/Sabah).
                if max(home_score, away_score) < 100.0:
                    continue
                combined_score = 90.0
            else:
                if min(home_score, away_score) < self._fuzzy_threshold:
                    continue
                combined_score = (home_score + away_score) / 2.0
            if max(home_score, away_score) < 100.0 and combined_score < 90.0:
                # Two merely-fuzzy names are not enough to connect odds to a
                # reference event, even when the time happens to line up.
                continue
            scored.append((combined_score, event))

        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        if (
            len(scored) > 1
            and scored[0][0] - scored[1][0] < self._fuzzy_ambiguity_margin
        ):
            logger.warning(
                "fixture_catalog.event.ambiguous_fixture_context",
                extra={
                    "competition_id": competition.competition_id,
                    "home_raw": home_raw,
                    "away_raw": away_raw,
                    "candidate_count": len(scored),
                },
            )
            return None
        return scored[0][1], scored[0][0]

    def _team_evidence_score(
        self,
        *,
        raw_name: str,
        candidate: Team,
        sport: str,
        source_team_id: str | None,
    ) -> float:
        if source_team_id is not None:
            id_hit = self._find_team_by_source_id(source_team_id, sport)
            if id_hit is not None:
                if id_hit[1]["trust_state"] != TRUST_VERIFIED:
                    return 0.0
                return 100.0 if id_hit[0].id == candidate.id else -1.0

        result = TeamNormalizer(
            [candidate.canonical_name],
            aliases=self._aliases,
            token_aliases=self._token_aliases,
            fuzzy_threshold=self._fuzzy_threshold,
            ambiguity_margin=self._fuzzy_ambiguity_margin,
        ).normalize(raw_name)
        if result.canonical_name != candidate.canonical_name:
            return 0.0
        return result.confidence

    def _resolve_event(
        self,
        *,
        sport: str,
        league: str,
        competition_id: str,
        home: Team,
        away: Team,
        start_time: datetime,
        source_event_id: str | None,
    ) -> Event:
        existing = self._find_event(
            competition_id=competition_id,
            home_team_id=home.id,
            away_team_id=away.id,
            start_time=start_time,
        )
        if existing is not None:
            # Same reasoning as match()'s source_event_id fast path: a
            # later sighting within start_time_tolerance of the stored
            # kickoff can still report a genuinely shifted start_time (a
            # provider moving a match by 15 minutes, say), and signal
            # expiry keys off events.start_time -- see
            # _sync_event_start_time.
            synced = self._sync_event_start_time(
                existing, start_time, source_event_id=source_event_id
            )
            self._promote_event_to_reference(synced.id, source_event_id)
            return synced

        event = Event(
            id=f"event-{uuid4().hex[:10]}",
            sport=sport,
            league=league,
            competition_id=competition_id,
            home_team=home,
            away_team=away,
            start_time=start_time,
        )
        is_reference_event = False
        if self._provider_id == REFERENCE_PROVIDER_ID and source_event_id is not None:
            is_reference_event = (
                self._connection.execute(
                    """
                    SELECT 1 FROM events
                    WHERE reference_provider = ? AND reference_provider_id = ?
                    """,
                    (REFERENCE_PROVIDER_ID, source_event_id),
                ).fetchone()
                is None
            )
        self._connection.execute(
            """
            INSERT INTO events (
                id, sport, league, competition_id, home_team_id, away_team_id,
                start_time, start_time_provider, reference_provider,
                reference_provider_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.sport,
                event.league,
                event.competition_id,
                home.id,
                away.id,
                to_utc_iso(event.start_time),
                self._provider_id,
                (
                    REFERENCE_PROVIDER_ID
                    if is_reference_event
                    else None
                ),
                (
                    source_event_id
                    if is_reference_event
                    else None
                ),
            ),
        )
        logger.info(
            "fixture_catalog.event.created",
            extra={"event_id": event.id, "display_name": event.display_name},
        )
        return event

    def _sync_event_start_time(
        self,
        event: Event,
        new_start_time: datetime,
        *,
        source_event_id: str | None,
    ) -> Event:
        """Updates an already-resolved event's start_time in place when a
        later sighting reports a different one -- a provider genuinely
        rescheduling a fixture's kickoff, not corrected retroactively
        (see both call sites above). Team/competition identity and the
        event's own id are exactly what makes this the *same* canonical
        event despite the new kickoff; only start_time itself is mutable
        here, deliberately not a broader "any field can drift" policy --
        a changed league/competition on an otherwise-matching sighting is
        more likely a genuinely different fixture than the same one
        rescheduled, and isn't something this method touches.

        No-op (no write, no log) when the two times are already equal
        under UTC-normalized comparison, so a routine re-poll of an
        unchanged fixture -- the overwhelming majority of calls here --
        never writes to events at all.
        """
        if to_utc_iso(event.start_time) == to_utc_iso(new_start_time):
            return event

        authority = self._connection.execute(
            """
            SELECT start_time_provider, reference_provider
            FROM events WHERE id = ?
            """,
            (event.id,),
        ).fetchone()
        if authority is not None:
            reference_provider = authority["reference_provider"]
            start_time_provider = authority["start_time_provider"]
            may_update = (
                self._provider_id == REFERENCE_PROVIDER_ID
                or (
                    reference_provider is None
                    and (start_time_provider is None or start_time_provider == self._provider_id)
                )
                or reference_provider == self._provider_id
            )
            if not may_update:
                logger.info(
                    "fixture_catalog.event.start_time_ignored_non_authoritative",
                    extra={
                        "event_id": event.id,
                        "provider_id": self._provider_id,
                        "reference_provider": reference_provider,
                    },
                )
                return event

        self._connection.execute(
            """
            UPDATE events
            SET start_time = ?, start_time_provider = ?,
                reference_provider = CASE WHEN ? THEN ? ELSE reference_provider END,
                reference_provider_id = CASE WHEN ? THEN ? ELSE reference_provider_id END
            WHERE id = ?
            """,
            (
                to_utc_iso(new_start_time),
                self._provider_id,
                self._provider_id == REFERENCE_PROVIDER_ID and source_event_id is not None,
                REFERENCE_PROVIDER_ID,
                self._provider_id == REFERENCE_PROVIDER_ID and source_event_id is not None,
                source_event_id,
                event.id,
            ),
        )
        logger.info(
            "fixture_catalog.event.start_time_rescheduled",
            extra={
                "event_id": event.id,
                "display_name": event.display_name,
                "previous_start_time": to_utc_iso(event.start_time),
                "new_start_time": to_utc_iso(new_start_time),
            },
        )
        return replace(event, start_time=new_start_time)

    def _promote_event_to_reference(
        self, event_id: str, source_event_id: str | None
    ) -> None:
        if self._provider_id != REFERENCE_PROVIDER_ID or source_event_id is None:
            return
        self._connection.execute(
            """
            UPDATE events
            SET reference_provider = ?, reference_provider_id = ?,
                start_time_provider = ?
            WHERE id = ?
              AND (reference_provider IS NULL OR reference_provider = ?)
              AND NOT EXISTS (
                  SELECT 1 FROM events existing
                  WHERE existing.reference_provider = ?
                    AND existing.reference_provider_id = ?
                    AND existing.id != ?
              )
            """,
            (
                REFERENCE_PROVIDER_ID,
                source_event_id,
                REFERENCE_PROVIDER_ID,
                event_id,
                REFERENCE_PROVIDER_ID,
                REFERENCE_PROVIDER_ID,
                source_event_id,
                event_id,
            ),
        )

    def _find_event_mapping(self, source_event_id: str) -> _EventMapping | None:
        row = self._connection.execute(
            """
            SELECT e.*, m.source, m.source_event_id,
                   m.trust_state, m.resolution_method, m.resolver_version,
                   m.sport AS mapping_sport,
                   m.last_verified_home_name, m.last_verified_away_name,
                   m.last_verified_competition_name, m.last_verified_country,
                   m.home_source_team_id, m.away_source_team_id,
                   m.source_competition_id, m.verified_at,
                   m.last_checked_at, m.drift_detected_at, m.created_at
            FROM source_event_mappings m
            JOIN events e ON e.id = m.event_id
            WHERE m.source = ? AND m.source_event_id = ?
            """,
            (self._provider_id, source_event_id),
        ).fetchone()
        return _EventMapping(self._map_event_row(row), row) if row else None

    def _save_event_mapping(
        self,
        source_event_id: str,
        event_id: str,
        *,
        trust_state: str,
        resolution_method: str,
        sport: str,
        league: str,
        home_raw: str,
        away_raw: str,
        country: str | None,
        home_team_source_id: str | None,
        away_team_source_id: str | None,
        competition_source_id: str | None,
    ) -> None:
        now = to_utc_iso(datetime.now(UTC))
        self._connection.execute(
            """
            INSERT INTO source_event_mappings (
                source, source_event_id, event_id, trust_state,
                resolution_method, resolver_version, sport,
                last_verified_home_name, last_verified_away_name,
                last_verified_competition_name, last_verified_country,
                home_source_team_id, away_source_team_id,
                source_competition_id, verified_at, last_checked_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, source_event_id) DO UPDATE SET
                event_id = CASE
                    WHEN source_event_mappings.trust_state = 'SUSPECT'
                    THEN source_event_mappings.event_id ELSE excluded.event_id END,
                trust_state = CASE
                    WHEN source_event_mappings.trust_state = 'SUSPECT'
                    THEN source_event_mappings.trust_state ELSE excluded.trust_state END,
                resolution_method = CASE
                    WHEN source_event_mappings.trust_state = 'SUSPECT'
                    THEN source_event_mappings.resolution_method
                    ELSE excluded.resolution_method END,
                resolver_version = excluded.resolver_version,
                sport = excluded.sport,
                last_verified_home_name = CASE
                    WHEN excluded.trust_state = 'VERIFIED'
                    THEN excluded.last_verified_home_name
                    ELSE source_event_mappings.last_verified_home_name END,
                last_verified_away_name = CASE
                    WHEN excluded.trust_state = 'VERIFIED'
                    THEN excluded.last_verified_away_name
                    ELSE source_event_mappings.last_verified_away_name END,
                last_verified_competition_name = CASE
                    WHEN excluded.trust_state = 'VERIFIED'
                    THEN excluded.last_verified_competition_name
                    ELSE source_event_mappings.last_verified_competition_name END,
                last_verified_country = CASE
                    WHEN excluded.trust_state = 'VERIFIED'
                    THEN excluded.last_verified_country
                    ELSE source_event_mappings.last_verified_country END,
                home_source_team_id = COALESCE(
                    excluded.home_source_team_id,
                    source_event_mappings.home_source_team_id
                ),
                away_source_team_id = COALESCE(
                    excluded.away_source_team_id,
                    source_event_mappings.away_source_team_id
                ),
                source_competition_id = COALESCE(
                    excluded.source_competition_id,
                    source_event_mappings.source_competition_id
                ),
                verified_at = CASE WHEN excluded.trust_state = 'VERIFIED'
                    THEN excluded.verified_at ELSE source_event_mappings.verified_at END,
                last_checked_at = excluded.last_checked_at
            """,
            (
                self._provider_id,
                source_event_id,
                event_id,
                trust_state,
                resolution_method,
                RESOLVER_VERSION,
                sport,
                home_raw if trust_state == TRUST_VERIFIED else None,
                away_raw if trust_state == TRUST_VERIFIED else None,
                league if trust_state == TRUST_VERIFIED else None,
                country if trust_state == TRUST_VERIFIED else None,
                home_team_source_id,
                away_team_source_id,
                competition_source_id,
                now if trust_state == TRUST_VERIFIED else None,
                now,
                now,
            ),
        )

    def _event_mapping_is_compatible(
        self,
        mapping: _EventMapping,
        *,
        sport: str,
        league: str,
        home_raw: str,
        away_raw: str,
        country: str | None,
        home_team_source_id: str | None,
        away_team_source_id: str | None,
        competition_source_id: str | None,
    ) -> bool:
        row = mapping.row
        if row["mapping_sport"] is not None and row["mapping_sport"] != sport:
            return False

        stored_pairs = (
            (row["home_source_team_id"], home_team_source_id),
            (row["away_source_team_id"], away_team_source_id),
            (row["source_competition_id"], competition_source_id),
        )
        if any(stored and incoming and stored != incoming for stored, incoming in stored_pairs):
            return False

        name_pairs = (
            (
                row["last_verified_home_name"],
                home_raw,
                row["home_source_team_id"],
                home_team_source_id,
            ),
            (
                row["last_verified_away_name"],
                away_raw,
                row["away_source_team_id"],
                away_team_source_id,
            ),
            (
                row["last_verified_competition_name"],
                league,
                row["source_competition_id"],
                competition_source_id,
            ),
        )
        if any(
            stored is not None
            and not (stored_id is not None and stored_id == incoming_id)
            and not names_are_similar(
                stored, incoming, fuzzy_threshold=self._fuzzy_threshold
            )
            for stored, incoming, stored_id, incoming_id in name_pairs
        ):
            return False

        stored_country = row["last_verified_country"]
        if (
            stored_country is not None
            and country is not None
            and _country_key(stored_country) != _country_key(country)
        ):
            return False

        if home_team_source_id is not None:
            home_id_hit = self._find_team_by_source_id(home_team_source_id, sport)
            if (
                home_id_hit is not None
                and home_id_hit[1]["trust_state"] == TRUST_VERIFIED
                and home_id_hit[0].id != mapping.event.home_team.id
            ):
                return False
        if away_team_source_id is not None:
            away_id_hit = self._find_team_by_source_id(away_team_source_id, sport)
            if (
                away_id_hit is not None
                and away_id_hit[1]["trust_state"] == TRUST_VERIFIED
                and away_id_hit[0].id != mapping.event.away_team.id
            ):
                return False
        if competition_source_id is not None:
            competition_id_hit = self._find_competition_by_source_id(
                competition_source_id, sport
            )
            if (
                competition_id_hit is not None
                and competition_id_hit[2]["trust_state"] == TRUST_VERIFIED
                and competition_id_hit[1] != mapping.event.competition_id
            ):
                return False

        home_name_mapping = self._find_mapping(
            home_raw, sport, mapping.event.competition_id
        )
        if home_name_mapping is not None and home_name_mapping.id != mapping.event.home_team.id:
            return False
        away_name_mapping = self._find_mapping(
            away_raw, sport, mapping.event.competition_id
        )
        if away_name_mapping is not None and away_name_mapping.id != mapping.event.away_team.id:
            return False
        competition_name_mapping = self._find_competition_mapping(
            league, sport, country
        )
        return not (
            competition_name_mapping is not None
            and competition_name_mapping[1] != mapping.event.competition_id
        )

    def _learn_identity_from_event_mapping(
        self,
        event: Event,
        *,
        sport: str,
        league: str,
        home_raw: str,
        away_raw: str,
        country: str | None,
        home_team_source_id: str | None,
        away_team_source_id: str | None,
        competition_source_id: str | None,
    ) -> None:
        self._learn_identity_from_event_context(
            event,
            sport=sport,
            competition_id=event.competition_id,
            league=league,
            home_raw=home_raw,
            away_raw=away_raw,
            country=country,
            home_team_source_id=home_team_source_id,
            away_team_source_id=away_team_source_id,
            competition_source_id=competition_source_id,
        )

    def _learn_identity_from_event_context(
        self,
        event: Event,
        *,
        sport: str,
        competition_id: str,
        league: str,
        home_raw: str,
        away_raw: str,
        country: str | None,
        home_team_source_id: str | None,
        away_team_source_id: str | None,
        competition_source_id: str | None,
    ) -> None:
        for raw_name, team, source_team_id in (
            (home_raw, event.home_team, home_team_source_id),
            (away_raw, event.away_team, away_team_source_id),
        ):
            self._save_mapping(
                raw_name,
                sport,
                competition_id,
                team.id,
                resolution_method="provider_event_context",
                confidence=100.0,
                trust_state=TRUST_VERIFIED,
            )
            if source_team_id is not None:
                self._save_team_id_mapping(
                    source_team_id,
                    sport,
                    team.id,
                    raw_name,
                    trust_state=TRUST_VERIFIED,
                    resolution_method="provider_event_context",
                )
                if self._provider_id == REFERENCE_PROVIDER_ID:
                    self._promote_team_to_reference(team.id, sport, source_team_id)
            self._record_team_competition(team.id, competition_id)

        self._save_competition_mapping(
            league,
            sport,
            country,
            event.competition_id,
            resolution_method="provider_event_context",
            confidence=100.0,
            trust_state=TRUST_VERIFIED,
        )
        if competition_source_id is not None:
            self._save_competition_id_mapping(
                competition_source_id,
                sport,
                event.competition_id,
                league,
                trust_state=TRUST_VERIFIED,
                resolution_method="provider_event_context",
            )
            if self._provider_id == REFERENCE_PROVIDER_ID:
                self._promote_competition_to_reference(
                    event.competition_id,
                    sport,
                    competition_source_id,
                    country,
                )

    def _refresh_event_mapping(
        self,
        source_event_id: str,
        *,
        sport: str,
        league: str,
        home_raw: str,
        away_raw: str,
        country: str | None,
        home_team_source_id: str | None,
        away_team_source_id: str | None,
        competition_source_id: str | None,
    ) -> None:
        now = to_utc_iso(datetime.now(UTC))
        self._connection.execute(
            """
            UPDATE source_event_mappings
            SET resolver_version = ?, sport = ?,
                last_verified_home_name = ?, last_verified_away_name = ?,
                last_verified_competition_name = ?, last_verified_country = ?,
                home_source_team_id = COALESCE(?, home_source_team_id),
                away_source_team_id = COALESCE(?, away_source_team_id),
                source_competition_id = COALESCE(?, source_competition_id),
                verified_at = ?, last_checked_at = ?
            WHERE source = ? AND source_event_id = ? AND trust_state = 'VERIFIED'
            """,
            (
                RESOLVER_VERSION,
                sport,
                home_raw,
                away_raw,
                league,
                country,
                home_team_source_id,
                away_team_source_id,
                competition_source_id,
                now,
                now,
                self._provider_id,
                source_event_id,
            ),
        )

    def _mark_event_mapping_suspect(self, source_event_id: str) -> None:
        now = to_utc_iso(datetime.now(UTC))
        self._connection.execute(
            """
            UPDATE source_event_mappings
            SET trust_state = 'SUSPECT', last_checked_at = ?, drift_detected_at = ?
            WHERE source = ? AND source_event_id = ?
            """,
            (now, now, self._provider_id, source_event_id),
        )
        logger.warning(
            "fixture_catalog.event_id.identity_drift",
            extra={"source_event_id": source_event_id},
        )

    @staticmethod
    def _trust_for_method(method: str) -> str:
        return (
            TRUST_UNVERIFIED
            if method in {"fuzzy", "ambiguous"}
            else TRUST_VERIFIED
        )

    def _find_event(
        self, *, competition_id: str, home_team_id: str, away_team_id: str, start_time: datetime
    ) -> Event | None:
        # competition_id is part of an event's identity, not just
        # descriptive metadata: the same two teams can play each other
        # in more than one competition (league + cup, or two age groups)
        # within the start_time tolerance window, and those must not be
        # merged into one canonical event just because the team IDs and
        # kickoff time happen to line up. Keyed on the competition's
        # stable id rather than its canonical_name display string so
        # that renaming a competition later (not currently exposed, but
        # the id already supports it) can't silently detach existing
        # events from future matches under the new name.
        # start_time is normalized to UTC the same way OddsSnapshot's
        # timestamps are (see storage.time_utils.to_utc_iso) -- without
        # this, two sources reporting the same real kickoff under
        # different but equally valid offsets (+00:00 vs +02:00) could
        # fail to resolve to the same canonical event, since events.
        # start_time and the BETWEEN bounds below would be compared as
        # plain, differently-offset text.
        lower = to_utc_iso(start_time - self._start_time_tolerance)
        upper = to_utc_iso(start_time + self._start_time_tolerance)
        rows = self._connection.execute(
            """
            SELECT * FROM events
            WHERE competition_id = ? AND home_team_id = ? AND away_team_id = ?
              AND start_time BETWEEN ? AND ?
            ORDER BY ABS(julianday(start_time) - julianday(?))
            LIMIT 2
            """,
            (competition_id, home_team_id, away_team_id, lower, upper, to_utc_iso(start_time)),
        ).fetchall()
        if len(rows) > 1:
            logger.warning(
                "fixture_catalog.event.ambiguous_exact_candidates",
                extra={
                    "competition_id": competition_id,
                    "home_team_id": home_team_id,
                    "away_team_id": away_team_id,
                    "start_time": to_utc_iso(start_time),
                },
            )
            return None
        return self._map_event_row(rows[0]) if rows else None

    def _map_team_row(self, row: Row) -> Team:
        return Team(id=row["id"], canonical_name=row["canonical_name"])

    def _map_event_row(self, row: Row) -> Event:
        return Event(
            id=row["id"],
            sport=row["sport"],
            league=row["league"],
            competition_id=row["competition_id"],
            home_team=self._get_team_by_id(row["home_team_id"]),
            away_team=self._get_team_by_id(row["away_team_id"]),
            start_time=datetime.fromisoformat(row["start_time"]),
        )

    def _get_team_by_id(self, team_id: str) -> Team:
        row = self._connection.execute(
            "SELECT * FROM teams WHERE id = ?", (team_id,)
        ).fetchone()
        return self._map_team_row(row)
