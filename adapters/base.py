"""
adapters/base.py — Source-adapter contracts.

An "adapter" is anything that talks to an external observability backend
(SigNoz today; Datadog/Prometheus/etc. later) and hands back Hyperion's own
canonical types (core.span.Span, core.metric.Metric). Adapters never talk to
the reasoning engine directly, and the reasoning engine never imports an
adapter — the ingestion layer is the only thing between them.

This keeps the dependency direction one-way:

    adapters/<vendor>/  --(produces)-->  core.Span / core.Metric  --(consumed by)-->  reasoning/

No adapter should import from reasoning/, context/, output/, or dashboard/.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from core.metric import Metric
from core.span import Span


class TraceSourceAdapter(ABC):
    """Contract for anything that can fetch traces from an external backend
    and normalize them into Hyperion's Span model."""

    @abstractmethod
    def fetch_spans(
        self,
        start: datetime,
        end: datetime,
        service_name: str | None = None,
    ) -> list[Span]:
        """Fetch spans for [start, end) and return normalized Span objects.

        Must never raise on malformed upstream data — log and skip instead,
        matching the contract of ingestion/trace_ingester.py.
        """
        raise NotImplementedError


class MetricSourceAdapter(ABC):
    """Contract for anything that can fetch metrics from an external backend
    and normalize them into Hyperion's Metric model."""

    @abstractmethod
    def fetch_metrics(
        self,
        start: datetime,
        end: datetime,
        node_id: str | None = None,
    ) -> list[Metric]:
        """Fetch metrics for [start, end) and return normalized Metric objects.

        Must never raise on malformed upstream data — log and skip instead,
        matching the contract of ingestion/metric_ingester.py.
        """
        raise NotImplementedError
