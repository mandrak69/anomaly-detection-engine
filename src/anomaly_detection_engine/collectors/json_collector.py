import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from anomaly_detection_engine.collectors.base import OddsCollector
from anomaly_detection_engine.models.market import MarketIdentity, MarketPeriod, MarketType
from anomaly_detection_engine.models.raw_odds import RawEventOdds

# Current MVP scope (see README) is limited to pre-match full-time 1X2 markets,
# and the sample/source payloads do not carry explicit market metadata yet.
DEFAULT_MARKET = MarketIdentity(
    market_type=MarketType.THREE_WAY,
    period=MarketPeriod.FULL_TIME,
)


class JsonOddsCollector(OddsCollector):

    def __init__(self, path: Path):
        self.path = path

    def collect(self) -> list[RawEventOdds]:
        raw_data = json.loads(
            self.path.read_text(encoding="utf-8"),
            parse_float=Decimal,
        )

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

        return result