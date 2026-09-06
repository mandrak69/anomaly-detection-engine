from decimal import Decimal

from anomaly_detection_engine.models.market import MarketType
from anomaly_detection_engine.models.raw_odds import RawEventOdds
from anomaly_detection_engine.validation.result import (
    DataValidationResult,
    ValidationIssue,
    ValidationStage,
)

_THREE_WAY_OUTCOMES = {"1", "X", "2"}


def validate_raw_event_odds(raw: RawEventOdds) -> DataValidationResult:
    structural_errors: list[ValidationIssue] = []

    if not raw.source.strip():
        structural_errors.append(
            ValidationIssue(
                code="missing-source",
                message="Source must be provided.",
            )
        )

    if not raw.sport.strip():
        structural_errors.append(
            ValidationIssue(
                code="missing-sport",
                message="Sport must be provided.",
            )
        )

    if not raw.league.strip():
        structural_errors.append(
            ValidationIssue(
                code="missing-league",
                message="League must be provided.",
            )
        )

    if not raw.home_team.strip():
        structural_errors.append(
            ValidationIssue(
                code="missing-home-team",
                message="Home team must be provided.",
            )
        )

    if not raw.away_team.strip():
        structural_errors.append(
            ValidationIssue(
                code="missing-away-team",
                message="Away team must be provided.",
            )
        )

    if raw.home_team.strip().casefold() == raw.away_team.strip().casefold():
        structural_errors.append(
            ValidationIssue(
                code="same-home-away-team",
                message="Home and away team cannot be identical.",
            )
        )

    if raw.market is None:
        structural_errors.append(
            ValidationIssue(
                code="missing-market",
                message="Market must be provided.",
            )
        )

    if not raw.odds:
        structural_errors.append(
            ValidationIssue(
                code="missing-odds",
                message="At least one outcome odd must be provided.",
            )
        )

    if structural_errors:
        return DataValidationResult.failure(
            stage=ValidationStage.STRUCTURAL,
            errors=tuple(structural_errors),
        )

    semantic_errors: list[ValidationIssue] = []
    semantic_warnings: list[ValidationIssue] = []

    for outcome, odds in raw.odds.items():
        if not outcome.strip():
            semantic_errors.append(
                ValidationIssue(
                    code="missing-outcome",
                    message="Outcome name must not be empty.",
                )
            )

        if not isinstance(odds, Decimal):
            semantic_errors.append(
                ValidationIssue(
                    code="invalid-odds-type",
                    message=f"Odds for outcome '{outcome}' must be a Decimal.",
                )
            )
            continue

        if not odds.is_finite():
            # NaN/Infinity would otherwise reach the "> 1.0"/">1000"
            # comparisons below, where NaN raises decimal.InvalidOperation
            # (an unhandled crash, not a clean rejection) and Infinity
            # silently passes as merely "suspiciously high".
            semantic_errors.append(
                ValidationIssue(
                    code="invalid-odds-value",
                    message=f"Odds for outcome '{outcome}' must be a finite number, got {odds}.",
                )
            )
            continue

        if odds <= Decimal("1.0"):
            semantic_errors.append(
                ValidationIssue(
                    code="invalid-odds-value",
                    message=(
                        f"Decimal odds for outcome '{outcome}' "
                        "must be greater than 1.0."
                    ),
                )
            )

        if odds > Decimal("1000"):
            semantic_warnings.append(
                ValidationIssue(
                    code="suspiciously-high-odds",
                    message=(
                        f"Odds for outcome '{outcome}' are unusually high: {odds}."
                    ),
                )
            )

    if raw.market is not None and raw.market.market_type == MarketType.THREE_WAY:
        # Domain invariant of the market itself, not just a collector's
        # own quirk: a THREE_WAY (1X2) market has exactly these three
        # outcomes, never more or fewer -- catching this here means every
        # collector benefits, not just whichever one happened to have a
        # test for it.
        actual_outcomes = set(raw.odds.keys())
        if actual_outcomes != _THREE_WAY_OUTCOMES:
            semantic_errors.append(
                ValidationIssue(
                    code="invalid-three-way-outcomes",
                    message=(
                        f"THREE_WAY market must have exactly outcomes "
                        f"{sorted(_THREE_WAY_OUTCOMES)}, got {sorted(actual_outcomes)}."
                    ),
                )
            )

    if raw.source_timestamp is not None:
        if raw.source_timestamp.tzinfo is None:
            semantic_errors.append(
                ValidationIssue(
                    code="naive-source-timestamp",
                    message="source_timestamp must be timezone-aware.",
                )
            )

    if raw.observed_at.tzinfo is None:
        semantic_errors.append(
            ValidationIssue(
                code="naive-observed-at",
                message="observed_at must be timezone-aware.",
            )
        )

    if raw.start_time.tzinfo is None:
        semantic_errors.append(
            ValidationIssue(
                code="naive-start-time",
                message="start_time must be timezone-aware.",
            )
        )

    if semantic_errors:
        return DataValidationResult.failure(
            stage=ValidationStage.SEMANTIC,
            errors=tuple(semantic_errors),
            warnings=tuple(semantic_warnings),
        )

    return DataValidationResult.success(
        stage=ValidationStage.SEMANTIC,
        warnings=tuple(semantic_warnings),
    )