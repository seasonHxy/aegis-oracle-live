from decimal import Decimal
import json
from unittest.mock import patch
import unittest

from hip3_oracle.config import SourceConfig
from hip3_oracle.models import MarketStatus
from hip3_oracle.sources import JsonRestSource


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.payload


class JsonSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_scales_and_maps_quote(self) -> None:
        config = SourceConfig(
            name="provider",
            kind="json",
            independence_group="provider-a",
            weight=Decimal("2"),
            options={
                "url": "https://provider.example/{symbol}",
                "symbols": {"AAPL": "AAPL.US"},
                "pricePath": "data.mid",
                "bidPath": "data.bid",
                "askPath": "data.ask",
                "priceScale": "0.01",
                "timestampPath": "data.ts",
                "timestampUnit": "ms",
                "statusPath": "data.session",
                "statusMap": {"REGULAR": "open"},
            },
        )
        response = FakeResponse(
            {"data": {"mid": "23020", "bid": "23019", "ask": "23021", "ts": 1_800_000_000_000, "session": "regular"}}
        )
        with patch("urllib.request.urlopen", return_value=response):
            result = await JsonRestSource(config).get_quote("AAPL")
        self.assertEqual(result.price, Decimal("230.20"))
        self.assertEqual(result.bid, Decimal("230.19"))
        self.assertEqual(result.ask, Decimal("230.21"))
        self.assertEqual(result.market_status, MarketStatus.OPEN)
        self.assertEqual(result.observed_at_ms, 1_800_000_000_000)


if __name__ == "__main__":
    unittest.main()
