from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Mapping

from .aggregation import PriceAggregator
from .config import AppConfig, FeedConfig
from .models import AggregatePrice, Quote
from .publisher import Publisher, build_payload
from .risk import CircuitBreaker, RiskDecision
from .sources import PriceSource
from .state import JsonStateStore


@dataclass(frozen=True, slots=True)
class FeedCycle:
    coin: str
    aggregate: AggregatePrice | None
    error: str | None
    source_errors: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class CycleResult:
    published: bool
    reason: str
    feeds: Mapping[str, FeedCycle]
    publisher_response: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "published": self.published,
            "reason": self.reason,
            "feeds": {
                coin: {
                    "aggregate": item.aggregate.as_dict() if item.aggregate else None,
                    "error": item.error,
                    "sourceErrors": dict(item.source_errors),
                }
                for coin, item in sorted(self.feeds.items())
            },
            "publisherResponse": self.publisher_response,
        }


class OracleService:
    def __init__(
        self,
        config: AppConfig,
        sources: Mapping[str, PriceSource],
        publisher: Publisher,
        state: JsonStateStore,
        *,
        aggregator: PriceAggregator | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ):
        self.config = config
        self.sources = sources
        self.publisher = publisher
        self.state = state
        self.aggregator = aggregator or PriceAggregator()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()

    async def cycle(self) -> CycleResult:
        outcomes = await asyncio.gather(*(self._process_feed(feed) for feed in self.config.feeds))
        feeds = {outcome.coin: outcome for outcome in outcomes}
        failures = [outcome for outcome in outcomes if outcome.error]
        if failures:
            reason = "; ".join(f"{item.coin}: {item.error}" for item in failures)
            return CycleResult(False, f"fail-closed batch: {reason}", feeds)

        aggregates = {item.coin: item.aggregate for item in outcomes if item.aggregate is not None}
        try:
            payload = build_payload(self.config, aggregates)
            response = await self.publisher.publish(payload)
        except Exception as exc:
            return CycleResult(False, f"publish failed: {exc}", feeds)

        published_at_ms = int(time.time() * 1000)
        for aggregate in aggregates.values():
            self.state.update(aggregate.coin, aggregate.price, aggregate.observed_at_ms, published_at_ms)
        self.state.save()
        return CycleResult(True, "published", feeds, response)

    async def _process_feed(self, feed: FeedConfig) -> FeedCycle:
        tasks = [self.sources[source_name].get_quote(feed.coin) for source_name in feed.sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        quotes: list[Quote] = []
        source_errors: dict[str, str] = {}
        for source_name, result in zip(feed.sources, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                source_errors[source_name] = str(result)
            else:
                quotes.append(result)
        try:
            now_ms = int(time.time() * 1000)
            aggregate = self.aggregator.aggregate(feed, quotes, now_ms=now_ms)
            decision = self.circuit_breaker.evaluate(feed, aggregate, self.state.previous_price(feed.coin))
            if decision.decision is RiskDecision.BLOCK:
                return FeedCycle(feed.coin, aggregate, f"circuit breaker: {decision.reason}", source_errors)
            return FeedCycle(feed.coin, aggregate, None, source_errors)
        except Exception as exc:
            return FeedCycle(feed.coin, None, str(exc), source_errors)

    async def run_forever(self) -> None:
        while True:
            started = time.monotonic()
            result = await self.cycle()
            print(self._summary(result), flush=True)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0, self.config.interval_ms / 1000 - elapsed))

    @staticmethod
    def _summary(result: CycleResult) -> str:
        timestamp = int(time.time() * 1000)
        if result.published:
            prices = ", ".join(
                f"{coin}={item.aggregate.price}" for coin, item in sorted(result.feeds.items()) if item.aggregate
            )
            return f"{timestamp} published {prices}"
        return f"{timestamp} blocked {result.reason}"
