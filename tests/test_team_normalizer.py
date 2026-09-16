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


def test_ambiguous_fuzzy_match_is_not_resolved():
    # "Manchester United Res" scores 89.5 against "Manchester United" and
    # 85.7 against "Manchester United U21" (verified via
    # rapidfuzz.fuzz.token_sort_ratio) -- both above threshold, and close
    # enough (3.8 apart) to trip the default 5.0 ambiguity_margin. Not a
    # clear winner. Silently picking the top result here risks merging a
    # reserve/youth team into the first team.
    normalizer = TeamNormalizer(
        ["Manchester United", "Manchester United U21"],
        fuzzy_threshold=80,
    )

    result = normalizer.normalize("Manchester United Res")

    assert result.canonical_name is None
    assert result.method == "ambiguous"


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
