from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hip3_oracle.state import JsonStateStore, StateError


class StateTests(unittest.TestCase):
    def test_state_round_trip(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = JsonStateStore(path)
            state.update("AAPL", Decimal("230.2"), 100, 200)
            state.save()
            loaded = JsonStateStore(path)
            self.assertEqual(loaded.previous_price("AAPL"), Decimal("230.2"))

    def test_corrupt_state_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(StateError):
                JsonStateStore(path)

    def test_non_finite_state_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                '{"version":1,"feeds":{"AAPL":{"last_price":"NaN","last_observed_at_ms":1,"last_published_at_ms":2}}}',
                encoding="utf-8",
            )
            with self.assertRaises(StateError):
                JsonStateStore(path)


if __name__ == "__main__":
    unittest.main()
