"""Pure tick-rounding math for paired stops.

Mirrors PriceMath in the NT8 AddOn so both ports behave identically on the
arithmetic. No external dependencies; straightforward to unit-test.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Tuple


def round_to_tick(price: float, tick_size: float) -> float:
    """Round a raw price to the nearest tick-size multiple, half-away-from-zero."""
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    # Use Decimal to avoid binary float drift on typical futures tick sizes (0.25, 0.01).
    p = Decimal(str(price))
    t = Decimal(str(tick_size))
    n = (p / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(n * t)


def compute_pair(
    reference: float, offset_points: float, tick_size: float
) -> Tuple[float, float]:
    """Compute (buy_stop, sell_stop) around a reference price, both tick-rounded."""
    buy = round_to_tick(reference + offset_points, tick_size)
    sell = round_to_tick(reference - offset_points, tick_size)
    return buy, sell


def prices_equal(a: float, b: float, tick_size: float) -> bool:
    """True when two order prices agree within half a tick. Use this instead of ==
    anywhere we compare order prices; it absorbs broker rounding noise."""
    return abs(a - b) < tick_size * 0.5
