from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hip3_oracle.config import load_config
from hip3_oracle.publisher import DryRunPublisher
from hip3_oracle.service import OracleService
from hip3_oracle.sources import build_sources
from hip3_oracle.state import JsonStateStore


ROOT = Path(__file__).resolve().parents[1]


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_cycle_filters_outlier_and_publishes(self) -> None:
        config = load_config(ROOT / "config" / "example.json")
        with TemporaryDirectory() as directory:
            publisher = DryRunPublisher(emit=False)
            service = OracleService(
                config,
                build_sources(config.sources),
                publisher,
                JsonStateStore(Path(directory) / "state.json"),
            )
            result = await service.cycle()
            self.assertTrue(result.published)
            self.assertIsNotNone(publisher.last_payload)
            self.assertEqual(publisher.last_payload.oracle_pxs["AAPL"], "230.2")
            aggregate = result.feeds["AAPL"].aggregate
            self.assertIsNotNone(aggregate)
            self.assertEqual(aggregate.source_count, 3)


if __name__ == "__main__":
    unittest.main()
