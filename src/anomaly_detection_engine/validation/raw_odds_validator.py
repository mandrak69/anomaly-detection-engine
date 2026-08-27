from datetime import datetime
from decimal import Decimal

from anomaly_detection_engine.models.raw_odds import RawEventOdds
from anomaly_detection_engine.validation.result import (
    DataValidationResult,
    ValidationIssue,
    ValidationStage,
)


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

    if raw.home_team.strip() == raw.away_team.strip():
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