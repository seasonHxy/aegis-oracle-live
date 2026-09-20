from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from .config import FeedConfig
from .models import AggregatePrice, MarketStatus, Quote, RejectedQuote


BPS = Decimal("10000")


class AggregationError(RuntimeError):
    pass


def weighted_quantile(quotes: list[Quote], quantile: Decimal, *, key=lambda quote: quote.price) -> Decimal:
    if not quotes:
        raise AggregationError("cannot calculate a quantile without quotes")
    ordered = sorted(quotes, key=key)
    total = sum((quote.weight for quote in ordered), Decimal(0))
    target = total * quantile
    cumulative = Decimal(0)
    for quote in ordered:
        cumulative += quote.weight
        if cumulative >= target:
            return key(quote)
    return key(ordered[-1])


def _market_status(quotes: Iterable[Quote]) -> MarketStatus:
    statuses = {quote.market_status for quote in quotes}
    if MarketStatus.OPEN in statuses and MarketStatus.CLOSED in statuses:
        return MarketStatus.UNKNOWN
    if MarketStatus.CLOSED in statuses:
        return MarketStatus.CLOSED
    if MarketStatus.OPEN in statuses:
        return MarketStatus.OPEN
    return MarketStatus.UNKNOWN


class PriceAggregator:
    def aggregate(self, feed: FeedConfig, quotes: Iterable[Quote], *, now_ms: int) -> AggregatePrice:
        accepted: list[Quote] = []
        rejected: list[RejectedQuote] = []
        seen: set[str] = set()

        for quote in quotes:
            reason = self._validate_quote(feed, quote, now_ms, seen)
            if reason:
                rejected.append(RejectedQuote(quote.source, reason))
                continue
            seen.add(quote.source)
            accepted.append(quote)

        self._require_quorum(feed, accepted, "before outlier filtering")
        grouped = self._collapse_groups(accepted)
        center = weighted_quantile(grouped, Decimal("0.5"))
        deviations = [
            Quote(
                coin=quote.coin,
                source=quote.source,
                independence_group=quote.independence_group,
                price=abs(quote.price - center),
                observed_at_ms=quote.observed_at_ms,
                received_at_ms=quote.received_at_ms,
                weight=quote.weight,
            )
            for quote in grouped
        ]
        mad = weighted_quantile(deviations, Decimal("0.5"))
        minimum_band = center * feed.outlier_min_band_bps / BPS
        outlier_band = max(mad * feed.outlier_mad_multiplier, minimum_band)

        filtered_groups: list[Quote] = []
        rejected_groups: set[str] = set()
        for quote in grouped:
            if abs(quote.price - center) > outlier_band:
                rejected_groups.add(quote.independence_group)
            else:
                filtered_groups.append(quote)
        filtered = [quote for quote in accepted if quote.independence_group not in rejected_groups]
        rejected.extend(
            RejectedQuote(quote.source, "MAD outlier group")
            for quote in accepted
            if quote.independence_group in rejected_groups
        )
        self._require_quorum(feed, filtered, "after outlier filtering")

        price = weighted_quantile(filtered_groups, Decimal("0.5"))
        q25 = weighted_quantile(filtered_groups, Decimal("0.25"))
        q75 = weighted_quantile(filtered_groups, Decimal("0.75"))
        confidence = max(abs(price - q25), abs(q75 - price))
        confidence_bps = confidence * BPS / price
        if confidence_bps > feed.max_confidence_bps:
            raise AggregationError(
                f"{feed.coin} confidence {confidence_bps:.4f} bps exceeds {feed.max_confidence_bps} bps"
            )

        return AggregatePrice(
            coin=feed.coin,
            price=price,
            confidence=confidence,
            confidence_bps=confidence_bps,
            observed_at_ms=min(quote.observed_at_ms for quote in filtered),
            source_count=len(filtered),
            independent_group_count=len({quote.independence_group for quote in filtered}),
            market_status=_market_status(filtered),
            sources=tuple(sorted(quote.source for quote in filtered)),
            rejected=tuple(rejected),
        )

    @staticmethod
    def _collapse_groups(quotes: list[Quote]) -> list[Quote]:
        """Give one upstream group one vote even when it is exposed through multiple APIs."""
        by_group: dict[str, list[Quote]] = {}
        for quote in quotes:
            by_group.setdefault(quote.independence_group, []).append(quote)
        grouped: list[Quote] = []
        for group, members in by_group.items():
            grouped.append(
                Quote(
                    coin=members[0].coin,
                    source=f"group:{group}",
                    independence_group=group,
                    price=weighted_quantile(members, Decimal("0.5")),
                    observed_at_ms=min(member.observed_at_ms for member in members),
                    received_at_ms=max(member.received_at_ms for member in members),
                    # Multiple endpoints for one upstream must not accumulate voting weight.
                    weight=max(member.weight for member in members),
                    market_status=_market_status(members),
                )
            )
        return grouped

    @staticmethod
    def _validate_quote(feed: FeedConfig, quote: Quote, now_ms: int, seen: set[str]) -> str | None:
        if quote.coin != feed.coin:
            return "wrong coin"
        if quote.source not in feed.sources:
            return "source is not configured for feed"
        if quote.source in seen:
            return "duplicate source"
        if not quote.price.is_finite() or quote.price <= 0:
            return "price is not finite and positive"
        if not quote.weight.is_finite() or quote.weight <= 0:
            return "weight is not finite and positive"
        if quote.observed_at_ms > now_ms + feed.max_future_ms:
            return "future timestamp"
        if now_ms - quote.observed_at_ms > feed.max_source_age_ms:
            return "stale timestamp"
        if quote.bid is not None or quote.ask is not None:
            if quote.bid is None or quote.ask is None:
                return "bid and ask must be supplied together"
            if not quote.bid.is_finite() or not quote.ask.is_finite():
                return "invalid bid/ask"
            if quote.bid <= 0 or quote.ask <= 0 or quote.bid > quote.ask:
                return "invalid bid/ask"
            mid = (quote.bid + quote.ask) / Decimal(2)
            spread_bps = (quote.ask - quote.bid) * BPS / mid
            if spread_bps > feed.max_spread_bps:
                return "spread exceeds limit"
        return None

    @staticmethod
    def _require_quorum(feed: FeedConfig, quotes: list[Quote], stage: str) -> None:
        if len(quotes) < feed.min_sources:
            raise AggregationError(f"{feed.coin} has {len(quotes)} valid sources {stage}; requires {feed.min_sources}")
        groups = len({quote.independence_group for quote in quotes})
        if groups < feed.min_independent_groups:
            raise AggregationError(
                f"{feed.coin} has {groups} independent groups {stage}; requires {feed.min_independent_groups}"
            )
