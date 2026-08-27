from datetime import datetime

from anomaly_detection_engine.analysis.best_odds import find_best_odds
from anomaly_detection_engine.models.odds import Bookmaker, OddsSnapshot


def test_finds_best_odds_per_outcome():
    now = datetime.fromisoformat("2026-08-27T08:00:00+00:00")
    a = Bookmaker("a", "A")
    b = Bookmaker("b", "B")

    snapshots = [
        OddsSnapshot("e1", a, "1X2", "1", 2.00, now),
        OddsSnapshot("e1", b, "1X2", "1", 2.20, now),
        OddsSnapshot("e1", a, "1X2", "X", 3.50, now),
        OddsSnapshot("e1", b, "1X2", "X", 3.40, now),
        OddsSnapshot("e1", a, "1X2", "2", 3.10, now),
        OddsSnapshot("e1", b, "1X2", "2", 3.60, now),
    ]

    result = find_best_odds(snapshots, event_id="e1", market="1X2")

    assert result["1"].odds == 2.20
    assert result["1"].bookmaker_name == "B"
    assert result["X"].odds == 3.50
    assert result["2"].odds == 3.60
