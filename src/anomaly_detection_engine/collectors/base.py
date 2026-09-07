from abc import ABC, abstractmethod
from dataclasses import dataclass

from anomaly_detection_engine.models.raw_odds import RawEventOdds


@dataclass(frozen=True)
class CollectionResult:
    """What one collect() call actually produced: the parsed records
    ingestion already consumes, plus the exact source payload (the
    original bytes, decoded to text) they were parsed from.

    source_payload is None when there was genuinely nothing to collect
    this cycle (e.g. a manual-capture collector's drop file isn't there
    yet) -- never as a substitute for a parse failure, which still
    raises and propagates unchanged; a payload that failed to parse is
    not silently "collected" with no records. Persisting source_payload
    (see OddsIngestionService/CollectorRun) is what lets a parser bug be
    fixed later and this exact historical response reprocessed, instead
    of the RawEventOdds parsed from it at the time being the only thing
    that survives.
    """

    source_payload: str | None
    records: list[RawEventOdds]


class OddsCollector(ABC):

    @property
    @abstractmethod
    def source(self) -> str:
        """Identifies this collector instance for CollectorRun tracking
        and logging (e.g. "the-odds-api-manual:soccer_epl",
        "mozzart-file:<dir>") -- encodes the acquisition method/sport
        key too, so two collectors for the same real provider normally
        have different source values. See provider_id for the identity
        that should be shared between them instead.
        """

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Identifies the real-world data provider (e.g. "the-odds-api",
        "mozzart", "json-demo"), independent of acquisition method.
        Used as FixtureCatalog's mapping-cache key so auto and manual
        collectors for the same provider -- or multiple demo polls --
        share one team/competition mapping cache instead of each
        acquisition method building its own redundant one.
        """

    @property
    @abstractmethod
    def parser_version(self) -> str:
        """Identifies the version of this collector's own parsing logic
        (e.g. parse_the_odds_api_response, parse_mozzart_response, or
        the equivalent inline mapping in JsonOddsCollector) -- distinct
        from collector_version (OddsIngestionService's own version,
        currently uniform across every collector), so a fix to just the
        parsing logic is independently trackable/queryable on stored
        CollectorRuns, and a reprocessing script knows exactly which
        parser version produced a given historical source_payload.
        Every current implementation is "1"; none has shipped a second
        version yet.
        """

    @abstractmethod
    def collect(self) -> CollectionResult:
        pass