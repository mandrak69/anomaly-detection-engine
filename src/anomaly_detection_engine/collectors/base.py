from abc import ABC, abstractmethod

from anomaly_detection_engine.models.raw_odds import RawEventOdds


class OddsCollector(ABC):

    @abstractmethod
    def collect(self) -> list[RawEventOdds]:
        pass