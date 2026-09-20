from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


class StateError(RuntimeError):
    pass


@dataclass(slots=True)
class FeedState:
    last_price: str
    last_observed_at_ms: int
    last_published_at_ms: int


class JsonStateStore:
    def __init__(self, path: Path):
        self.path = path
        self._feeds: dict[str, FeedState] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raise StateError("state root must be an object")
            if raw.get("version") != 1 or not isinstance(raw.get("feeds"), dict):
                raise StateError("unsupported state format")
            for coin, value in raw["feeds"].items():
                price = Decimal(str(value["last_price"]))
                if not price.is_finite() or price <= 0:
                    raise StateError(f"invalid stored price for {coin}")
                observed_at_ms = int(value["last_observed_at_ms"])
                published_at_ms = int(value["last_published_at_ms"])
                if observed_at_ms < 0 or published_at_ms < 0:
                    raise StateError(f"invalid stored timestamp for {coin}")
                self._feeds[coin] = FeedState(
                    last_price=str(price),
                    last_observed_at_ms=observed_at_ms,
                    last_published_at_ms=published_at_ms,
                )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise StateError(f"cannot read state file {self.path}: {exc}") from exc

    def previous_price(self, coin: str) -> Decimal | None:
        state = self._feeds.get(coin)
        return Decimal(state.last_price) if state else None

    def update(self, coin: str, price: Decimal, observed_at_ms: int, published_at_ms: int) -> None:
        if not price.is_finite() or price <= 0:
            raise StateError(f"refusing invalid state price for {coin}")
        if observed_at_ms < 0 or published_at_ms < 0:
            raise StateError(f"refusing invalid state timestamp for {coin}")
        self._feeds[coin] = FeedState(str(price), observed_at_ms, published_at_ms)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "feeds": {coin: asdict(state) for coin, state in sorted(self._feeds.items())}}
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, prefix=f".{self.path.name}.", delete=False
            ) as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = handle.name
            os.replace(temporary_path, self.path)
        except OSError as exc:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
            raise StateError(f"cannot persist state file {self.path}: {exc}") from exc
