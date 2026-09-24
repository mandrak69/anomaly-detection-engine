import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from rapidfuzz import fuzz, process

_DIGIT_RUN = re.compile(r"\d+")


@dataclass(frozen=True)
class NormalizationResult:
    raw_name: str
    canonical_name: str | None
    confidence: float
    method: str


def _comparison_key(name: str) -> str:
    """Case/whitespace/Unicode-form-insensitive key used for every exact/
    alias/token-alias/fuzzy comparison in this class -- two spellings
    that differ only by case ("Real Madrid" vs "real madrid"), repeated
    or irregular whitespace ("Real   Madrid"), or Unicode representation
    (full-width "Ｕｎｉｔｅｄ" vs plain "United", a combining-character
    accent vs its precomposed form) are the same name and must compare
    equal. This is purely a comparison key -- it never leaks into what
    gets stored or displayed; canonical_name/raw_name/alias targets
    everywhere in this module stay the original, well-formed string a
    human or provider actually wrote.

    NFKC, not NFC: also folds compatibility variants a plain accent-form
    normalization would miss (full-width characters, certain ligatures),
    not just combining-vs-precomposed accents. casefold(), not lower():
    correct for scripts where lower() alone under- or over-matches
    (e.g. German "ß" casefolds to "ss", matching "SS").
    """
    return unicodedata.normalize("NFKC", " ".join(name.split())).casefold()


def _digit_tokens(comparison_key: str) -> tuple[str, ...]:
    """The digit runs in a _comparison_key'd string, in order -- e.g.
    "3. division - girone 2" -> ("3", "2"). token_sort_ratio scores two
    otherwise-identical strings that differ only in one embedded number
    as near-100 (a single-character edit out of a long string), even
    though that number is almost always the whole reason two names are
    different real entities, not a spelling variant of each other --
    verified live: "Engleska 1" (EPL) and "Engleska 3" (English League
    One, a completely different division) scored well above this
    project's fuzzy_threshold this way, along with 3. Division - Girone
    2/3/4/5/6 (different regional sub-groups of the same tier) and
    U19/U21 age-group leagues. Comparing digit runs as a precondition
    for fuzzy candidacy (see normalize()) closes this the same way
    _comparison_key's own normalization closes case/whitespace/Unicode
    mismatches -- a name with no digits at all (most team/league names)
    is unaffected, since two empty tuples always compare equal."""
    return tuple(_DIGIT_RUN.findall(comparison_key))


def names_are_similar(a: str, b: str, *, fuzzy_threshold: float = 85.0) -> bool:
    """True when two raw names are close enough to plausibly be the same
    real-world team/competition, by the same normalization/digit-guard/
    fuzzy-ratio rules `TeamNormalizer.normalize()` itself uses to accept
    a fuzzy match -- unlike normalize(), this compares exactly two given
    strings rather than one name against a whole candidate pool, and
    returns a plain bool rather than picking a canonical target.

    Used by FixtureCatalog's provider-id fast path (see
    source_team_id_mappings/source_competition_id_mappings) to detect a
    provider recycling a numeric id across a season boundary onto a
    genuinely different name -- a mismatch here doesn't override the id
    (a provider doesn't silently swap identities without also renaming;
    the id is still trusted), it only decides whether to log a drift
    warning for a human to notice.
    """
    key_a, key_b = _comparison_key(a), _comparison_key(b)
    if key_a == key_b:
        return True
    if _digit_tokens(key_a) != _digit_tokens(key_b):
        return False
    return fuzz.token_sort_ratio(key_a, key_b) >= fuzzy_threshold


class TeamNormalizer:
    def __init__(
        self,
        canonical_names: Iterable[str],
        aliases: dict[str, str] | None = None,
        token_aliases: dict[str, str] | None = None,
        fuzzy_threshold: float = 80.0,
        ambiguity_margin: float = 5.0,
    ) -> None:
        self._canonical_names = list(canonical_names)
        # Keyed by _comparison_key, not the raw canonical string -- see
        # normalize()'s exact/fuzzy lookups below. Two distinct existing
        # canonical rows that happen to collide under this key (e.g. a
        # pre-existing "Real Madrid"/"real madrid" duplicate pair from
        # before this normalization existed) is a real but separate
        # problem -- consolidating already-duplicated canonical rows is
        # a one-off data cleanup, not something a per-call comparison key
        # can or should silently resolve; whichever one this dict ends up
        # keeping just continues to be preferred, same as today.
        self._canonical_name_by_key = {
            _comparison_key(name): name for name in self._canonical_names
        }
        self._aliases = aliases or {}
        self._alias_by_key = {_comparison_key(k): v for k, v in self._aliases.items()}
        self._token_aliases = token_aliases or {}
        self._token_alias_by_key = {
            _comparison_key(k): v for k, v in self._token_aliases.items()
        }
        self._fuzzy_threshold = fuzzy_threshold
        self._ambiguity_margin = ambiguity_margin

    def normalize(self, raw_name: str) -> NormalizationResult:
        # Word-level, not whole-string like `aliases` below: an acronym
        # like "UAE" only ever appears as one word inside a longer raw
        # name ("UAE M23", "UAE U23", ...), never as the entire raw
        # string, so a whole-string alias table can never match it -- no
        # fuzzy scorer bridges "UAE" and "United Arab Emirates" either,
        # since they share almost no characters. Applied before anything
        # else (exact/alias/fuzzy all then see the expanded form), and
        # NormalizationResult.raw_name carries the *expanded* string
        # onward -- FixtureCatalog uses it (not the original raw_name
        # argument) when creating a brand-new team, so the canonical name
        # is consistent regardless of which provider's spelling is seen
        # first. Also collapses irregular internal whitespace: splitting
        # on any whitespace and rejoining with single spaces means a
        # brand-new team's canonical name is never created with the raw,
        # possibly-double-spaced spelling a specific sighting happened to
        # have.
        candidate = self._expand_tokens(raw_name.strip())
        candidate_key = _comparison_key(candidate)

        exact_match = self._canonical_name_by_key.get(candidate_key)
        if exact_match is not None:
            return NormalizationResult(candidate, exact_match, 100.0, "exact")

        alias_match = self._alias_by_key.get(candidate_key)
        if alias_match:
            # An alias's own target string is whatever the caller wrote
            # in the aliases dict ("Man Utd": "MANCHESTER UNITED"),
            # which is not necessarily spelled exactly like the existing
            # canonical row it's supposed to point at ("Manchester
            # United") -- without resolving through the same comparison
            # key used everywhere else here, FixtureCatalog's own exact,
            # non-normalized `existing.get(canonical_name)` lookup would
            # miss and create a needless duplicate team differing from
            # the real one only by case/whitespace/Unicode form. Falls
            # back to the alias's own target string when nothing
            # existing matches it yet (the alias's first-ever use, or a
            # target that genuinely doesn't exist as a team row yet --
            # both already handled downstream in FixtureCatalog).
            resolved_target = self._canonical_name_by_key.get(
                _comparison_key(alias_match), alias_match
            )
            return NormalizationResult(candidate, resolved_target, 100.0, "alias")

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
        #
        # Matched against comparison keys, not self._canonical_names
        # directly -- rapidfuzz's scorers are case-sensitive by default
        # (no processor is passed here), so without this, "Real Madrid"
        # vs "real madrid" would score well below 100 on case alone, on
        # top of whatever the real spelling difference contributes.
        # When the raw name itself contains digits, only candidates whose
        # digit runs match it exactly are eligible for fuzzy comparison
        # at all -- see _digit_tokens' own docstring for why (a differing
        # embedded number is almost always a different real entity, not a
        # spelling variant, and token_sort_ratio alone can't tell the
        # difference). A brand-new name whose number no existing
        # candidate shares correctly falls through to "unknown" below
        # rather than fuzzy-matching a wrong, differently-numbered
        # existing entity.
        #
        # Only restricts candidates when the *raw* name has a number to
        # be specific about -- a digit-less raw name (e.g. a reserve-team
        # marker like "Man Utd Res", with no number of its own) must
        # still be free to fuzzy-match a numbered candidate like
        # "Manchester United U21" and land in the existing ambiguous/
        # fuzzy handling below, exactly as before this check existed.
        candidate_digits = _digit_tokens(candidate_key)
        if candidate_digits:
            canonical_keys = [
                key
                for key in self._canonical_name_by_key
                if _digit_tokens(key) == candidate_digits
            ]
        else:
            canonical_keys = list(self._canonical_name_by_key.keys())
        matches = process.extract(
            candidate_key,
            canonical_keys,
            scorer=fuzz.token_sort_ratio,
            limit=2,
        )

        if not matches:
            return NormalizationResult(candidate, None, 0.0, "unknown")

        best_key, best_score, _ = matches[0]
        best_name = self._canonical_name_by_key[best_key]

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

    def _expand_tokens(self, name: str) -> str:
        words = name.split()
        return " ".join(
            self._token_alias_by_key.get(_comparison_key(word), word) for word in words
        )
