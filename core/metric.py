"""
Normalized metric signal dataclass.

Represents a single metric observation (e.g. p99 latency, error rate) after
ingestion normalization. Carries service name, metric name/type, value,
timestamp, and any relevant labels. All metric ingesters must produce this type.

Depends only on shared core node type definitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from core.nodes import NodeType


class MetricType(str, Enum):
    LATENCY_P99 = "latency_p99"
    LATENCY_P50 = "latency_p50"
    ERROR_RATE = "error_rate"
    REQUEST_RATE = "request_rate"
    CONSUMER_LAG = "consumer_lag"
    SATURATION = "saturation"


@dataclass
class Metric:
    node_id: str
    node_type: NodeType
    metric_type: MetricType
    value: float
    timestamp: datetime
    window_seconds: int
    unit: str
    tags: dict[str, Any] = field(default_factory=dict)
