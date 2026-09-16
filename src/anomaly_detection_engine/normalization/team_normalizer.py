from collections.abc import Iterable
from dataclasses import dataclass

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
        aliases: dict[str, str] | None = None,
        fuzzy_threshold: float = 80.0,
        ambiguity_margin: float = 5.0,
    ) -> None:
        self._canonical_names = list(canonical_names)
        self._aliases = aliases or {}
        self._fuzzy_threshold = fuzzy_threshold
        self._ambiguity_margin = ambiguity_margin

    def normalize(self, raw_name: str) -> NormalizationResult:
        candidate = raw_name.strip()

        if candidate in self._canonical_names:
            return NormalizationResult(candidate, candidate, 100.0, "exact")

        alias_match = self._aliases.get(candidate)
        if alias_match:
            return NormalizationResult(candidate, alias_match, 100.0, "alias")

        # token_sort_ratio, not WRatio: WRatio's internal partial/token-set
        # blending scores two names sharing just one short common token
        # (e.g. "Iran U23" vs "United Arab Emirates U23", or "Zaqatala FK"
        # vs "FK Karvan Yevlakh") as high as 85.5 -- confidently above a
        # typical fuzzy_threshold -- regardless of how different the rest
        # of the name is, silently merging genuinely different teams
        # (verified live: six real, distinct Meridianbet teams collapsed
        # into wrong canonical teams this way). token_sort_ratio still
        # correctly handles legitimate word-reordering ("Barcelona FC" vs
        # "FC Barcelona" -> 100) and every other case this project's own
        # tests already covered, while scoring every one of the false-
        # positive cases above at 50 or well below -- safely under any
        # threshold this project actually uses.
        matches = process.extract(
            candidate,
            self._canonical_names,
            scorer=fuzz.token_sort_ratio,
            limit=2,
        )

        if not matches:
            return NormalizationResult(candidate, None, 0.0, "unknown")

        best_name, best_score, _ = matches[0]

        if best_score < self._fuzzy_threshold:
            return NormalizationResult(candidate, None, float(best_score), "unknown")

        if len(matches) > 1:
            _, second_score, _ = matches[1]
            # Two candidates nearly tied for best match is a much riskier
            # situation than one clear winner -- e.g. best=91/second=90
            # ("Manchester United" vs "Manchester United U21") shouldn't
            # be silently resolved the same way as best=91/second=52. The
            # caller decides what "ambiguous" means for it (FixtureCatalog
            # treats it like "unknown": create a new team rather than
            # guess which of two close candidates was meant).
            if best_score - second_score < self._ambiguity_margin:
                return NormalizationResult(candidate, None, float(best_score), "ambiguous")

        return NormalizationResult(candidate, best_name, float(best_score), "fuzzy")
