"""
Metric ingester.

Parses raw metric JSON snapshots and emits normalized Metric objects
(core.metric.Metric). Stateless — each call processes one independent snapshot.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone

from core.metric import Metric, MetricType
from core.nodes import NodeType

logger = logging.getLogger(__name__)

_METRIC_TYPE_MAP: dict[str, MetricType] = {
    "latency_p99": MetricType.LATENCY_P99,
    "latency_p50": MetricType.LATENCY_P50,
    "error_rate": MetricType.ERROR_RATE,
    "request_rate": MetricType.REQUEST_RATE,
    "consumer_lag": MetricType.CONSUMER_LAG,
    "saturation": MetricType.SATURATION,
}

_NODE_TYPE_MAP: dict[str, NodeType] = {
    "service": NodeType.SERVICE,
    "database": NodeType.DATABASE,
    "queue": NodeType.QUEUE,
    "external_dep": NodeType.EXTERNAL_DEP,
}

_REQUIRED_FIELDS: tuple[str, ...] = (
    "node_id", "node_type", "metric_type", "value", "timestamp", "window_seconds", "unit"
)


def _parse_metric_type(raw: str) -> MetricType | None:
    result = _METRIC_TYPE_MAP.get(raw)
    if result is None:
        logger.warning("Unknown metric_type: %r", raw)
    return result


def _parse_node_type(raw: str) -> NodeType | None:
    result = _NODE_TYPE_MAP.get(raw)
    if result is None:
        logger.warning("Unknown node_type: %r", raw)
    return result


def _parse_timestamp(ts_str: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unparseable timestamp: {ts_str!r}")


def ingest(raw: dict) -> Metric | None:
    if not isinstance(raw, dict):
        logger.warning("ingest() received non-dict input: %r", type(raw))
        return None

    for field in _REQUIRED_FIELDS:
        if field not in raw:
            logger.warning("Missing required field %r in metric snapshot", field)
            return None

    node_id = raw["node_id"]
    if not isinstance(node_id, str) or not node_id:
        logger.warning("Invalid node_id: %r", node_id)
        return None

    node_type = _parse_node_type(raw["node_type"])
    if node_type is None:
        return None

    metric_type = _parse_metric_type(raw["metric_type"])
    if metric_type is None:
        return None

    # Reject booleans explicitly: bool is a subclass of int in Python, so
    # isinstance(True, (int, float)) is True and float(True) == 1.0 — both
    # silently pass without this guard.
    raw_value = raw["value"]
    if isinstance(raw_value, bool):
        logger.warning("Boolean value rejected for node %r metric %r", node_id, raw["metric_type"])
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        logger.warning("Non-numeric value: %r", raw_value)
        return None
    # Reject NaN, +inf, -inf: nan < 0 is False so the guard below does NOT
    # catch NaN. math.isfinite() catches all three non-finite cases.
    if not math.isfinite(value):
        logger.warning(
            "Non-finite value %r for node %r metric %r", value, node_id, raw["metric_type"]
        )
        return None
    if value < 0:
        logger.warning("Negative value %r for node %r metric %r", value, node_id, raw["metric_type"])
        return None

    try:
        timestamp = _parse_timestamp(raw["timestamp"])
    except ValueError as exc:
        logger.warning("Bad timestamp: %s", exc)
        return None

    # Reject booleans for window_seconds: bool is a subclass of int, so
    # int(True) == 1 and True > 0 is True — both silently pass without this guard.
    raw_ws = raw["window_seconds"]
    if isinstance(raw_ws, bool):
        logger.warning("Boolean window_seconds rejected for node %r", node_id)
        return None
    try:
        window_seconds = int(raw_ws)
    except (TypeError, ValueError):
        logger.warning("Non-integer window_seconds: %r", raw_ws)
        return None
    if window_seconds <= 0:
        logger.warning("window_seconds must be positive, got %r", window_seconds)
        return None

    unit = raw["unit"]
    if not isinstance(unit, str):
        logger.warning("Invalid unit: %r", unit)
        return None

    return Metric(
        node_id=node_id,
        node_type=node_type,
        metric_type=metric_type,
        value=value,
        timestamp=timestamp,
        window_seconds=window_seconds,
        unit=unit,
    )


def ingest_batch(raw_metrics: list[dict]) -> list[Metric]:
    if not raw_metrics:
        return []
    metrics: list[Metric] = []
    for raw in raw_metrics:
        result = ingest(raw)
        if result is not None:
            metrics.append(result)
    return metrics


def validate(raw: dict) -> tuple[bool, str]:
    # validate() is a pre-filter for the file-ingestion path; ingest() is the
    # authoritative guard and always runs. Their checks deliberately overlap so
    # that callers who bypass validate() (e.g. ingest_batch()) still get clean
    # output. Keep them in sync; do not collapse into a shared helper.
    if not isinstance(raw, dict):
        return False, f"Expected dict, got {type(raw).__name__}"

    for key in _REQUIRED_FIELDS:
        if key not in raw:
            return False, f"Missing required key: {key!r}"

    if not isinstance(raw["node_id"], str) or not raw["node_id"]:
        return False, "node_id must be a non-empty string"

    # bool is a subclass of int/float in Python, so isinstance(True, (int, float)) is True.
    # Reject booleans explicitly before the numeric type check.
    if isinstance(raw["value"], bool) or not isinstance(raw["value"], (int, float)):
        return False, f"value must be a number, got {type(raw['value']).__name__}"

    raw_ws = raw["window_seconds"]
    if isinstance(raw_ws, bool) or not isinstance(raw_ws, int) or raw_ws <= 0:
        return False, f"window_seconds must be a positive integer, got {raw_ws!r}"

    return True, ""


def ingest_from_file(path: str) -> list[Metric]:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.error("Metric file not found: %s", path)
        return []
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse JSON from %s: %s", path, exc)
        return []

    if isinstance(data, dict):
        ok, reason = validate(data)
        if not ok:
            logger.warning("Invalid metric snapshot in %s: %s", path, reason)
            return []
        result = ingest(data)
        return [result] if result is not None else []

    if isinstance(data, list):
        metrics: list[Metric] = []
        for i, item in enumerate(data):
            ok, reason = validate(item)
            if not ok:
                logger.warning("Skipping snapshot[%d] in %s: %s", i, path, reason)
                continue
            result = ingest(item)
            if result is not None:
                metrics.append(result)
        return metrics

    logger.error("Unexpected JSON structure in %s: expected dict or list", path)
    return []
