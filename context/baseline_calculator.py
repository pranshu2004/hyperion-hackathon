"""
Metric baseline calculator.

Computes rolling baselines (mean, std, EMA) per node per metric type from
pre-incident metrics. Baselines are used by the scorer to compute deviation
magnitude and evidence strength.
Stateful within an incident window.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime
from dataclasses import dataclass, field

from core.metric import Metric, MetricType
from core.nodes import NodeType

logger = logging.getLogger(__name__)

EMA_ALPHA = 0.3  # Higher = more weight to recent observations.


@dataclass
class BaselineStats:
    """
    Baseline statistics for one node + metric_type combination.
    Computed from pre-incident metrics.
    """

    node_id: str
    metric_type: MetricType
    mean: float
    std: float
    ema: float
    sample_count: int
    min_value: float
    max_value: float

    @property
    def is_reliable(self) -> bool:
        """True if we have enough samples for reliable statistics (>= 3)."""
        return self.sample_count >= 3


def _compute_ema(values: list[float], alpha: float = EMA_ALPHA) -> float:
    """
    Compute exponential moving average over an ordered list of values.
    Earlier values are older, later values are more recent.

    Formula: ema = alpha * current + (1 - alpha) * previous_ema
    Initialized with the first value.

    Returns 0.0 on empty list. Never raises.
    """
    if not values:
        return 0.0
    ema = values[0]
    for v in values[1:]:
        ema = alpha * v + (1.0 - alpha) * ema
    return ema


def _compute_stats(
    node_id: str,
    metric_type: MetricType,
    values: list[float],
) -> BaselineStats:
    """
    Compute BaselineStats from a list of observed values.

    Uses population std (divide by N). Floors std at 0.01 to prevent
    division-by-zero in downstream z-score calculations.
    Returns stats with 0.0 defaults on empty values. Never raises.
    """
    if not values:
        return BaselineStats(
            node_id=node_id,
            metric_type=metric_type,
            mean=0.0,
            std=0.01,
            ema=0.0,
            sample_count=0,
            min_value=0.0,
            max_value=0.0,
        )

    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    std = max(math.sqrt(variance), 0.01)
    ema = _compute_ema(values)

    return BaselineStats(
        node_id=node_id,
        metric_type=metric_type,
        mean=mean,
        std=std,
        ema=ema,
        sample_count=n,
        min_value=min(values),
        max_value=max(values),
    )


def compute_baselines(
    metrics: list[Metric],
    incident_start: datetime,
) -> dict[tuple[str, MetricType], BaselineStats]:
    """
    Compute baseline statistics for all nodes from pre-incident metrics.

    Metrics with timestamp < incident_start are used for baseline computation;
    metrics at or after incident_start are ignored.

    Returns a dict mapping (node_id, MetricType) → BaselineStats.
    Only includes nodes/metrics with at least 1 pre-incident sample.
    Returns empty dict if no pre-incident metrics are found.
    Never raises.
    """
    # Group pre-incident metric values by (node_id, metric_type).
    grouped: dict[tuple[str, MetricType], list[tuple[datetime, float]]] = defaultdict(list)

    for m in metrics:
        ts = m.timestamp
        # Make comparison timezone-consistent.
        if ts.tzinfo is None and incident_start.tzinfo is not None:
            from datetime import timezone
            ts = ts.replace(tzinfo=timezone.utc)
        elif ts.tzinfo is not None and incident_start.tzinfo is None:
            ts = ts.replace(tzinfo=None)

        if ts < incident_start:
            grouped[(m.node_id, m.metric_type)].append((ts, m.value))

    if not grouped:
        logger.warning(
            "compute_baselines: no pre-incident metrics found "
            "(incident_start=%s, total_metrics=%d)",
            incident_start.isoformat(),
            len(metrics),
        )
        return {}

    result: dict[tuple[str, MetricType], BaselineStats] = {}
    for (node_id, metric_type), ts_values in grouped.items():
        # Sort oldest-first before computing EMA so older values influence less.
        ts_values.sort(key=lambda tv: tv[0])
        values = [v for _, v in ts_values]
        result[(node_id, metric_type)] = _compute_stats(node_id, metric_type, values)

    return result


ANOMALY_THRESHOLD = 3.0  # z-score threshold for anomaly detection


@dataclass
class DeviationResult:
    """
    Anomaly deviation result for one node + metric_type combination.
    """

    node_id: str
    metric_type: MetricType
    current_value: float
    baseline_mean: float
    baseline_std: float
    z_score: float
    is_anomalous: bool
    baseline_reliable: bool


def compute_deviation(
    current_value: float,
    baseline: BaselineStats,
) -> DeviationResult:
    """
    Compute deviation of a current metric value from its baseline.

    z_score = (current_value - baseline.mean) / baseline.std
    Capped at 20.0, floored at -5.0.
    Never raises.
    """
    z = (current_value - baseline.mean) / baseline.std
    z = max(-5.0, min(20.0, z))
    return DeviationResult(
        node_id=baseline.node_id,
        metric_type=baseline.metric_type,
        current_value=current_value,
        baseline_mean=baseline.mean,
        baseline_std=baseline.std,
        z_score=z,
        is_anomalous=z >= ANOMALY_THRESHOLD,
        baseline_reliable=baseline.is_reliable,
    )


def get_current_metrics(
    metrics: list[Metric],
    incident_start: datetime,
    lookback_seconds: int = 300,
) -> dict[tuple[str, MetricType], float]:
    """
    Extract current metric values for the incident window.

    Returns mean value per (node_id, MetricType) for metrics within
    [incident_start, incident_start + lookback_seconds].
    Never raises.
    """
    from datetime import timedelta

    incident_end = incident_start + timedelta(seconds=lookback_seconds)
    grouped: dict[tuple[str, MetricType], list[float]] = defaultdict(list)

    for m in metrics:
        ts = m.timestamp
        if ts.tzinfo is None and incident_start.tzinfo is not None:
            from datetime import timezone
            ts = ts.replace(tzinfo=timezone.utc)
        elif ts.tzinfo is not None and incident_start.tzinfo is None:
            ts = ts.replace(tzinfo=None)

        if incident_start <= ts < incident_end:
            grouped[(m.node_id, m.metric_type)].append(m.value)

    return {
        key: sum(vals) / len(vals)
        for key, vals in grouped.items()
    }


def get_anomalous_nodes(
    metrics: list[Metric],
    baselines: dict[tuple[str, MetricType], BaselineStats],
    incident_start: datetime,
    lookback_seconds: int = 300,
) -> list[DeviationResult]:
    """
    Identify anomalous nodes by comparing current metrics to baselines.

    Returns DeviationResult objects where is_anomalous=True, sorted by
    z_score descending. Only includes results where baseline.is_reliable.
    Never raises.
    """
    current = get_current_metrics(metrics, incident_start, lookback_seconds)
    results: list[DeviationResult] = []

    for (node_id, metric_type), value in current.items():
        baseline = baselines.get((node_id, metric_type))
        if baseline is None or not baseline.is_reliable:
            continue
        dev = compute_deviation(value, baseline)
        if dev.is_anomalous:
            results.append(dev)

    results.sort(key=lambda d: d.z_score, reverse=True)
    return results


def summarize_baselines(
    baselines: dict[tuple[str, MetricType], BaselineStats],
) -> dict[str, dict[str, float]]:
    """
    Summarize baselines into a readable dict for logging/debugging.

    Returns {node_id: {metric_type_value: mean, ...}, ...}.
    Never raises.
    """
    summary: dict[str, dict[str, float]] = {}
    for (node_id, metric_type), stats in baselines.items():
        summary.setdefault(node_id, {})[metric_type.value] = stats.mean
    return summary
