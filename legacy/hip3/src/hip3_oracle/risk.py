from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .aggregation import BPS
from .config import FeedConfig
from .models import AggregatePrice, MarketStatus


class RiskDecision(str, Enum):
    PUBLISH = "publish"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class RiskResult:
    decision: RiskDecision
    reason: str
    deviation_bps: Decimal = Decimal(0)


class CircuitBreaker:
    def evaluate(
        self,
        feed: FeedConfig,
        aggregate: AggregatePrice,
        previous_price: Decimal | None,
    ) -> RiskResult:
        if feed.block_when_market_closed and aggregate.market_status is MarketStatus.CLOSED:
            return RiskResult(RiskDecision.BLOCK, "market is closed")
        if feed.block_when_market_unknown and aggregate.market_status is MarketStatus.UNKNOWN:
            return RiskResult(RiskDecision.BLOCK, "market status is unknown")
        if previous_price is None:
            return RiskResult(RiskDecision.PUBLISH, "initial price")
        if previous_price <= 0:
            return RiskResult(RiskDecision.BLOCK, "stored reference price is invalid")
        deviation_bps = abs(aggregate.price - previous_price) * BPS / previous_price
        if deviation_bps > feed.max_jump_bps:
            return RiskResult(
                RiskDecision.BLOCK,
                f"jump {deviation_bps:.4f} bps exceeds {feed.max_jump_bps} bps",
                deviation_bps,
            )
        return RiskResult(RiskDecision.PUBLISH, "within limits", deviation_bps)
