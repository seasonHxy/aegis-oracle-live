from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any


class MarketStatus(str, Enum):
    UNKNOWN = "unknown"
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class Quote:
    coin: str
    source: str
    independence_group: str
    price: Decimal
    observed_at_ms: int
    received_at_ms: int
    weight: Decimal = Decimal("1")
    market_status: MarketStatus = MarketStatus.UNKNOWN
    bid: Decimal | None = None
    ask: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RejectedQuote:
    source: str
    reason: str


@dataclass(frozen=True, slots=True)
class AggregatePrice:
    coin: str
    price: Decimal
    confidence: Decimal
    confidence_bps: Decimal
    observed_at_ms: int
    source_count: int
    independent_group_count: int
    market_status: MarketStatus
    sources: tuple[str, ...]
    rejected: tuple[RejectedQuote, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "coin": self.coin,
            "price": str(self.price),
            "confidence": str(self.confidence),
            "confidenceBps": str(self.confidence_bps),
            "observedAtMs": self.observed_at_ms,
            "sourceCount": self.source_count,
            "independentGroupCount": self.independent_group_count,
            "marketStatus": self.market_status.value,
            "sources": list(self.sources),
            "rejected": [{"source": item.source, "reason": item.reason} for item in self.rejected],
        }
