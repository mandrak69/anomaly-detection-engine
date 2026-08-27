from abc import ABC, abstractmethod

from anomaly_detection_engine.models.raw_odds import RawEventOdds


class OddsCollector(ABC):

    @property
    @abstractmethod
    def source(self) -> str:
        """Identifies this collector for CollectorRun tracking."""

    @abstractmethod
    def collect(self) -> list[RawEventOdds]:
        pass