from dataclasses import dataclass, field
from enum import Enum


class ValidationStage(str, Enum):
    STRUCTURAL = "structural"
    SEMANTIC = "semantic"
    IDENTITY = "identity"
    TEMPORAL = "temporal"


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str


@dataclass(frozen=True)
class DataValidationResult:
    valid: bool
    stage: ValidationStage
    errors: tuple[ValidationIssue, ...] = field(default_factory=tuple)
    warnings: tuple[ValidationIssue, ...] = field(default_factory=tuple)

    @classmethod
    def success(
        cls,
        stage: ValidationStage,
        warnings: tuple[ValidationIssue, ...] = (),
    ) -> "DataValidationResult":
        return cls(
            valid=True,
            stage=stage,
            warnings=warnings,
        )

    @classmethod
    def failure(
        cls,
        stage: ValidationStage,
        errors: tuple[ValidationIssue, ...],
        warnings: tuple[ValidationIssue, ...] = (),
    ) -> "DataValidationResult":
        return cls(
            valid=False,
            stage=stage,
            errors=errors,
            warnings=warnings,
        )