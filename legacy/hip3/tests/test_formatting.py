from decimal import Decimal
import unittest

from hip3_oracle.formatting import PriceFormatError, format_hip3_price


class PriceFormattingTests(unittest.TestCase):
    def test_rounds_to_five_significant_figures(self) -> None:
        self.assertEqual(format_hip3_price(Decimal("268.154"), 2), "268.15")

    def test_respects_perp_decimal_limit(self) -> None:
        self.assertEqual(format_hip3_price(Decimal("0.00123456"), 1), "0.00123")

    def test_integer_prices_are_plain_strings(self) -> None:
        self.assertEqual(format_hip3_price(Decimal("123456"), 2), "123456")

    def test_rejects_non_positive_price(self) -> None:
        with self.assertRaises(PriceFormatError):
            format_hip3_price(Decimal("0"), 2)


if __name__ == "__main__":
    unittest.main()
