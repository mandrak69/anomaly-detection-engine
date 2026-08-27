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
