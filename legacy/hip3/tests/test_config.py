from pathlib import Path
import unittest

from hip3_oracle.config import ConfigError, load_config, parse_config


ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_example_configuration_loads(self) -> None:
        config = load_config(ROOT / "config" / "example.json")
        self.assertEqual(config.dex, "demo")
        self.assertEqual(config.feeds[0].coin, "AAPL")
        self.assertTrue(config.dry_run)

    def test_rejects_interval_below_protocol_limit(self) -> None:
        with self.assertRaisesRegex(ConfigError, "2500"):
            parse_config({"dex": "demo", "intervalMs": 2499, "sources": {}, "feeds": []})

    def test_rejects_false_independence_quorum(self) -> None:
        raw = {
            "dex": "demo",
            "sources": {
                "a": {"kind": "static", "independenceGroup": "same", "prices": {"X": "1"}},
                "b": {"kind": "static", "independenceGroup": "same", "prices": {"X": "1"}},
            },
            "feeds": [
                {
                    "coin": "X",
                    "szDecimals": 2,
                    "sources": ["a", "b"],
                    "minSources": 2,
                    "minIndependentGroups": 2,
                }
            ],
        }
        with self.assertRaisesRegex(ConfigError, "independent source groups"):
            parse_config(raw)

    def test_rejects_string_boolean(self) -> None:
        with self.assertRaisesRegex(ConfigError, "boolean"):
            parse_config({"dex": "demo", "dryRun": "false", "sources": {}, "feeds": []})


if __name__ == "__main__":
    unittest.main()
