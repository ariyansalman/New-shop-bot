"""utils/money.py and validate_amount() - no DB needed."""

from decimal import Decimal, InvalidOperation

import pytest

from utils import to_money, money_or_none, validate_amount, format_price


def test_to_money_rounds_half_up():
    # Python's built-in round() would give 19.98 here (banker's rounding);
    # money is conventionally rounded half-up.
    assert to_money("19.985") == Decimal("19.99")
    assert to_money(19.985) == Decimal("19.99")


def test_to_money_accepts_decimal_float_int_str():
    assert to_money(Decimal("5.5")) == Decimal("5.50")
    assert to_money(5.5) == Decimal("5.50")
    assert to_money(5) == Decimal("5.00")
    assert to_money("5.5") == Decimal("5.50")


def test_to_money_avoids_binary_float_noise():
    # 0.1 + 0.2 == 0.30000000000000004 in binary float; to_money must not
    # propagate that into the stored/displayed value.
    assert to_money(0.1 + 0.2) == Decimal("0.30")


def test_to_money_rejects_nan_and_inf():
    with pytest.raises(InvalidOperation):
        to_money(float("nan"))
    with pytest.raises(InvalidOperation):
        to_money(float("inf"))


def test_money_or_none():
    assert money_or_none("19.99") == Decimal("19.99")
    assert money_or_none("not a number") is None
    assert money_or_none(float("nan")) is None


def test_validate_amount_valid():
    ok, amount, err = validate_amount("100")
    assert ok
    assert amount == Decimal("100.00")
    assert isinstance(amount, Decimal)
    assert err == ""


def test_validate_amount_rounds_half_up():
    ok, amount, err = validate_amount("19.999")
    assert ok
    assert amount == Decimal("20.00")


def test_validate_amount_strips_dollar_and_commas():
    ok, amount, err = validate_amount("$1,234.50")
    assert ok
    assert amount == Decimal("1234.50")


def test_validate_amount_rejects_garbage():
    ok, amount, err = validate_amount("not a number")
    assert not ok
    assert "Invalid amount" in err


def test_validate_amount_rejects_nan_and_inf():
    for bad in ("nan", "inf", "-inf"):
        ok, amount, err = validate_amount(bad)
        assert not ok, f"{bad!r} should have been rejected"


def test_validate_amount_enforces_min_max(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "MIN_TOPUP_AMOUNT", 5.0)
    monkeypatch.setattr(settings, "MAX_TOPUP_AMOUNT", 1000.0)

    ok, _, err = validate_amount("1")
    assert not ok and "Minimum" in err

    ok, _, err = validate_amount("100000")
    assert not ok and "Maximum" in err

    ok, amount, _ = validate_amount("500")
    assert ok
    assert amount == Decimal("500.00")


def test_format_price_accepts_decimal_and_float():
    assert format_price(Decimal("19.99")) == "$19.99"
    assert format_price(19.99) == "$19.99"
    assert format_price(20) == "$20.00"
