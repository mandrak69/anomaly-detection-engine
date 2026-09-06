from abc import ABC, abstractmethod

from anomaly_detection_engine.models.raw_odds import RawEventOdds


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

    @abstractmethod
    def collect(self) -> list[RawEventOdds]:
        pass