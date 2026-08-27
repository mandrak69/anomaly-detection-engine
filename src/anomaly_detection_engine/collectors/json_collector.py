import json
from datetime import datetime
from pathlib import Path

from anomaly_detection_engine.collectors.base import OddsCollector
from anomaly_detection_engine.models.raw_odds import RawEventOdds


class JsonOddsCollector(OddsCollector):

    def __init__(self, path: Path):
        self.path = path

    def collect(self) -> list[RawEventOdds]:
        raw_data = json.loads(
            self.path.read_text(encoding="utf-8")
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
                    market="1X2",
                    odds={
                        "1": float(row["odds"]["1"]),
                        "X": float(row["odds"]["X"]),
                        "2": float(row["odds"]["2"]),
                    },
                )
            )

        return result