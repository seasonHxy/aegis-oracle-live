from decimal import Decimal
from pathlib import Path
import unittest

from hip3_oracle.config import AppConfig, FeedConfig
from hip3_oracle.models import AggregatePrice, MarketStatus
from hip3_oracle.publisher import PublishError, SdkPublisher, build_payload


def aggregate(coin: str, price: str) -> AggregatePrice:
    return AggregatePrice(
        coin=coin,
        price=Decimal(price),
        confidence=Decimal("0.01"),
        confidence_bps=Decimal("1"),
        observed_at_ms=1,
        source_count=3,
        independent_group_count=3,
        market_status=MarketStatus.OPEN,
        sources=("a", "b", "c"),
    )


class PayloadTests(unittest.TestCase):
    def config(self) -> AppConfig:
        return AppConfig(
            dex="demo",
            network="testnet",
            interval_ms=3000,
            dry_run=True,
            state_file=Path("state.json"),
            feeds=(
                FeedConfig("TSLA", 2, ("a", "b", "c")),
                FeedConfig("AAPL", 2, ("a", "b", "c")),
            ),
            sources={},
        )

    def test_action_tuples_are_lexicographically_sorted(self) -> None:
        payload = build_payload(
            self.config(),
            {"TSLA": aggregate("TSLA", "390.22"), "AAPL": aggregate("AAPL", "230.2")},
        )
        action = payload.as_action()["setOracle"]
        self.assertEqual([item[0] for item in action["oraclePxs"]], ["AAPL", "TSLA"])
        self.assertEqual(action["markPxs"], [])
        self.assertEqual(action["externalPerpPxs"], action["oraclePxs"])

    def test_partial_batch_is_refused(self) -> None:
        with self.assertRaises(PublishError):
            build_payload(self.config(), {"AAPL": aggregate("AAPL", "230.2")})


class FakeExchange:
    def __init__(self, response):
        self.response = response
        self.call = None

    def perp_deploy_set_oracle(self, dex, oracle_pxs, mark_pxs, external_perp_pxs):
        self.call = (dex, oracle_pxs, mark_pxs, external_perp_pxs)
        return self.response


class LiveAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_calls_official_sdk_shape(self) -> None:
        config = PayloadTests().config()
        payload = build_payload(
            config,
            {"TSLA": aggregate("TSLA", "390.22"), "AAPL": aggregate("AAPL", "230.2")},
        )
        exchange = FakeExchange({"status": "ok", "response": {"type": "default"}})
        publisher = SdkPublisher(config, private_key="not-used-by-fake")
        publisher._exchange = exchange
        result = await publisher.publish(payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(exchange.call[0], "demo")
        self.assertEqual(exchange.call[1], {"AAPL": "230.2", "TSLA": "390.22"})

    async def test_rejected_sdk_response_raises(self) -> None:
        config = PayloadTests().config()
        payload = build_payload(
            config,
            {"TSLA": aggregate("TSLA", "390.22"), "AAPL": aggregate("AAPL", "230.2")},
        )
        publisher = SdkPublisher(config, private_key="not-used-by-fake")
        publisher._exchange = FakeExchange({"status": "err", "response": "rejected"})
        with self.assertRaises(PublishError):
            await publisher.publish(payload)


if __name__ == "__main__":
    unittest.main()
