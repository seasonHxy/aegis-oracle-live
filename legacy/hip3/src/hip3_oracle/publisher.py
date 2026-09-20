from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .config import AppConfig
from .formatting import format_hip3_price
from .models import AggregatePrice


class PublishError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Hip3Payload:
    dex: str
    oracle_pxs: Mapping[str, str]
    mark_pxs: tuple[Mapping[str, str], ...]
    external_perp_pxs: Mapping[str, str]

    def as_action(self) -> dict[str, Any]:
        return {
            "type": "perpDeploy",
            "setOracle": {
                "dex": self.dex,
                "oraclePxs": [[key, value] for key, value in sorted(self.oracle_pxs.items())],
                "markPxs": [
                    [[key, value] for key, value in sorted(price_set.items())] for price_set in self.mark_pxs
                ],
                "externalPerpPxs": [[key, value] for key, value in sorted(self.external_perp_pxs.items())],
            },
        }


def build_payload(config: AppConfig, aggregates: Mapping[str, AggregatePrice]) -> Hip3Payload:
    feed_by_coin = {feed.coin: feed for feed in config.feeds}
    if set(aggregates) != set(feed_by_coin):
        missing = sorted(set(feed_by_coin) - set(aggregates))
        raise PublishError(f"refusing partial HIP-3 update; missing feeds: {', '.join(missing)}")
    prices = {
        coin: format_hip3_price(aggregate.price, feed_by_coin[coin].sz_decimals)
        for coin, aggregate in sorted(aggregates.items())
    }
    marks: tuple[Mapping[str, str], ...] = (dict(prices),) if config.mark_price_sets == 1 else ()
    return Hip3Payload(
        dex=config.dex,
        oracle_pxs=prices,
        mark_pxs=marks,
        external_perp_pxs=dict(prices),
    )


class Publisher(Protocol):
    async def publish(self, payload: Hip3Payload) -> Any: ...


class DryRunPublisher:
    def __init__(self, *, emit: bool = True):
        self.emit = emit
        self.last_payload: Hip3Payload | None = None

    async def publish(self, payload: Hip3Payload) -> dict[str, Any]:
        self.last_payload = payload
        action = payload.as_action()
        if self.emit:
            print(json.dumps(action, indent=2, sort_keys=True))
        return {"status": "dry-run", "action": action}


class SdkPublisher:
    """Live publisher backed by Hyperliquid's official Python SDK."""

    def __init__(self, config: AppConfig, *, private_key: str):
        if not private_key:
            raise PublishError("HIP3_PRIVATE_KEY is required for live publishing")
        self.config = config
        self.private_key = private_key
        self._last_publish_monotonic = 0.0
        self._exchange: Any = None

    @classmethod
    def from_environment(cls, config: AppConfig) -> "SdkPublisher":
        if os.environ.get("HIP3_ENABLE_LIVE") != "YES":
            raise PublishError("set HIP3_ENABLE_LIVE=YES to acknowledge live testnet publishing")
        if config.network == "mainnet" and os.environ.get("HIP3_ENABLE_MAINNET") != "YES":
            raise PublishError("set HIP3_ENABLE_MAINNET=YES to acknowledge mainnet publishing risk")
        return cls(config, private_key=os.environ.get("HIP3_PRIVATE_KEY", ""))

    def _get_exchange(self) -> Any:
        if self._exchange is not None:
            return self._exchange
        try:
            import eth_account
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL
        except ImportError as exc:
            raise PublishError("install live dependencies with: pip install -e '.[live]'") from exc
        wallet = eth_account.Account.from_key(self.private_key)
        self.private_key = ""
        base_url = self.config.api_url or (MAINNET_API_URL if self.config.network == "mainnet" else TESTNET_API_URL)
        self._exchange = Exchange(wallet, base_url)
        return self._exchange

    async def publish(self, payload: Hip3Payload) -> Any:
        elapsed = time.monotonic() - self._last_publish_monotonic
        if self._last_publish_monotonic and elapsed < 2.5:
            raise PublishError(f"local rate limit: only {elapsed:.3f}s since the previous setOracle")
        result = await asyncio.to_thread(self._publish_sync, payload)
        self._last_publish_monotonic = time.monotonic()
        return result

    def _publish_sync(self, payload: Hip3Payload) -> Any:
        exchange = self._get_exchange()
        result = exchange.perp_deploy_set_oracle(
            payload.dex,
            dict(payload.oracle_pxs),
            [dict(item) for item in payload.mark_pxs],
            dict(payload.external_perp_pxs),
        )
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise PublishError(f"Hyperliquid rejected setOracle: {result!r}")
        return result
