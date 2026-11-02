"""Processor fee schedule.

Fees are computed in integer cents to avoid floating-point drift, then
converted back to dollars at the edges.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# method -> (percentage rate, fixed fee in dollars, cap in dollars or None)
FEE_SCHEDULE: dict[str, tuple[Decimal, Decimal, Decimal | None]] = {
    "ACH": (Decimal("0.008"), Decimal("0.00"), Decimal("5.00")),
    "CARD": (Decimal("0.029"), Decimal("0.30"), None),
    "WIRE": (Decimal("0.000"), Decimal("15.00"), None),
}

CENT = Decimal("0.01")


def to_money(value: float | str | Decimal) -> Decimal:
    """Normalize any numeric input to a 2-decimal Decimal."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def expected_fee(amount: float | Decimal, method: str) -> Decimal:
    """Return the fee the processor should charge for a gross amount."""
    method = method.upper()
    if method not in FEE_SCHEDULE:
        raise ValueError(f"Unknown payment method: {method!r}")
    rate, fixed, cap = FEE_SCHEDULE[method]
    fee = to_money(to_money(amount) * rate + fixed)
    if cap is not None:
        fee = min(fee, cap)
    return fee
