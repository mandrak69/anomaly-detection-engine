import json
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from anomaly_detection_engine.collectors.base import CollectionResult, OddsCollector
from anomaly_detection_engine.models.market import DEFAULT_MARKET
from anomaly_detection_engine.models.raw_odds import RawEventOdds

logger = logging.getLogger(__name__)


class JsonOddsCollector(OddsCollector):

    def __init__(self, path: Path):
        self.path = path

    @property
    def source(self) -> str:
        return f"json:{self.path.name}"

    @property
    def provider_id(self) -> str:
        return "json-demo"

    @property
    def parser_version(self) -> str:
        return "1"

    def collect(self) -> CollectionResult:
        source_payload = self.path.read_text(encoding="utf-8")
        raw_data = json.loads(source_payload, parse_float=Decimal)

        result = []

        for row in raw_data:
            result.append(
                RawEventOdds(
                    source=row["bookmaker"],
                    sport=row["sport"],
                    league=row["league"],
                    home_team=row["home_team"],
                    away_team=row["away_team"],
                    start_time=datetime.fromisoformat(row["start_time"]),
                    observed_at=datetime.fromisoformat(row["observed_at"]),
                    market=DEFAULT_MARKET,
                    odds={
                        "1": Decimal(row["odds"]["1"]),
                        "X": Decimal(row["odds"]["X"]),
                        "2": Decimal(row["odds"]["2"]),
                    },
                )
            )

        logger.info(
            "json_collector.read",
            extra={"path": str(self.path), "records_produced": len(result)},
        )

        return CollectionResult(source_payload=source_payload, records=result)