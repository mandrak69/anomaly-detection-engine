import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot
from anomaly_detection_engine.storage.database import initialize_database
from anomaly_detection_engine.storage.odds_repository import OddsRepository

MARKET = MarketIdentity(
    market_type=MarketType.THREE_WAY,
    period=MarketPeriod.FULL_TIME,
)


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
        market=MARKET,
        outcome="1",
        odds=Decimal("2.15"),
        observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
    )

    repository.save(snapshot)

    row = connection.execute(
        "SELECT * FROM odds_snapshots"
    ).fetchone()

    assert row is not None
    assert row["event_id"] == "event-001"
    assert row["bookmaker_id"] == "mozzart"
    assert Decimal(row["odds"]) == Decimal("2.15")


def test_saving_the_same_snapshot_twice_is_idempotent():
    connection = create_test_connection()
    repository = OddsRepository(connection)

    snapshot = OddsSnapshot(
        event_id="event-001",
        bookmaker=Bookmaker("mozzart", "Mozzart"),
        market=MARKET,
        outcome="1",
        odds=Decimal("2.15"),
        observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
    )

    repository.save(snapshot)
    repository.save(snapshot)

    rows = connection.execute("SELECT * FROM odds_snapshots").fetchall()
    assert len(rows) == 1


def test_saving_a_changed_odds_value_at_the_same_timestamp_is_still_deduped():
    connection = create_test_connection()
    repository = OddsRepository(connection)

    observed_at = datetime.fromisoformat("2026-08-27T08:00:00+00:00")
    bookmaker = Bookmaker("mozzart", "Mozzart")

    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market=MARKET,
            outcome="1",
            odds=Decimal("2.15"),
            observed_at=observed_at,
        )
    )
    # Same identity (event/bookmaker/market/outcome/observed_at) but a
    # different odds value -- this is what a duplicated/retried collector
    # payload for the same poll would look like, and must not create a
    # second row.
    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market=MARKET,
            outcome="1",
            odds=Decimal("2.20"),
            observed_at=observed_at,
        )
    )

    rows = connection.execute("SELECT * FROM odds_snapshots").fetchall()
    assert len(rows) == 1
    assert Decimal(rows[0]["odds"]) == Decimal("2.15")


def test_snapshots_differing_only_in_market_specifier_are_not_deduped_together():
    # Two genuinely different markets (see models.market.MarketIdentity)
    # that happen to share type/period/line must not collapse into one
    # row just because the old unique index ignored rules/specifier.
    connection = create_test_connection()
    repository = OddsRepository(connection)

    observed_at = datetime.fromisoformat("2026-08-27T08:00:00+00:00")
    bookmaker = Bookmaker("mozzart", "Mozzart")

    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market=MarketIdentity(
                market_type=MarketType.HANDICAP,
                period=MarketPeriod.FULL_TIME,
                specifier="home",
            ),
            outcome="1",
            odds=Decimal("2.15"),
            observed_at=observed_at,
        )
    )
    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market=MarketIdentity(
                market_type=MarketType.HANDICAP,
                period=MarketPeriod.FULL_TIME,
                specifier="away",
            ),
            outcome="1",
            odds=Decimal("1.80"),
            observed_at=observed_at,
        )
    )

    rows = connection.execute("SELECT * FROM odds_snapshots").fetchall()
    assert len(rows) == 2


def test_save_all_persists_every_snapshot_in_one_call():
    connection = create_test_connection()
    repository = OddsRepository(connection)

    bookmaker = Bookmaker("mozzart", "Mozzart")
    observed_at = datetime.fromisoformat("2026-08-27T08:00:00+00:00")

    repository.save_all(
        [
            OddsSnapshot(
                event_id="event-001",
                bookmaker=bookmaker,
                market=MARKET,
                outcome=outcome,
                odds=Decimal(odds),
                observed_at=observed_at,
            )
            for outcome, odds in [("1", "2.15"), ("X", "3.45"), ("2", "3.20")]
        ]
    )

    rows = connection.execute("SELECT * FROM odds_snapshots").fetchall()
    assert len(rows) == 3


def test_observed_at_is_normalized_to_utc_regardless_of_source_offset():
    # Two equally-valid ISO timestamps for the exact same instant, saved
    # under different offsets -- if storage didn't normalize to UTC,
    # lexicographic ORDER BY on the raw text could sort them incorrectly
    # against a third, later snapshot.
    connection = create_test_connection()
    repository = OddsRepository(connection)
    bookmaker = Bookmaker("mozzart", "Mozzart")

    plus_two = datetime.fromisoformat("2026-08-27T10:00:00+02:00")
    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market=MARKET,
            outcome="1",
            odds=Decimal("2.15"),
            observed_at=plus_two,
        )
    )

    row = connection.execute("SELECT observed_at FROM odds_snapshots").fetchone()
    stored = datetime.fromisoformat(row["observed_at"])

    assert stored == plus_two.astimezone(timezone.utc)
    assert stored.utcoffset().total_seconds() == 0


def test_finds_all_snapshots_for_event():
    connection = create_test_connection()
    repository = OddsRepository(connection)

    bookmaker = Bookmaker("mozzart", "Mozzart")

    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market=MARKET,
            outcome="1",
            odds=Decimal("2.15"),
            observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
        )
    )

    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market=MARKET,
            outcome="1",
            odds=Decimal("1.95"),
            observed_at=datetime.fromisoformat("2026-08-27T08:05:00+00:00"),
        )
    )

    result = repository.find_by_event("event-001")

    assert len(result) == 2
    assert result[0].odds == Decimal("2.15")
    assert result[1].odds == Decimal("1.95")


def test_finds_latest_snapshot():
    connection = create_test_connection()
    repository = OddsRepository(connection)

    bookmaker = Bookmaker("mozzart", "Mozzart")

    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market=MARKET,
            outcome="1",
            odds=Decimal("2.15"),
            observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
        )
    )

    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market=MARKET,
            outcome="1",
            odds=Decimal("1.95"),
            observed_at=datetime.fromisoformat("2026-08-27T08:05:00+00:00"),
        )
    )

    result = repository.find_latest(
        event_id="event-001",
        bookmaker_id="mozzart",
        market=MARKET,
        outcome="1",
    )

    assert result is not None
    assert result.odds == Decimal("1.95")


def test_finds_last_two_snapshots_in_chronological_order():
    connection = create_test_connection()
    repository = OddsRepository(connection)

    bookmaker = Bookmaker("mozzart", "Mozzart")

    for odds, observed_at in [
        ("2.30", "2026-08-27T09:55:00+00:00"),
        ("2.20", "2026-08-27T10:00:00+00:00"),
        ("1.90", "2026-08-27T10:04:00+00:00"),
    ]:
        repository.save(
            OddsSnapshot(
                event_id="event-001",
                bookmaker=bookmaker,
                market=MARKET,
                outcome="1",
                odds=Decimal(odds),
                observed_at=datetime.fromisoformat(observed_at),
            )
        )

    result = repository.find_last_two(
        event_id="event-001",
        bookmaker_id="mozzart",
        market=MARKET,
        outcome="1",
    )

    assert len(result) == 2

    assert result[0].odds == Decimal("2.20")
    assert result[1].odds == Decimal("1.90")

    assert result[0].observed_at < result[1].observed_at


def test_find_last_two_returns_available_snapshots_when_only_one_exists():
    connection = create_test_connection()
    repository = OddsRepository(connection)

    bookmaker = Bookmaker("mozzart", "Mozzart")

    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market=MARKET,
            outcome="1",
            odds=Decimal("2.20"),
            observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
        )
    )

    result = repository.find_last_two(
        event_id="event-001",
        bookmaker_id="mozzart",
        market=MARKET,
        outcome="1",
    )

    assert len(result) == 1
    assert result[0].odds == Decimal("2.20")


def test_finds_latest_snapshot_for_each_bookmaker_and_outcome():
    connection = create_test_connection()
    repository = OddsRepository(connection)

    mozzart = Bookmaker("mozzart", "Mozzart")
    maxbet = Bookmaker("maxbet", "MaxBet")

    snapshots = [
        OddsSnapshot(
            event_id="event-001",
            bookmaker=mozzart,
            market=MARKET,
            outcome="1",
            odds=Decimal("2.20"),
            observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
        ),
        OddsSnapshot(
            event_id="event-001",
            bookmaker=mozzart,
            market=MARKET,
            outcome="1",
            odds=Decimal("2.10"),
            observed_at=datetime.fromisoformat("2026-08-27T08:05:00+00:00"),
        ),
        OddsSnapshot(
            event_id="event-001",
            bookmaker=mozzart,
            market=MARKET,
            outcome="X",
            odds=Decimal("3.40"),
            observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
        ),
        OddsSnapshot(
            event_id="event-001",
            bookmaker=maxbet,
            market=MARKET,
            outcome="1",
            odds=Decimal("2.15"),
            observed_at=datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
        ),
    ]

    for snapshot in snapshots:
        repository.save(snapshot)

    result = repository.find_latest_for_market(
        event_id="event-001",
        market=MARKET,
    )

    assert len(result) == 3

    mozzart_home = next(
        snapshot
        for snapshot in result
        if snapshot.bookmaker.id == "mozzart"
        and snapshot.outcome == "1"
    )

    assert mozzart_home.odds == Decimal("2.10")


def test_finds_latest_for_market_when_an_older_snapshot_is_inserted_after_a_newer_one():
    # Regression test for a MAX(observed_at)+MAX(id) join bug: if a
    # newer-observed_at row is inserted first (id=1) and an
    # older-observed_at row for the same (bookmaker, outcome) arrives
    # later (id=2), MAX(observed_at) and MAX(id) come from *different*
    # rows -- no row actually has both, so the old join silently dropped
    # this (bookmaker, outcome) out of the result entirely. The
    # ROW_NUMBER()-based query must still return the genuinely newest
    # (by observed_at) snapshot.
    connection = create_test_connection()
    repository = OddsRepository(connection)
    bookmaker = Bookmaker("mozzart", "Mozzart")

    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market=MARKET,
            outcome="1",
            odds=Decimal("2.20"),
            observed_at=datetime.fromisoformat("2026-08-27T12:00:00+00:00"),
        )
    )
    # Arrives later (higher id) but reports an *older* observed_at.
    repository.save(
        OddsSnapshot(
            event_id="event-001",
            bookmaker=bookmaker,
            market=MARKET,
            outcome="1",
            odds=Decimal("2.10"),
            observed_at=datetime.fromisoformat("2026-08-27T11:59:00+00:00"),
        )
    )

    result = repository.find_latest_for_market(
        event_id="event-001",
        market=MARKET,
    )

    assert len(result) == 1
    assert result[0].odds == Decimal("2.20")
