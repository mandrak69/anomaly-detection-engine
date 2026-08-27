from datetime import datetime, timedelta

from anomaly_detection_engine.matching.event_matcher import EventMatcher
from anomaly_detection_engine.models.event import Event, Team
from anomaly_detection_engine.normalization.team_normalizer import TeamNormalizer


def build_matcher():
    event = Event(
        id="event-1",
        sport="football",
        league="premier-league",
        home_team=Team("team-1", "Manchester United"),
        away_team=Team("team-2", "Liverpool"),
        start_time=datetime.fromisoformat("2026-09-01T20:00:00"),
    )

    normalizer = TeamNormalizer(
        ["Manchester United", "Liverpool"],
        aliases={"Man Utd": "Manchester United", "Liv": "Liverpool"},
    )
    return EventMatcher([event], normalizer, timedelta(minutes=30))


def test_match_event_using_aliases_and_time_tolerance():
    matcher = build_matcher()

    result = matcher.match(
        sport="football",
        league="premier-league",
        home_team_raw="Man Utd",
        away_team_raw="Liv",
        start_time=datetime.fromisoformat("2026-09-01T20:15:00"),
    )

    assert result.event is not None
    assert result.event.id == "event-1"
    assert result.reason == "matched"


def test_does_not_match_wrong_league():
    matcher = build_matcher()

    result = matcher.match(
        sport="football",
        league="champions-league",
        home_team_raw="Man Utd",
        away_team_raw="Liv",
        start_time=datetime.fromisoformat("2026-09-01T20:00:00"),
    )

    assert result.event is None
    assert result.reason == "no-event-match"
