import sqlite3
from datetime import datetime

from anomaly_detection_engine.models.market import EventLifecycle
from anomaly_detection_engine.storage.database import configure_connection, initialize_database
from anomaly_detection_engine.storage.event_status_repository import EventStatusRepository

T0 = datetime.fromisoformat("2026-09-01T20:00:00+00:00")


def make_repository() -> EventStatusRepository:
    connection = sqlite3.connect(":memory:")
    configure_connection(connection)
    initialize_database(connection)
    return EventStatusRepository(connection)


def test_get_many_returns_empty_for_unknown_events():
    repository = make_repository()
    assert repository.get_many(["event-1", "event-2"]) == {}


def test_get_many_returns_empty_for_an_empty_list_without_querying():
    repository = make_repository()
    assert repository.get_many([]) == {}


def test_update_then_get_many_returns_the_stored_lifecycle():
    repository = make_repository()
    repository.update(event_id="event-1", lifecycle=EventLifecycle.LIVE, updated_at=T0)

    assert repository.get_many(["event-1"]) == {"event-1": EventLifecycle.LIVE}


def test_update_overwrites_a_previous_lifecycle_for_the_same_event():
    repository = make_repository()
    repository.update(event_id="event-1", lifecycle=EventLifecycle.SCHEDULED, updated_at=T0)
    repository.update(event_id="event-1", lifecycle=EventLifecycle.FINISHED, updated_at=T0)

    assert repository.get_many(["event-1"]) == {"event-1": EventLifecycle.FINISHED}


def test_get_many_only_returns_requested_events_present_in_the_table():
    repository = make_repository()
    repository.update(event_id="event-1", lifecycle=EventLifecycle.LIVE, updated_at=T0)
    repository.update(event_id="event-2", lifecycle=EventLifecycle.FINISHED, updated_at=T0)

    result = repository.get_many(["event-1", "event-3"])

    assert result == {"event-1": EventLifecycle.LIVE}
