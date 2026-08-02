"""Tests for quantity-price range selection."""

import unittest

from src.core.price import price_for_quantity


def test_ranges_are_inclusive() -> None:
    prices = "1-199:0.019,200-599:0.016,600-:0.015"
    assert price_for_quantity(1, prices) == 0.019
    assert price_for_quantity(199, prices) == 0.019
    assert price_for_quantity(200, prices) == 0.016
    assert price_for_quantity(599, prices) == 0.016
    assert price_for_quantity(600, prices) == 0.015


def test_quantity_below_first_minimum_uses_first_price() -> None:
    assert price_for_quantity(0, "1-:0.5") == 0.5


def test_invalid_price_returns_sentinel() -> None:
    assert price_for_quantity(1, "") == -1.0
    assert price_for_quantity(1, "broken") == -1.0


class PriceTests(unittest.TestCase):
    def test_inclusive_ranges(self) -> None:
        test_ranges_are_inclusive()

    def test_below_minimum(self) -> None:
        test_quantity_below_first_minimum_uses_first_price()

    def test_invalid(self) -> None:
        test_invalid_price_returns_sentinel()
