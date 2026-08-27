import sqlite3
from datetime import datetime

from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.storage.database import initialize_database
from anomaly_detection_engine.storage.odds_repository import OddsRepository


def create_test_connection():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    return connection

def test_saves_odds_snapshot():
    connection = create_test_connection()
    repository = OddsRepository(connection)

    snapshot = OddsSnapshot(
        event_id="event-001",
        bookmaker=Bookmaker("mozzart", "Mozzart"),
        market="1X2",
        outcome="1",
        odds=2.15,
        observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
    )

    repository.save(snapshot)

    row = connection.execute(
        "SELECT * FROM odds_snapshots"
    ).fetchone()

    assert row is not None
    assert row[1] == "event-001"
    assert row[2] == "mozzart"
    assert row[5] == 2.15

def test_finds_all_snapshots_for_event():
    connection = create_test_connection()
    repository = OddsRepository(connection)

    bookmaker = Bookmaker("mozzart", "Mozzart")

    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market="1X2",
            outcome="1",
            odds=2.15,
            observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
        )
    )

    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market="1X2",
            outcome="1",
            odds=1.95,
            observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
        )
    )

    result = repository.find_by_event("event-001")

    assert len(result) == 2
    assert result[0].odds == 2.15
    assert result[1].odds == 1.95

def test_finds_latest_snapshot():
    connection = create_test_connection()
    repository = OddsRepository(connection)

    bookmaker = Bookmaker("mozzart", "Mozzart")

    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market="1X2",
            outcome="1",
            odds=2.15,
            observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:000"),
        )
    )

    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market="1X2",
            outcome="1",
            odds=1.95,
            observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
        )
    )

    result = repository.find_latest(
        event_id="event-001",
        bookmaker_id="mozzart",
        market="1X2",
        outcome="1",
    )

    assert result is not None
    assert result.odds == 1.95

    def test_finds_last_two_snapshots_in_chronological_order():
    connection = create_test_connection()
    repository = OddsRepository(connection)

    bookmaker = Bookmaker("mozzart", "Mozzart")

    for odds, observed_at in [
        (2.30, "2026-08-27T09:55:00"),
        (2.20, "2026-08-27T10:00:00"),
        (1.90, "2026-08-27T10:04:00"),
    ]:
        repository.save(
            OddsSnapshot(
                event_id="event-001",
                bookmaker=bookmaker,
                market="1X2",
                outcome="1",
                odds=odds,
                observed_at=datetime.fromisoformat(observed_at),
            )
        )

    result = repository.find_last_two(
        event_id="event-001",
        bookmaker_id="mozzart",
        market="1X2",
        outcome="1",
    )

    assert len(result) == 2

    assert result[0].odds == 2.20
    assert result[1].odds == 1.90

    assert result[0].observed_at < result[1].observed_at

    def test_find_last_two_returns_available_snapshots_when_only_one_exists():
    connection = create_test_connection()
    repository = OddsRepository(connection)

    bookmaker = Bookmaker("mozzart", "Mozzart")

    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market="1X2",
            outcome="1",
            odds=2.20,
            observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
        )
    )

    result = repository.find_last_two(
        event_id="event-001",
        bookmaker_id="mozzart",
        market="1X2",
        outcome="1",
    )

    assert len(result) == 1
    assert result[0].odds == 2.20

    def test_finds_latest_snapshot_for_each_bookmaker_and_outcome():
    connection = create_test_connection()
    repository = OddsRepository(connection)

    mozzart = Bookmaker("mozzart", "Mozzart")
    maxbet = Bookmaker("maxbet", "MaxBet")

    snapshots = [
        OddsSnapshot(
            event_id="event-001",
            bookmaker=mozzart,
            market="1X2",
            outcome="1",
            odds=2.20,
            observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
        ),
        OddsSnapshot(
            event_id="event-001",
            bookmaker=mozzart,
            market="1X2",
            outcome="1",
            odds=2.10,
            observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:000"),
        ),
        OddsSnapshot(
            event_id="event-001",
            bookmaker=mozzart,
            market="1X2",
            outcome="X",
            odds=3.40,
            observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
        ),
        OddsSnapshot(
            event_id="event-001",
            bookmaker=maxbet,
            market="1X2",
            outcome="1",
            odds=2.15,
            observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
        ),
    ]

    for snapshot in snapshots:
        repository.save(snapshot)

    result = repository.find_latest_for_market(
        event_id="event-001",
        market="1X2",
    )

    assert len(result) == 3

    mozzart_home = next(
        snapshot
        for snapshot in result
        if snapshot.bookmaker.id == "mozzart"
        and snapshot.outcome == "1"
    )

    assert mozzart_home.odds == 2.10