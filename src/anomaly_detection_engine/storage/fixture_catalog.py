import logging
from datetime import datetime, timedelta
from sqlite3 import Connection, Row
from uuid import uuid4

from anomaly_detection_engine.matching.event_matcher import EventMatchResult
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.normalization.team_normalizer import TeamNormalizer
from anomaly_detection_engine.storage.time_utils import to_utc_iso

logger = logging.getLogger(__name__)


class FixtureCatalog:
    """Persistent, auto-growing canonical event/team/competition registry.

    Unlike EventMatcher (a fixed, in-memory candidate list that rejects
    anything it doesn't already know), this resolves-or-creates: a team,
    competition, or event it hasn't seen before gets a new canonical row
    instead of being rejected. Every (source, sport, raw team name)
    resolution is cached permanently in source_team_mappings (and every
    (source, sport, raw league name) resolution in
    source_competition_mappings) so it is never re-solved on a later run
    -- that cache is what lets two different sources reporting the same
    real match under different team-name spellings ("Man Utd" vs
    "Manchester United") or league names ("Premier League" vs "England
    Premier League") end up sharing one canonical Event, once that
    particular spelling has been resolved once (by exact/alias/fuzzy
    match against the existing catalog, or simply replayed from a prior
    run's mapping).

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

    (The `source_team_mappings`/`source_competition_mappings` columns
    are still named `source` at the SQL level -- an internal storage
    detail kept as-is to avoid a schema migration for a rename with no
    external consumer; every Python-facing name here is provider_id.)

    Safe for multiple concurrent watch_capture.py-spawned processes to
    share one database file: match() wraps its whole read-then-maybe-
    create resolution (team, competition, event) in a single BEGIN
    IMMEDIATE transaction, so a second process attempting the same
    resolution blocks on SQLite's writer lock instead of racing through
    the same "does this already exist" check and creating a duplicate.

    Trade-off: a raw name that fuzzy-matches below fuzzy_threshold, or
    whose top two candidates score within fuzzy_ambiguity_margin of each
    other (see TeamNormalizer), becomes a brand-new canonical team rather
    than being merged into an existing one. That is the conservative
    choice -- a harmless near-duplicate team is better than silently
    merging two different real teams because a threshold was set too
    loose, or guessing between two candidates that were both plausible.
    """

    def __init__(
        self,
        connection: Connection,
        *,
        provider_id: str,
        aliases: dict[str, str] | None = None,
        league_aliases: dict[str, str] | None = None,
        fuzzy_threshold: float = 85.0,
        fuzzy_ambiguity_margin: float = 5.0,
        start_time_tolerance: timedelta = timedelta(minutes=30),
    ) -> None:
        self._connection = connection
        self._provider_id = provider_id
        self._aliases = aliases or {}
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
    ) -> EventMatchResult:
        home_raw = home_team_raw.strip()
        away_raw = away_team_raw.strip()

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
            home, home_confidence = self._resolve_team(raw_name=home_raw, sport=sport)
            away, away_confidence = self._resolve_team(raw_name=away_raw, sport=sport)
            canonical_league, competition_id = self._resolve_competition(
                raw_league=league.strip(), sport=sport
            )

            event = self._resolve_event(
                sport=sport,
                league=canonical_league,
                competition_id=competition_id,
                home=home,
                away=away,
                start_time=start_time,
            )
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

        return EventMatchResult(event, min(home_confidence, away_confidence), "resolved")

    def list_events(self, *, sport: str | None = None) -> list[Event]:
        if sport is None:
            rows = self._connection.execute("SELECT * FROM events").fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE sport = ?", (sport,)
            ).fetchall()
        return [self._map_event_row(row) for row in rows]

    # -- team resolution --------------------------------------------------

    def _resolve_team(self, *, raw_name: str, sport: str) -> tuple[Team, float]:
        mapped = self._find_mapping(raw_name, sport)
        if mapped is not None:
            return mapped, 100.0

        existing = self._teams_for_sport(sport)
        normalizer = TeamNormalizer(
            existing.keys(),
            aliases=self._aliases,
            fuzzy_threshold=self._fuzzy_threshold,
            ambiguity_margin=self._fuzzy_ambiguity_margin,
        )
        result = normalizer.normalize(raw_name)

        if result.method == "ambiguous":
            # Two existing teams scored too close together to safely pick
            # one (see TeamNormalizer) -- the conservative choice is the
            # same as "unknown": a new team under the raw name, not a
            # guessed merge into either candidate. Logged distinctly since
            # this is exactly the kind of borderline call worth a human
            # noticing, unlike a routine first-sighting.
            logger.warning(
                "fixture_catalog.team.ambiguous_fuzzy_match",
                extra={"raw_name": raw_name, "sport": sport, "score": result.confidence},
            )
            team = self._create_team(canonical_name=raw_name, sport=sport)
            confidence = result.confidence
        elif result.canonical_name is not None:
            # The alias/fuzzy target may not exist as a team row yet (e.g.
            # the first-ever sighting of this team arrives under an alias
            # like "Man Utd" -> "Manchester United") -- create it under
            # that canonical form, not under the raw name, so later exact
            # matches on the canonical name itself resolve correctly too.
            team = existing.get(result.canonical_name) or self._create_team(
                canonical_name=result.canonical_name, sport=sport
            )
            confidence = result.confidence
        else:
            team = self._create_team(canonical_name=raw_name, sport=sport)
            confidence = 100.0

        self._save_mapping(raw_name, sport, team.id)
        return team, confidence

    def _find_mapping(self, raw_name: str, sport: str) -> Team | None:
        row = self._connection.execute(
            """
            SELECT t.* FROM source_team_mappings m
            JOIN teams t ON t.id = m.team_id
            WHERE m.source = ? AND m.sport = ? AND m.source_team_name = ?
            """,
            (self._provider_id, sport, raw_name),
        ).fetchone()
        return self._map_team_row(row) if row else None

    def _save_mapping(self, raw_name: str, sport: str, team_id: str) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO source_team_mappings
                (source, sport, source_team_name, team_id)
            VALUES (?, ?, ?, ?)
            """,
            (self._provider_id, sport, raw_name, team_id),
        )

    def _teams_for_sport(self, sport: str) -> dict[str, Team]:
        rows = self._connection.execute(
            "SELECT * FROM teams WHERE sport = ?", (sport,)
        ).fetchall()
        return {row["canonical_name"]: self._map_team_row(row) for row in rows}

    def _create_team(self, *, canonical_name: str, sport: str) -> Team:
        team = Team(id=f"team-{uuid4().hex[:10]}", canonical_name=canonical_name)
        self._connection.execute(
            "INSERT INTO teams (id, canonical_name, sport) VALUES (?, ?, ?)",
            (team.id, team.canonical_name, sport),
        )
        logger.info(
            "fixture_catalog.team.created",
            extra={"team_id": team.id, "canonical_name": team.canonical_name, "sport": sport},
        )
        return team

    # -- competition resolution --------------------------------------------

    def _resolve_competition(self, *, raw_league: str, sport: str) -> tuple[str, str]:
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
        mapped = self._find_competition_mapping(raw_league, sport)
        if mapped is not None:
            return mapped

        existing = self._competitions_for_sport(sport)
        normalizer = TeamNormalizer(
            existing.keys(),
            aliases=self._league_aliases,
            fuzzy_threshold=self._fuzzy_threshold,
            ambiguity_margin=self._fuzzy_ambiguity_margin,
        )
        result = normalizer.normalize(raw_league)

        if result.method == "ambiguous":
            logger.warning(
                "fixture_catalog.competition.ambiguous_fuzzy_match",
                extra={"raw_league": raw_league, "sport": sport, "score": result.confidence},
            )
            canonical_name = raw_league
            competition_id = self._create_competition(canonical_name=canonical_name, sport=sport)
        elif result.canonical_name is not None:
            canonical_name = result.canonical_name
            competition_id = existing.get(canonical_name) or self._create_competition(
                canonical_name=canonical_name, sport=sport
            )
        else:
            canonical_name = raw_league
            competition_id = self._create_competition(canonical_name=canonical_name, sport=sport)

        self._save_competition_mapping(raw_league, sport, competition_id)
        return canonical_name, competition_id

    def _find_competition_mapping(self, raw_league: str, sport: str) -> tuple[str, str] | None:
        row = self._connection.execute(
            """
            SELECT c.canonical_name, c.id FROM source_competition_mappings m
            JOIN competitions c ON c.id = m.competition_id
            WHERE m.source = ? AND m.sport = ? AND m.source_competition_name = ?
            """,
            (self._provider_id, sport, raw_league),
        ).fetchone()
        return (row["canonical_name"], row["id"]) if row else None

    def _save_competition_mapping(self, raw_league: str, sport: str, competition_id: str) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO source_competition_mappings
                (source, sport, source_competition_name, competition_id)
            VALUES (?, ?, ?, ?)
            """,
            (self._provider_id, sport, raw_league, competition_id),
        )

    def _competitions_for_sport(self, sport: str) -> dict[str, str]:
        rows = self._connection.execute(
            "SELECT * FROM competitions WHERE sport = ?", (sport,)
        ).fetchall()
        return {row["canonical_name"]: row["id"] for row in rows}

    def _create_competition(self, *, canonical_name: str, sport: str) -> str:
        competition_id = f"competition-{uuid4().hex[:10]}"
        self._connection.execute(
            "INSERT INTO competitions (id, canonical_name, sport) VALUES (?, ?, ?)",
            (competition_id, canonical_name, sport),
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

    # -- event resolution --------------------------------------------------

    def _resolve_event(
        self,
        *,
        sport: str,
        league: str,
        competition_id: str,
        home: Team,
        away: Team,
        start_time: datetime,
    ) -> Event:
        existing = self._find_event(
            competition_id=competition_id,
            home_team_id=home.id,
            away_team_id=away.id,
            start_time=start_time,
        )
        if existing is not None:
            return existing

        event = Event(
            id=f"event-{uuid4().hex[:10]}",
            sport=sport,
            league=league,
            competition_id=competition_id,
            home_team=home,
            away_team=away,
            start_time=start_time,
        )
        self._connection.execute(
            """
            INSERT INTO events
                (id, sport, league, competition_id, home_team_id, away_team_id, start_time)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.sport,
                event.league,
                event.competition_id,
                home.id,
                away.id,
                to_utc_iso(event.start_time),
            ),
        )
        logger.info(
            "fixture_catalog.event.created",
            extra={"event_id": event.id, "display_name": event.display_name},
        )
        return event

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
        row = self._connection.execute(
            """
            SELECT * FROM events
            WHERE competition_id = ? AND home_team_id = ? AND away_team_id = ?
              AND start_time BETWEEN ? AND ?
            ORDER BY ABS(julianday(start_time) - julianday(?))
            LIMIT 1
            """,
            (competition_id, home_team_id, away_team_id, lower, upper, to_utc_iso(start_time)),
        ).fetchone()
        return self._map_event_row(row) if row else None

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
