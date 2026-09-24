import pytest

from anomaly_detection_engine.normalization.team_normalizer import TeamNormalizer


def test_alias_normalization():
    normalizer = TeamNormalizer(
        ["Manchester United", "Liverpool"],
        aliases={"Man Utd": "Manchester United"},
    )

    result = normalizer.normalize("Man Utd")

    assert result.canonical_name == "Manchester United"
    assert result.method == "alias"
    assert result.confidence == 100.0


def test_fuzzy_normalization():
    normalizer = TeamNormalizer(
        ["Manchester United", "Liverpool"],
        fuzzy_threshold=70,
    )

    result = normalizer.normalize("Manchester Utd")

    assert result.canonical_name == "Manchester United"
    assert result.method == "fuzzy"


def test_unknown_team_below_threshold():
    normalizer = TeamNormalizer(
        ["Manchester United", "Liverpool"],
        fuzzy_threshold=95,
    )

    result = normalizer.normalize("Completely Different Team")

    assert result.canonical_name is None
    assert result.method == "unknown"


def test_reserve_qualifier_is_not_fuzzily_merged_into_senior_or_u21():
    # Qualifiers are identity-bearing, so this is rejected before a fuzzy
    # scorer can turn the short "Res" suffix into an apparent close match.
    normalizer = TeamNormalizer(
        ["Manchester United", "Manchester United U21"],
        fuzzy_threshold=80,
    )

    result = normalizer.normalize("Manchester United Res")

    assert result.canonical_name is None
    assert result.method == "unknown"
    assert result.confidence == 0


@pytest.mark.parametrize(
    ("raw_name", "existing_name"),
    [
        ("Manchester United", "Manchester United U21"),
        ("Manchester United U21", "Manchester United"),
        ("Barcelona", "Barcelona B"),
        ("Barcelona B", "Barcelona"),
        ("Chelsea", "Chelsea W"),
        ("Premier League", "Premier League 2"),
        ("Bundesliga", "2. Bundesliga"),
    ],
)
def test_identity_qualifiers_are_symmetric(raw_name, existing_name):
    result = TeamNormalizer([existing_name], fuzzy_threshold=80).normalize(raw_name)

    assert result.canonical_name is None
    assert result.method == "unknown"


def test_m21_and_u21_remain_compatible_provider_spellings():
    result = TeamNormalizer(["United Arab Emirates M23"], fuzzy_threshold=80).normalize(
        "United Arab Emirates U23"
    )

    assert result.canonical_name == "United Arab Emirates M23"
    assert result.method == "fuzzy"


def test_short_shared_token_does_not_falsely_merge_unrelated_teams():
    # Regression test: WRatio (the scorer this project used before)
    # scored "Iran U23" vs "United Arab Emirates U23" at 85.5 -- above a
    # typical fuzzy_threshold -- purely because both share the "U23"
    # token, despite the rest of the name being completely unrelated.
    # Verified live against a real Meridianbet capture: six genuinely
    # different teams (several "<country> U23" entries, several
    # "<club> RS" entries sharing only a short suffix/prefix token) were
    # silently merged into the wrong canonical team this way.
    normalizer = TeamNormalizer(
        ["United Arab Emirates U23"],
        fuzzy_threshold=85,
    )

    result = normalizer.normalize("Iran U23")

    assert result.canonical_name is None
    assert result.method == "unknown"


def test_short_shared_prefix_does_not_falsely_merge_unrelated_teams():
    normalizer = TeamNormalizer(
        ["FK Karvan Yevlakh"],
        fuzzy_threshold=85,
    )

    result = normalizer.normalize("Zaqatala FK")

    assert result.canonical_name is None
    assert result.method == "unknown"


def test_word_reordering_still_matches():
    # token_sort_ratio's whole point over plain character-based ratio:
    # a word-order swap alone must not prevent a match.
    normalizer = TeamNormalizer(
        ["FC Barcelona"],
        fuzzy_threshold=85,
    )

    result = normalizer.normalize("Barcelona FC")

    assert result.canonical_name == "FC Barcelona"
    assert result.method == "fuzzy"


def test_token_alias_expands_an_acronym_word_within_a_longer_raw_name():
    # ALIASES-style whole-string aliases can never match "UAE" as a key
    # against the raw name "UAE M23" (the whole string isn't "UAE") --
    # token_aliases substitutes it one word at a time instead, then lets
    # ordinary fuzzy matching handle the rest ("M23" vs "U23").
    normalizer = TeamNormalizer(
        ["United Arab Emirates U23"],
        token_aliases={"UAE": "United Arab Emirates"},
        fuzzy_threshold=85,
    )

    result = normalizer.normalize("UAE M23")

    assert result.canonical_name == "United Arab Emirates U23"
    assert result.method == "fuzzy"


def test_token_alias_expansion_is_reflected_in_result_raw_name():
    # FixtureCatalog creates a brand-new team under result.raw_name (not
    # the original argument) specifically so the expansion is consistent
    # regardless of which provider's spelling is seen first -- this is
    # what makes that possible.
    normalizer = TeamNormalizer([], token_aliases={"UAE": "United Arab Emirates"})

    result = normalizer.normalize("UAE M23")

    assert result.raw_name == "United Arab Emirates M23"
    assert result.canonical_name is None
    assert result.method == "unknown"


def test_token_alias_only_matches_whole_words():
    # "UAEFC" must not become "United Arab EmiratesFC" -- word-boundary
    # (whitespace-split) matching only, never a substring replacement
    # inside a longer word.
    normalizer = TeamNormalizer([], token_aliases={"UAE": "United Arab Emirates"})

    result = normalizer.normalize("UAEFC")

    assert result.raw_name == "UAEFC"


def test_case_only_difference_is_an_exact_match_not_a_new_team():
    normalizer = TeamNormalizer(["Manchester United", "Liverpool"])

    result = normalizer.normalize("manchester united")

    assert result.canonical_name == "Manchester United"
    assert result.method == "exact"
    assert result.confidence == 100.0


def test_repeated_internal_whitespace_is_an_exact_match():
    normalizer = TeamNormalizer(["Real Madrid"])

    result = normalizer.normalize("Real   Madrid")

    assert result.canonical_name == "Real Madrid"
    assert result.method == "exact"


def test_unicode_full_width_variant_is_an_exact_match():
    # NFKC folds full-width Unicode forms (as seen in some Asian-market
    # bookmaker feeds) onto their plain-ASCII equivalents.
    normalizer = TeamNormalizer(["United"])

    result = normalizer.normalize("Ｕｎｉｔｅｄ")

    assert result.canonical_name == "United"
    assert result.method == "exact"


def test_case_insensitive_alias_match():
    normalizer = TeamNormalizer(
        ["Manchester United"], aliases={"Man Utd": "Manchester United"}
    )

    result = normalizer.normalize("MAN UTD")

    assert result.canonical_name == "Manchester United"
    assert result.method == "alias"


def test_alias_target_resolves_to_the_existing_canonical_names_own_spelling():
    # The alias's own target string ("MANCHESTER UNITED") differs from
    # the real existing canonical row ("Manchester United") only by
    # case -- returning the alias's raw target as-is would make
    # FixtureCatalog's exact, non-normalized `existing.get(...)` lookup
    # miss and create a needless duplicate team.
    normalizer = TeamNormalizer(
        ["Manchester United"], aliases={"Man Utd": "MANCHESTER UNITED"}
    )

    result = normalizer.normalize("Man Utd")

    assert result.canonical_name == "Manchester United"  # the existing row's own spelling
    assert result.method == "alias"


def test_alias_target_with_no_existing_match_falls_back_to_its_own_spelling():
    # No existing canonical team matches the alias target at all yet --
    # falls back to the alias's own target string exactly as before,
    # so FixtureCatalog creates a brand-new team under that spelling.
    normalizer = TeamNormalizer([], aliases={"Man Utd": "Manchester United"})

    result = normalizer.normalize("Man Utd")

    assert result.canonical_name == "Manchester United"
    assert result.method == "alias"


def test_differently_cased_acronym_still_expands_via_token_alias():
    normalizer = TeamNormalizer(
        ["United Arab Emirates U23"],
        token_aliases={"UAE": "United Arab Emirates"},
        fuzzy_threshold=85,
    )

    result = normalizer.normalize("uae M23")

    assert result.raw_name == "United Arab Emirates M23"
    assert result.canonical_name == "United Arab Emirates U23"
    assert result.method == "fuzzy"


def test_case_and_whitespace_differences_do_not_affect_fuzzy_score():
    # The same fuzzy match, differing only by case/whitespace in the raw
    # spelling, must score identically -- case-sensitivity was a real gap
    # in the fuzzy path too (rapidfuzz scorers are case-sensitive by
    # default), not just exact/alias.
    normalizer = TeamNormalizer(["Manchester United"], fuzzy_threshold=70)

    plain = normalizer.normalize("Manchester Utd")
    upper = normalizer.normalize("MANCHESTER   UTD")

    assert plain.confidence == upper.confidence
    assert upper.canonical_name == "Manchester United"
    assert upper.method == "fuzzy"


def test_differing_embedded_number_does_not_falsely_merge_league_tiers():
    # Regression test: token_sort_ratio scores "Engleska 1" (EPL) vs
    # "Engleska 3" (English League One, a completely different division)
    # at 90+ (a single-character edit out of a short string), well above
    # a typical fuzzy_threshold, purely because only the trailing digit
    # differs -- verified live against a real Mozzart capture: League
    # One/Two/National League matches were silently attributed to the
    # EPL competition this way, along with several countries' 2nd/3rd
    # divisions merging into their 1st.
    normalizer = TeamNormalizer(["Engleska 1"], fuzzy_threshold=85)

    result = normalizer.normalize("Engleska 3")

    assert result.canonical_name is None
    assert result.method == "unknown"


def test_differing_girone_group_number_does_not_falsely_merge():
    normalizer = TeamNormalizer(["3. Division - Girone 2"], fuzzy_threshold=85)

    result = normalizer.normalize("3. Division - Girone 3")

    assert result.canonical_name is None
    assert result.method == "unknown"


def test_matching_embedded_number_still_fuzzy_matches_normally():
    # The digit-run check is a precondition, not a replacement for the
    # existing scorer -- two names sharing the same embedded number still
    # fuzzy-match exactly as before.
    normalizer = TeamNormalizer(["Manchester United 2"], fuzzy_threshold=70)

    result = normalizer.normalize("Manchester Utd 2")

    assert result.canonical_name == "Manchester United 2"
    assert result.method == "fuzzy"


def test_name_with_no_digits_is_unaffected_by_the_digit_check():
    normalizer = TeamNormalizer(["Manchester United"], fuzzy_threshold=70)

    result = normalizer.normalize("Manchester Utd")

    assert result.canonical_name == "Manchester United"
    assert result.method == "fuzzy"


def test_clear_winner_is_not_treated_as_ambiguous():
    # Sanity check for the ambiguity margin itself: a huge gap between
    # best and second-best (unlike the tied case above) must still
    # resolve normally.
    normalizer = TeamNormalizer(
        ["Manchester United", "Liverpool"],
        fuzzy_threshold=70,
    )

    result = normalizer.normalize("Manchester Utd")

    assert result.canonical_name == "Manchester United"
    assert result.method == "fuzzy"
