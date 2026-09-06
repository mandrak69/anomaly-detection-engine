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
    """Persistent, auto-growing canonical event/team registry.

    Unlike EventMatcher (a fixed, in-memory candidate list that rejects
    anything it doesn't already know), this resolves-or-creates: a team
    or event it hasn't seen before gets a new canonical row instead of
    being rejected. Every (source, sport, raw team name) resolution is
    cached permanently in source_team_mappings so it is never re-solved
    on a later run -- that cache is what lets two different sources
    reporting the same real match under different team-name spellings
    ("Man Utd" vs "Manchester United") end up sharing one canonical
    Event, once that particular spelling has been resolved once (by
    exact/alias/fuzzy match against the existing catalog, or simply
    replayed from a prior run's mapping).

    Implements the same match(...) -> EventMatchResult shape as
    EventMatcher, so OddsIngestionService can use either interchangeably
    -- construct one FixtureCatalog per collector/source (the `source`
    label is fixed at construction, matching how OddsIngestionService is
    already constructed once per collector).

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
        source: str,
        aliases: dict[str, str] | None = None,
        fuzzy_threshold: float = 85.0,
        fuzzy_ambiguity_margin: float = 5.0,
        start_time_tolerance: timedelta = timedelta(minutes=30),
    ) -> None:
        self._connection = connection
        self._source = source
        self._aliases = aliases or {}
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

        home, home_confidence = self._resolve_team(raw_name=home_raw, sport=sport)
        away, away_confidence = self._resolve_team(raw_name=away_raw, sport=sport)

        event = self._resolve_event(
            sport=sport,
            league=league,
            home=home,
            away=away,
            start_time=start_time,
        )

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
            (self._source, sport, raw_name),
        ).fetchone()
        return self._map_team_row(row) if row else None

    def _save_mapping(self, raw_name: str, sport: str, team_id: str) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO source_team_mappings
                (source, sport, source_team_name, team_id)
            VALUES (?, ?, ?, ?)
            """,
            (self._source, sport, raw_name, team_id),
        )
        self._connection.commit()

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
        self._connection.commit()
        logger.info(
            "fixture_catalog.team.created",
            extra={"team_id": team.id, "canonical_name": team.canonical_name, "sport": sport},
        )
        return team

    # -- event resolution --------------------------------------------------

    def _resolve_event(
        self, *, sport: str, league: str, home: Team, away: Team, start_time: datetime
    ) -> Event:
        existing = self._find_event(
            league=league, home_team_id=home.id, away_team_id=away.id, start_time=start_time
        )
        if existing is not None:
            return existing

        event = Event(
            id=f"event-{uuid4().hex[:10]}",
            sport=sport,
            league=league,
            home_team=home,
            away_team=away,
            start_time=start_time,
        )
        self._connection.execute(
            """
            INSERT INTO events (id, sport, league, home_team_id, away_team_id, start_time)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.sport,
                event.league,
                home.id,
                away.id,
                to_utc_iso(event.start_time),
            ),
        )
        self._connection.commit()
        logger.info(
            "fixture_catalog.event.created",
            extra={"event_id": event.id, "display_name": event.display_name},
        )
        return event

    def _find_event(
        self, *, league: str, home_team_id: str, away_team_id: str, start_time: datetime
    ) -> Event | None:
        # league is part of an event's identity, not just descriptive
        # metadata: the same two teams can play each other in more than
        # one competition (league + cup, or two age groups) within the
        # start_time tolerance window, and those must not be merged into
        # one canonical event just because the team IDs and kickoff time
        # happen to line up.
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
            WHERE league = ? AND home_team_id = ? AND away_team_id = ?
              AND start_time BETWEEN ? AND ?
            ORDER BY ABS(julianday(start_time) - julianday(?))
            LIMIT 1
            """,
            (league, home_team_id, away_team_id, lower, upper, to_utc_iso(start_time)),
        ).fetchone()
        return self._map_event_row(row) if row else None

    def _map_team_row(self, row: Row) -> Team:
        return Team(id=row["id"], canonical_name=row["canonical_name"])

    def _map_event_row(self, row: Row) -> Event:
        return Event(
            id=row["id"],
            sport=row["sport"],
            league=row["league"],
            home_team=self._get_team_by_id(row["home_team_id"]),
            away_team=self._get_team_by_id(row["away_team_id"]),
            start_time=datetime.fromisoformat(row["start_time"]),
        )

    def _get_team_by_id(self, team_id: str) -> Team:
        row = self._connection.execute(
            "SELECT * FROM teams WHERE id = ?", (team_id,)
        ).fetchone()
        return self._map_team_row(row)
