from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.models.market import (
    MarketIdentity,
    MarketPeriod,
    MarketType,
)
from anomaly_detection_engine.models.raw_odds import RawEventOdds
from anomaly_detection_engine.validation.raw_odds_validator import (
    validate_raw_event_odds,
)


def build_valid_raw_event_odds() -> RawEventOdds:
    return RawEventOdds(
        source="Mozzart",
        sport="football",
        league="demo-league",
        home_team="Manchester United",
        away_team="Liverpool",
        start_time=datetime.fromisoformat(
            "2026-09-01T18:00:00+00:00"
        ),
        observed_at=datetime.fromisoformat(
            "2026-08-27T08:00:00+00:00"
        ),
        market=MarketIdentity(
            market_type=MarketType.THREE_WAY,
            period=MarketPeriod.FULL_TIME,
        ),
        odds={
            "1": Decimal("2.15"),
            "X": Decimal("3.45"),
            "2": Decimal("3.20"),
        },
    )


def test_valid_raw_event_odds_passes_validation():
    raw = build_valid_raw_event_odds()

    result = validate_raw_event_odds(raw)

    assert result.valid is True
    assert result.errors == ()


def test_missing_home_team_fails_structural_validation():
    raw = build_valid_raw_event_odds()

    raw = RawEventOdds(
        **{
            **raw.__dict__,
            "home_team": "",
        }
    )

    result = validate_raw_event_odds(raw)

    assert result.valid is False
    assert result.stage.value == "structural"
    assert any(
        issue.code == "missing-home-team"
        for issue in result.errors
    )


def test_invalid_odds_fail_semantic_validation():
    raw = build_valid_raw_event_odds()

    raw = RawEventOdds(
        **{
            **raw.__dict__,
            "odds": {
                "1": Decimal("0.0"),
                "X": Decimal("3.45"),
                "2": Decimal("3.20"),
            },
        }
    )

    result = validate_raw_event_odds(raw)

    assert result.valid is False
    assert any(
        issue.code == "invalid-odds-value"
        for issue in result.errors
    )


def test_naive_observed_at_fails_validation():
    raw = build_valid_raw_event_odds()

    raw = RawEventOdds(
        **{
            **raw.__dict__,
            "observed_at": datetime.fromisoformat(
                "2026-08-27T08:00:00"
            ),
        }
    )

    result = validate_raw_event_odds(raw)

    assert result.valid is False
    assert any(
        issue.code == "naive-observed-at"
        for issue in result.errors
    )