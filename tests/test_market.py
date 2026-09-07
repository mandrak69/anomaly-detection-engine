from decimal import Decimal

from anomaly_detection_engine.models.market import (
    MarketIdentity,
    MarketPeriod,
    MarketPhase,
    MarketType,
    canonical_decimal,
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
