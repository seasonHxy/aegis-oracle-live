from decimal import Decimal
import unittest

from hip3_oracle.config import FeedConfig
from hip3_oracle.models import AggregatePrice, MarketStatus
from hip3_oracle.risk import CircuitBreaker, RiskDecision


def aggregate(price: str, status: MarketStatus = MarketStatus.OPEN) -> AggregatePrice:
    return AggregatePrice(
        coin="AAPL",
        price=Decimal(price),
        confidence=Decimal("0.01"),
        confidence_bps=Decimal("1"),
        observed_at_ms=1,
        source_count=3,
        independent_group_count=3,
        market_status=status,
        sources=("a", "b", "c"),
    )


class CircuitBreakerTests(unittest.TestCase):
    def test_large_jump_is_blocked(self) -> None:
        config = FeedConfig("AAPL", 2, ("a", "b", "c"), max_jump_bps=Decimal("1000"))
        result = CircuitBreaker().evaluate(config, aggregate("120"), Decimal("100"))
        self.assertEqual(result.decision, RiskDecision.BLOCK)

    def test_first_price_is_allowed(self) -> None:
        config = FeedConfig("AAPL", 2, ("a", "b", "c"))
        result = CircuitBreaker().evaluate(config, aggregate("100"), None)
        self.assertEqual(result.decision, RiskDecision.PUBLISH)

    def test_closed_market_policy_is_enforced(self) -> None:
        config = FeedConfig("AAPL", 2, ("a", "b", "c"), block_when_market_closed=True)
        result = CircuitBreaker().evaluate(config, aggregate("100", MarketStatus.CLOSED), Decimal("100"))
        self.assertEqual(result.decision, RiskDecision.BLOCK)


if __name__ == "__main__":
    unittest.main()
