from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.models.market import MarketIdentity


@dataclass(frozen=True)
class Bookmaker:
    id: str
    name: str


@dataclass(frozen=True)
class OddsSnapshot:
    event_id: str
    bookmaker: Bookmaker
    market: MarketIdentity
    outcome: str
    odds: Decimal
    observed_at: datetime
    source_timestamp: datetime | None = None
    # Which CollectorRun produced this snapshot -- storage.collector_run_
    # repository.CollectorRun.id, not enforced as a SQL foreign key (see
    # migration 8: the row would need to exist before
    # OddsIngestionService.run() even generates its run_id, since the
    # CollectorRun itself is only persisted at the very end of the run).
    # None for a snapshot saved directly rather than through
    # OddsIngestionService (tests, historical pre-migration-8 data) --
    # its provider is then "unknown", not "no provider"; see
    # OddsRepository.find_last_two_same_provider for how that is
    # handled.
    collector_run_id: str | None = None

    def __post_init__(self) -> None:
        if self.odds <= Decimal("1.0"):
            raise ValueError("Decimal odds must be greater than 1.0")

    @property
    def quote_time(self) -> datetime:
        """When this price actually was/is current, for freshness
        purposes -- source_timestamp if the source provided one (e.g.
        the-odds-api's per-bookmaker last_update), otherwise observed_at.

        observed_at is when *we* saw the data, not when the quote itself
        was last current -- a poll can retrieve a response the source
        computed or cached well before the request. A source-reported
        last_update of 14:00 fetched by us at 16:00 is a two-hour-old
        quote regardless of how quickly our own poll completed, so
        freshness must be measured against quote_time, not observed_at.
        """
        return self.source_timestamp or self.observed_at
