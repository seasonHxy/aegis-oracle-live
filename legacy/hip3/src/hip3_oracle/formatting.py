from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN


class PriceFormatError(ValueError):
    pass


def format_hip3_price(value: Decimal, sz_decimals: int) -> str:
    """Format a perp price for Hyperliquid's 5-significant-figure/decimal constraints."""
    if not value.is_finite() or value <= 0:
        raise PriceFormatError("price must be finite and positive")
    if not 0 <= sz_decimals <= 6:
        raise PriceFormatError("sz_decimals must be between 0 and 6")

    # Hyperliquid explicitly allows integer prices regardless of significant figures.
    if value == value.to_integral_value():
        return format(value.quantize(Decimal(1)), "f")

    max_decimal_places = 6 - sz_decimals
    significant_exponent = value.adjusted() - 4
    decimal_exponent = -max_decimal_places
    quantum_exponent = max(significant_exponent, decimal_exponent)
    quantum = Decimal(1).scaleb(quantum_exponent)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_EVEN)
    if rounded <= 0:
        raise PriceFormatError("price rounds to zero at the configured szDecimals")

    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
