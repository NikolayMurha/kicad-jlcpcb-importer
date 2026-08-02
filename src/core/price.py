"""Helpers for compact quantity-price ranges in the parts database."""

from __future__ import annotations


def price_for_quantity(quantity: int, prices: str) -> float:
    """Return the price for an inclusive quantity range, or ``-1`` on failure."""

    try:
        ranges = prices.split(",") if prices else []
        if not ranges:
            return -1.0
        parsed = []
        for entry in ranges:
            quantity_range, raw_price = entry.split(":", 1)
            lower, upper = quantity_range.split("-", 1)
            parsed.append(
                (int(lower), int(upper) if upper else None, float(raw_price))
            )
        if quantity <= parsed[0][0]:
            return parsed[0][2]
        for lower, upper, price in parsed:
            if lower <= quantity and (upper is None or quantity <= upper):
                return price
    except (TypeError, ValueError):
        return -1.0
    return -1.0
