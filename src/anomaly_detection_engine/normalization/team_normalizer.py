from dataclasses import dataclass
from typing import Dict, Iterable

from rapidfuzz import fuzz, process


@dataclass(frozen=True)
class NormalizationResult:
    raw_name: str
    canonical_name: str | None
    confidence: float
    method: str


class TeamNormalizer:
    def __init__(
        self,
        canonical_names: Iterable[str],
        aliases: Dict[str, str] | None = None,
        fuzzy_threshold: float = 80.0,
    ) -> None:
        self._canonical_names = list(canonical_names)
        self._aliases = aliases or {}
        self._fuzzy_threshold = fuzzy_threshold

    def normalize(self, raw_name: str) -> NormalizationResult:
        candidate = raw_name.strip()

        if candidate in self._canonical_names:
            return NormalizationResult(candidate, candidate, 100.0, "exact")

        alias_match = self._aliases.get(candidate)
        if alias_match:
            return NormalizationResult(candidate, alias_match, 100.0, "alias")

        result = process.extractOne(
            candidate,
            self._canonical_names,
            scorer=fuzz.WRatio,
        )

        if not result:
            return NormalizationResult(candidate, None, 0.0, "unknown")

        canonical_name, score, _ = result
        if score >= self._fuzzy_threshold:
            return NormalizationResult(candidate, canonical_name, float(score), "fuzzy")

        return NormalizationResult(candidate, None, float(score), "unknown")
