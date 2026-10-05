"""Decimal helpers. All amounts in records are Decimal, rounded with explicit rules."""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def D(value) -> Decimal:
    """Convert to Decimal through str so floats from SQLite don't leak binary noise."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal(0)
    return Decimal(str(value))


def q2(value) -> Decimal:
    """Round to cents, half-up (the commercial rounding rule used everywhere)."""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def convert(amount, rate) -> Decimal:
    """Convert once into the document currency; everything is computed from the result."""
    return q2(D(amount) * D(rate))
