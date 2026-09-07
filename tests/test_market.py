from decimal import Decimal

import pytest

from anomaly_detection_engine.models.market import (
    MarketIdentity,
    MarketPeriod,
    MarketPhase,
    MarketType,
    canonical_decimal,
    required_outcomes,
)


def test_canonical_decimal_strips_trailing_zeros():
    assert canonical_decimal(Decimal("2.50")) == Decimal("2.5")
    assert str(canonical_decimal(Decimal("2.50"))) == "2.5"


def test_canonical_decimal_does_not_produce_exponential_notation():
    # Decimal.normalize() alone would turn a round number like 100 into
    # Decimal("1E+2") -- useless as a betting line's text form.
    result = canonical_decimal(Decimal("100.00"))
    assert result == Decimal("100")
    assert str(result) == "100"
    assert "E" not in str(result)


def test_canonical_decimal_preserves_negative_handicap_lines():
    assert str(canonical_decimal(Decimal("-1.50"))) == "-1.5"


def _market(**overrides) -> MarketIdentity:
    defaults = dict(
        market_type=MarketType.TOTALS,
        period=MarketPeriod.FULL_TIME,
        phase=MarketPhase.PRE_MATCH,
    )
    defaults.update(overrides)
    return MarketIdentity(**defaults)


def test_market_identity_canonicalizes_line_on_construction():
    from_one_source = _market(line=Decimal("2.50"))
    from_another_source = _market(line=Decimal("2.5"))

    assert from_one_source == from_another_source
    assert from_one_source.line == from_another_source.line
    assert str(from_one_source.line) == "2.5"


def test_market_identity_normalizes_empty_rules_and_specifier_to_none():
    with_empty_strings = _market(rules="", specifier="")
    with_none = _market(rules=None, specifier=None)

    assert with_empty_strings == with_none
    assert with_empty_strings.rules is None
    assert with_empty_strings.specifier is None


def test_market_identity_leaves_a_none_line_untouched():
    assert _market(line=None).line is None


def test_market_identity_never_mixes_different_totals_lines():
    # The same market_type/period/phase, only the line differs -- must
    # be different identities, never merged in dedupe/detection.
    line_2_5 = _market(line=Decimal("2.5"))
    line_3_5 = _market(line=Decimal("3.5"))

    assert line_2_5 != line_3_5
    assert hash(line_2_5) != hash(line_3_5)


def test_required_outcomes_three_way():
    assert required_outcomes(MarketType.THREE_WAY) == ("1", "X", "2")


def test_required_outcomes_totals():
    assert required_outcomes(MarketType.TOTALS) == ("OVER", "UNDER")


def test_required_outcomes_handicap():
    # The 3-way "Handicap Result" flavor this project detects (see
    # HANDICAP_MINUS_1_MARKET) reuses THREE_WAY's exact outcome shape --
    # Home/Draw/Away are all still possible, just with a fixed handicap
    # applied before settlement.
    assert required_outcomes(MarketType.HANDICAP) == ("1", "X", "2")


def test_required_outcomes_raises_for_an_unmapped_market_type():
    # MONEYLINE's enum value exists but isn't wired into detection yet --
    # must fail loudly, not silently accept "any outcomes will do".
    with pytest.raises(ValueError, match="MONEYLINE|moneyline"):
        required_outcomes(MarketType.MONEYLINE)
