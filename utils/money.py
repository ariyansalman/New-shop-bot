"""Money handling: one place that turns any numeric input into a Decimal
rounded to cents, the way currency is supposed to round.

Why this exists: `wallet_balance`, `price`, `total_amount` and `amount` used
to be plain `Float` columns, with Python doing `round(x, 2)` on `float`
values throughout the codebase. Two different kinds of error come from that:

1. Binary floats can't represent most decimal fractions exactly (`0.1 + 0.2
   != 0.3`), so repeated wallet debits/credits accumulate drift over time.
2. Python's built-in `round()` uses banker's rounding (round-half-to-even),
   which is not how money is conventionally rounded (round-half-up).

`to_money()` fixes both: it goes through `Decimal(str(value))` (never
`Decimal(value)` directly on a float - that captures the float's ugly exact
binary value instead of what it displays as) and quantizes with
ROUND_HALF_UP. Pair this with `Numeric(12, 2)` columns (see
database/models.py) and every money value in the system is an exact Decimal
from database to Telegram message and back.
"""

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

TWOPLACES = Decimal("0.01")


def to_money(value) -> Decimal:
    """Convert str/float/int/Decimal money input into a Decimal, rounded to
    2 decimal places with round-half-up.

    Raises InvalidOperation/ValueError for unparsable or non-finite input
    ("abc", "inf", "nan"). That last one matters: quantize() on a NaN
    Decimal quietly returns NaN instead of raising (only Infinity does), so
    without an explicit check here a "nan" input would silently turn into a
    NaN money value instead of being rejected - callers that take user input
    should still catch the exception themselves (see
    utils.helpers.validate_amount for the user-facing version).
    """
    if isinstance(value, Decimal):
        d = value
    else:
        # str(float) round-trips to the shortest decimal that reads back as
        # the same float, e.g. str(19.99) == "19.99" - that's what we want.
        # Decimal(19.99) directly would instead capture the binary float's
        # exact (and much uglier) value.
        d = Decimal(str(value))
    if not d.is_finite():
        raise InvalidOperation(f"non-finite money value: {value!r}")
    return d.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def money_or_none(value) -> Decimal | None:
    """Like to_money(), but returns None instead of raising for bad input."""
    try:
        return to_money(value)
    except (InvalidOperation, ValueError, TypeError):
        return None
