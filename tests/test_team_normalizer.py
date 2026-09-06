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
    # "Man United" scores ~85.5 against both "Manchester United" and
    # "Manchester United U21" (verified via rapidfuzz.process.extract) --
    # a near-exact tie, not a clear winner. Silently picking the top
    # result here risks merging a reserve/youth team into the first team.
    normalizer = TeamNormalizer(
        ["Manchester United", "Manchester United U21"],
        fuzzy_threshold=80,
    )

    result = normalizer.normalize("Man United")

    assert result.canonical_name is None
    assert result.method == "ambiguous"


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
