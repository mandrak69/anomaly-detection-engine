from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from anomaly_detection_engine.models.event import Event
from anomaly_detection_engine.normalization.team_normalizer import TeamNormalizer


@dataclass(frozen=True)
class EventMatchResult:
    event: Event | None
    confidence: float
    reason: str


class EventResolver(Protocol):
    """The match(...) -> EventMatchResult shape OddsIngestionService
    actually depends on -- both EventMatcher (below) and
    storage.fixture_catalog.FixtureCatalog implement it, but only
    duck-typed the same shape rather than one declaring it. Naming it
    here lets OddsIngestionService type-hint what it actually needs
    (any resolver with this shape) instead of a concrete EventMatcher
    it is often not actually given -- app.py's real ingestion path
    always passes a FixtureCatalog, not an EventMatcher.
    """

    def match(
        self,
        *,
        sport: str,
        league: str,
        home_team_raw: str,
        away_team_raw: str,
        start_time: datetime,
    ) -> EventMatchResult: ...


class EventMatcher:
    def __init__(
        self,
        events: Iterable[Event],
        team_normalizer: TeamNormalizer,
        start_time_tolerance: timedelta = timedelta(minutes=30),
    ) -> None:
        self._events = list(events)
        self._team_normalizer = team_normalizer
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
        home = self._team_normalizer.normalize(home_team_raw)
        away = self._team_normalizer.normalize(away_team_raw)

        if home.canonical_name is None or away.canonical_name is None:
            return EventMatchResult(None, 0.0, "team-normalization-failed")

        candidates = [
            event
            for event in self._events
            if event.sport == sport
            and event.league == league
            and event.home_team.canonical_name == home.canonical_name
            and event.away_team.canonical_name == away.canonical_name
            and abs(event.start_time - start_time) <= self._start_time_tolerance
        ]

        if len(candidates) == 1:
            confidence = min(home.confidence, away.confidence)
            return EventMatchResult(candidates[0], confidence, "matched")

        if len(candidates) > 1:
            candidate_ids = ", ".join(candidate.id for candidate in candidates)
            return EventMatchResult(
                None, 0.0, f"ambiguous-event: {candidate_ids}"
            )

        return EventMatchResult(None, 0.0, "no-event-match")
