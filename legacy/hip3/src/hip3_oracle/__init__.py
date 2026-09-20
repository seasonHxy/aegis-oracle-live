"""HIP-3 multi-source oracle library."""

from .aggregation import AggregationError, PriceAggregator
from .config import AppConfig, ConfigError, FeedConfig, load_config
from .models import AggregatePrice, MarketStatus, Quote
from .publisher import DryRunPublisher, Hip3Payload, SdkPublisher
from .risk import CircuitBreaker, RiskDecision
from .service import OracleService

__all__ = [
    "AggregatePrice",
    "AggregationError",
    "AppConfig",
    "CircuitBreaker",
    "ConfigError",
    "DryRunPublisher",
    "FeedConfig",
    "Hip3Payload",
    "MarketStatus",
    "OracleService",
    "PriceAggregator",
    "Quote",
    "RiskDecision",
    "SdkPublisher",
    "load_config",
]

__version__ = "0.1.0"
