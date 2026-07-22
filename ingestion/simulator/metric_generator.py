from __future__ import annotations

import random
import math
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

_BASELINES: dict[str, dict[str, tuple[float, str]]] = {
    "api-gateway": {
        "latency_p99": (8, "ms"),
        "latency_p50": (5, "ms"),
        "error_rate": (0.1, "%"),
        "request_rate": (500, "rps"),
    },
    "frontend-service": {
        "latency_p99": (20, "ms"),
        "latency_p50": (15, "ms"),
        "error_rate": (0.1, "%"),
        "request_rate": (480, "rps"),
    },
    "checkout-service": {
        "latency_p99": (35, "ms"),
        "latency_p50": (25, "ms"),
        "error_rate": (0.2, "%"),
        "request_rate": (300, "rps"),
    },
    "payment-service": {
        "latency_p99": (110, "ms"),
        "latency_p50": (80, "ms"),
        "error_rate": (0.1, "%"),
        "request_rate": (280, "rps"),
    },
    "fraud-service": {
        "latency_p99": (160, "ms"),
        "latency_p50": (120, "ms"),
        "error_rate": (0.1, "%"),
        "request_rate": (280, "rps"),
    },
    "auth-service": {
        "latency_p99": (28, "ms"),
        "latency_p50": (20, "ms"),
        "error_rate": (0.1, "%"),
        "request_rate": (200, "rps"),
    },
    "catalog-service": {
        "latency_p99": (42, "ms"),
        "latency_p50": (30, "ms"),
        "error_rate": (0.1, "%"),
        "request_rate": (400, "rps"),
    },
    "inventory-service": {
        "latency_p99": (48, "ms"),
        "latency_p50": (35, "ms"),
        "error_rate": (0.2, "%"),
        "request_rate": (280, "rps"),
    },
    "notification-service": {
        "latency_p99": (55, "ms"),
        "latency_p50": (40, "ms"),
        "error_rate": (0.3, "%"),
        "request_rate": (100, "rps"),
    },
    "postgres-payments": {
        "latency_p99": (15, "ms"),
        "latency_p50": (10, "ms"),
        "error_rate": (0.05, "%"),
    },
    "postgres-inventory": {
        "latency_p99": (12, "ms"),
        "latency_p50": (8, "ms"),
        "error_rate": (0.05, "%"),
    },
    "postgres-fraud": {
        "latency_p99": (18, "ms"),
        "latency_p50": (12, "ms"),
        "error_rate": (0.05, "%"),
    },
    "postgres-catalog": {
        "latency_p99": (12, "ms"),
        "latency_p50": (8, "ms"),
        "error_rate": (0.05, "%"),
    },
    "redis-cache": {
        "latency_p99": (3, "ms"),
        "latency_p50": (2, "ms"),
        "error_rate": (0.01, "%"),
    },
    "redis-sessions": {
        "latency_p99": (3, "ms"),
        "latency_p50": (2, "ms"),
        "error_rate": (0.01, "%"),
    },
    "order-queue": {
        "consumer_lag": (50, "messages"),
    },
    "stripe-api": {
        "latency_p99": (280, "ms"),
        "latency_p50": (200, "ms"),
        "error_rate": (0.5, "%"),
    },
    "risk-api": {
        "latency_p99": (210, "ms"),
        "latency_p50": (150, "ms"),
        "error_rate": (0.3, "%"),
    },
    "sms-provider": {
        "latency_p99": (420, "ms"),
        "latency_p50": (300, "ms"),
        "error_rate": (0.8, "%"),
    },
    "email-provider": {
        "latency_p99": (350, "ms"),
        "latency_p50": (250, "ms"),
        "error_rate": (0.5, "%"),
    },
}

_NODE_TYPES: dict[str, str] = {
    "api-gateway": "service",
    "frontend-service": "service",
    "checkout-service": "service",
    "payment-service": "service",
    "fraud-service": "service",
    "auth-service": "service",
    "catalog-service": "service",
    "inventory-service": "service",
    "notification-service": "service",
    "postgres-payments": "database",
    "postgres-inventory": "database",
    "postgres-fraud": "database",
    "postgres-catalog": "database",
    "redis-cache": "database",
    "redis-sessions": "database",
    "order-queue": "queue",
    "stripe-api": "external_dep",
    "risk-api": "external_dep",
    "sms-provider": "external_dep",
    "email-provider": "external_dep",
}


def _apply_noise(metric_type: str, baseline: float) -> float:
    if "latency" in metric_type:
        return max(0.1, random.gauss(baseline, baseline * 0.15))
    if metric_type == "error_rate":
        return max(0.0, random.gauss(baseline, baseline * 0.2))
    if metric_type == "request_rate":
        return max(0.0, random.gauss(baseline, baseline * 0.1))
    if metric_type == "consumer_lag":
        return max(0.0, random.gauss(baseline, baseline * 0.25))
    return baseline


def generate_normal(
    duration_seconds: int = 3600,
    scrape_interval_seconds: int = 60,
    start_time: datetime | None = None,
) -> list[dict]:
    """
    Generate normal baseline metric snapshots for all nodes.

    Args:
        duration_seconds:        Total window to generate metrics for.
                                 Default 3600 = 1 hour.
        scrape_interval_seconds: Interval between snapshots.
                                 Default 60 = 1 minute.
        start_time:              Start of the window. Defaults to
                                 datetime.now(UTC) - duration_seconds.

    Returns:
        List of metric JSON snapshot dicts, one per node per metric
        type per scrape interval.
        Total count = num_nodes_metrics * (duration / interval)
        For default params: ~20 nodes * ~3 metrics avg * 60 intervals
        = ~3600 snapshots.

    Design:
        - Iterate over scrape intervals
        - For each interval, iterate over all nodes in _BASELINES
        - For each node, generate one snapshot per metric type
        - Apply noise model per metric type
        - Round values to 2 decimal places
        - timestamp = start_time + (interval_index * scrape_interval)
    """
    if start_time is None:
        start_time = datetime.now(timezone.utc) - timedelta(seconds=duration_seconds)

    num_intervals = duration_seconds // scrape_interval_seconds
    snapshots: list[dict] = []

    for i in range(num_intervals):
        ts = start_time + timedelta(seconds=i * scrape_interval_seconds)
        ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        for node_id, metrics in _BASELINES.items():
            node_type = _NODE_TYPES[node_id]
            for metric_type, (baseline, unit) in metrics.items():
                value = round(_apply_noise(metric_type, baseline), 2)
                snapshots.append({
                    "node_id": node_id,
                    "node_type": node_type,
                    "metric_type": metric_type,
                    "value": value,
                    "timestamp": ts_str,
                    "window_seconds": scrape_interval_seconds,
                    "unit": unit,
                })

    return snapshots


def generate_failure(
    affected_node_ids: list[str],
    latency_multiplier: float,
    error_rate_multiplier: float,
    duration_seconds: int = 3600,
    scrape_interval_seconds: int = 60,
    start_time: datetime | None = None,
    failure_start_offset_seconds: int = 300,
    target_error_rate: float | None = None,
) -> list[dict]:
    """
    Generate metric snapshots with failure injection for affected nodes.

    Args:
        affected_node_ids:        Nodes showing anomalous metrics.
        latency_multiplier:       Multiply baseline latency by this
                                  for affected nodes post-failure.
        error_rate_multiplier:    Multiply baseline error rate by this
                                  for affected nodes post-failure.
        duration_seconds:         Total window. Default 3600.
        scrape_interval_seconds:  Scrape interval. Default 60.
        start_time:               Start of window. Defaults to
                                  now(UTC) - duration_seconds.
        failure_start_offset_seconds: Seconds after start_time when
                                  failure begins. Pre-failure metrics
                                  are normal for all nodes.

    Returns:
        List of metric JSON snapshot dicts. Same format as
        generate_normal(). Affected nodes show elevated values
        after failure_start_offset_seconds.

    Design:
        - Same iteration pattern as generate_normal()
        - Track whether current interval is pre or post failure
        - Pre-failure: all nodes use normal noise model
        - Post-failure:
            affected nodes latency metrics: baseline * latency_multiplier
              + Gaussian noise (sigma = baseline * 0.1)
            affected nodes error_rate: baseline * error_rate_multiplier
              + Gaussian noise (sigma = baseline * 0.05)
              capped at 100.0
            affected nodes request_rate: slight drop,
              baseline * 0.7 (traffic drops during failure)
            affected nodes consumer_lag: baseline * latency_multiplier
              (queue backs up proportionally)
            unaffected nodes: normal noise model throughout
        - Round all values to 2 decimal places
        - Floor all values at 0.0
    """
    if start_time is None:
        start_time = datetime.now(timezone.utc) - timedelta(seconds=duration_seconds)

    num_intervals = duration_seconds // scrape_interval_seconds
    affected_set = set(affected_node_ids)
    snapshots: list[dict] = []

    for i in range(num_intervals):
        interval_offset = i * scrape_interval_seconds
        ts = start_time + timedelta(seconds=interval_offset)
        ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        is_failure = interval_offset >= failure_start_offset_seconds

        for node_id, metrics in _BASELINES.items():
            node_type = _NODE_TYPES[node_id]
            is_affected = is_failure and node_id in affected_set

            for metric_type, (baseline, unit) in metrics.items():
                if not is_affected:
                    value = _apply_noise(metric_type, baseline)
                elif "latency" in metric_type:
                    mean = baseline * latency_multiplier
                    value = max(0.0, round(random.gauss(mean, mean * 0.15), 2))
                elif metric_type == "error_rate":
                    if target_error_rate is not None:
                        mean = target_error_rate
                    else:
                        mean = baseline * error_rate_multiplier
                    value = max(0.0, min(100.0, random.gauss(mean, mean * 0.05)))
                elif metric_type == "request_rate":
                    mean = baseline * 0.7
                    value = max(0.0, round(random.gauss(mean, mean * 0.12), 2))
                elif metric_type == "consumer_lag":
                    mean = baseline * latency_multiplier
                    value = max(0.0, round(random.gauss(mean, mean * 0.20), 2))
                else:
                    value = _apply_noise(metric_type, baseline)

                snapshots.append({
                    "node_id": node_id,
                    "node_type": node_type,
                    "metric_type": metric_type,
                    "value": round(value, 2),
                    "timestamp": ts_str,
                    "window_seconds": scrape_interval_seconds,
                    "unit": unit,
                })

    return snapshots


def generate_failure_from_scenario(
    scenario_failure_mode,
    duration_seconds: int = 3600,
    scrape_interval_seconds: int = 60,
    start_time: datetime | None = None,
    failure_start_offset_seconds: int = 300,
) -> list[dict]:
    """
    Convenience wrapper that accepts a FailureMode object directly.
    Extracts affected_node_ids and derives multipliers from
    the FailureMode's error_rate field.

    Derives:
        latency_multiplier = scenario_failure_mode.latency_multiplier
        error_rate_multiplier = scenario_failure_mode.error_rate * 100
            (converts 0.0-1.0 fraction to multiplier against baseline %)
            capped at a minimum of 2.0 so there is always a detectable spike

    This allows run_scenario() in failure_injector.py to generate
    metrics with a single call alongside traces.
    """
    latency_multiplier = scenario_failure_mode.latency_multiplier
    target_error_rate = scenario_failure_mode.error_rate * 100.0

    return generate_failure(
        affected_node_ids=scenario_failure_mode.affected_node_ids,
        latency_multiplier=latency_multiplier,
        error_rate_multiplier=1.0,
        target_error_rate=target_error_rate,
        duration_seconds=duration_seconds,
        scrape_interval_seconds=scrape_interval_seconds,
        start_time=start_time,
        failure_start_offset_seconds=failure_start_offset_seconds,
    )
