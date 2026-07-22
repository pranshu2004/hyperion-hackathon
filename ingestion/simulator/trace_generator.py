"""
Synthetic OTel trace generator.

Generates realistic OpenTelemetry-format trace JSON for both normal operation
and failure scenarios using the topology defined in topology.py. Output format
must be parseable by ingestion/trace_ingester.py without modification.
"""

from __future__ import annotations

import math
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from core.nodes import DatabaseNode, ExternalDepNode, QueueNode
from ingestion.simulator.topology import get_edges, get_nodes

# Derive node classification from topology at import time
_topo = get_nodes()
_DB_NODES: frozenset[str] = frozenset(
    nid for nid, n in _topo.items() if isinstance(n, DatabaseNode)
)
_EXTERNAL_NODES: frozenset[str] = frozenset(
    nid for nid, n in _topo.items() if isinstance(n, ExternalDepNode)
)
_QUEUE_NODES: frozenset[str] = frozenset(
    nid for nid, n in _topo.items() if isinstance(n, QueueNode)
)
# Hostname only (no scheme) — matches what a real span's server.address
# attribute carries. Derived from topology.py's endpoint so there's one
# source of truth for each external dependency's address.
_EXTERNAL_DEP_HOSTS: dict[str, str] = {
    nid: urlparse(n.endpoint).netloc
    for nid, n in _topo.items()
    if isinstance(n, ExternalDepNode) and n.endpoint
}


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class FailureMode:
    affected_node_ids: list[str]
    latency_multiplier: float
    error_rate: float
    error_type: str
    failure_version: str | None = None


# ---------------------------------------------------------------------------
# Internal path tree
# ---------------------------------------------------------------------------


@dataclass
class _CallNode:
    node_id: str
    span_kind: int  # OTel proto: 0=UNSPECIFIED,1=INTERNAL,2=SERVER,3=CLIENT,4=PRODUCER,5=CONSUMER
    children: list[_CallNode] = field(default_factory=list)


# Path 1 — Checkout (60% of traffic)
_CHECKOUT_PATH = _CallNode("api-gateway", 2, [
    _CallNode("frontend-service", 2, [
        _CallNode("checkout-service", 2, [
            _CallNode("payment-service", 2, [
                _CallNode("fraud-service", 2, [
                    _CallNode("postgres-fraud", 3),
                    _CallNode("stripe-api", 3),
                    _CallNode("risk-api", 3),
                ]),
                _CallNode("postgres-payments", 3),
            ]),
            _CallNode("inventory-service", 2),
            _CallNode("order-queue", 4),
        ]),
    ]),
])

# Path 2 — Browse/catalog (30% of traffic)
_CATALOG_PATH = _CallNode("api-gateway", 2, [
    _CallNode("frontend-service", 2, [
        _CallNode("redis-cache", 3),
        _CallNode("catalog-service", 2, [
            _CallNode("postgres-catalog", 3),
        ]),
    ]),
])

# Path 3 — Auth, cache hit (8% of traffic)
_AUTH_PATH_HIT = _CallNode("api-gateway", 2, [
    _CallNode("auth-service", 2, [
        _CallNode("redis-sessions", 3),
    ]),
])

# Path 3 — Auth, cache miss → SMS OTP (2% of traffic)
_AUTH_PATH_MISS = _CallNode("api-gateway", 2, [
    _CallNode("auth-service", 2, [
        _CallNode("redis-sessions", 3),
        _CallNode("sms-provider", 3),
    ]),
])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASELINE_LATENCY_MS: dict[str, float] = {
    "api-gateway": 5.0,
    "frontend-service": 15.0,
    "checkout-service": 25.0,
    "payment-service": 80.0,
    "fraud-service": 120.0,
    "auth-service": 20.0,
    "catalog-service": 30.0,
    "notification-service": 40.0,
    "inventory-service": 35.0,
    "stripe-api": 200.0,
    "risk-api": 150.0,
    "sms-provider": 300.0,
    "email-provider": 250.0,
    "postgres-payments": 10.0,
    "postgres-inventory": 8.0,
    "postgres-fraud": 12.0,
    "postgres-catalog": 8.0,
    "redis-cache": 2.0,
    "redis-sessions": 2.0,
}

_SERVICE_VERSION: dict[str, str] = {
    "payment-service": "v2.3.0",
    "fraud-service": "v1.8.0",
}
_DEFAULT_VERSION = "v1.0.0"

_OPERATION_NAMES: dict[str, str] = {
    "api-gateway": "POST /api/checkout",
    "frontend-service": "render_checkout",
    "checkout-service": "process_checkout",
    "payment-service": "process_payment",
    "fraud-service": "evaluate_transaction",
    "auth-service": "authenticate_user",
    "catalog-service": "get_catalog_items",
    "inventory-service": "check_inventory",
    "notification-service": "send_notification",
    "stripe-api": "POST /v1/charges",
    "risk-api": "POST /v1/evaluate",
    "postgres-payments": "INSERT payments",
    "postgres-fraud": "SELECT fraud_rules",
    "postgres-catalog": "SELECT catalog_items",
    "postgres-inventory": "SELECT inventory",
    "redis-cache": "GET catalog_cache",
    "redis-sessions": "GET session",
    "sms-provider": "POST /send",
    "email-provider": "POST /send",
    "order-queue": "publish order-created",
}

_PATH_OPERATION_OVERRIDES: dict[str, dict[str, str]] = {
    "catalog": {
        "api-gateway": "GET /api/catalog",
        "frontend-service": "render_catalog",
    },
    "auth": {
        "api-gateway": "POST /api/auth",
        "frontend-service": "handle_auth",
    },
}

_HTTP_ROUTES: dict[str, dict[str, str]] = {
    "api-gateway": {
        "checkout": "/api/checkout",
        "catalog": "/api/catalog",
        "auth": "/api/auth",
    },
    "frontend-service": {
        "checkout": "/checkout",
        "catalog": "/catalog",
        "auth": "/auth",
    },
}
_HTTP_ROUTE_DEFAULTS: dict[str, str] = {
    "checkout-service": "/checkout",
    "payment-service": "/payment",
    "fraud-service": "/fraud/evaluate",
    "auth-service": "/auth",
    "catalog-service": "/catalog",
    "inventory-service": "/inventory",
    "notification-service": "/notify",
}

_DB_ATTRS: dict[str, dict[str, str]] = {
    "postgres-payments": {
        "db.system": "postgresql",
        "db.name": "postgres-payments",
        "db.statement": "INSERT INTO payments (id, amount, status) VALUES ($1, $2, $3)",
    },
    "postgres-inventory": {
        "db.system": "postgresql",
        "db.name": "postgres-inventory",
        "db.statement": "SELECT quantity FROM inventory WHERE product_id = $1",
    },
    "postgres-fraud": {
        "db.system": "postgresql",
        "db.name": "postgres-fraud",
        "db.statement": "SELECT rule_id, threshold FROM fraud_rules WHERE active = true",
    },
    "postgres-catalog": {
        "db.system": "postgresql",
        "db.name": "postgres-catalog",
        "db.statement": "SELECT id, name, price FROM catalog_items WHERE category = $1 LIMIT 50",
    },
    "redis-cache": {
        "db.system": "redis",
        "db.name": "redis-cache",
        "db.statement": "GET catalog_cache:category:all",
    },
    "redis-sessions": {
        "db.system": "redis",
        "db.name": "redis-sessions",
        "db.statement": "GET session:abc123",
    },
}

# Hero scenario stacktrace (exact lines required by spec)
_HERO_STACKTRACE = (
    "com.hyperion.fraud.RiskEvaluator.evaluateTransaction(RiskEvaluator.java:47)\n"
    "com.hyperion.fraud.FraudService.evaluate(FraudService.java:112)\n"
    "com.hyperion.fraud.FraudController.post(FraudController.java:89)"
)
_HERO_MESSAGE = (
    'Cannot invoke "Tokenizer.generate(CardData)" because '
    '"this.tokenizer" is null'
)


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------


def _make_trace_id() -> str:
    return uuid.uuid4().hex  # 32 hex chars (128-bit)


def _make_span_id() -> str:
    return uuid.uuid4().hex[:16]  # 16 hex chars (64-bit)


# ---------------------------------------------------------------------------
# Latency + version helpers
# ---------------------------------------------------------------------------


def _sample_latency(
    node_id: str,
    failure_mode: FailureMode | None,
    is_failure_active: bool,
) -> float:
    """Return a log-normally sampled latency in ms (intrinsic, not including children)."""
    p50 = _BASELINE_LATENCY_MS.get(node_id, 50.0)
    if (
        failure_mode is not None
        and is_failure_active
        and node_id in failure_mode.affected_node_ids
    ):
        p50 *= failure_mode.latency_multiplier
    return random.lognormvariate(math.log(max(p50, 0.1)), 0.4)


def _get_version(
    node_id: str,
    failure_mode: FailureMode | None,
    is_failure_active: bool,
) -> str:
    if (
        failure_mode is not None
        and is_failure_active
        and node_id in failure_mode.affected_node_ids
        and failure_mode.failure_version is not None
    ):
        return failure_mode.failure_version
    return _SERVICE_VERSION.get(node_id, _DEFAULT_VERSION)


# ---------------------------------------------------------------------------
# Attribute + event builders
# ---------------------------------------------------------------------------


def _get_operation(node_id: str, path_name: str) -> str:
    overrides = _PATH_OPERATION_OVERRIDES.get(path_name, {})
    return overrides.get(node_id, _OPERATION_NAMES.get(node_id, node_id))


def _get_route(node_id: str, path_name: str) -> str:
    if node_id in _HTTP_ROUTES:
        return _HTTP_ROUTES[node_id].get(path_name, f"/{node_id}")
    return _HTTP_ROUTE_DEFAULTS.get(node_id, f"/{node_id}")


def _build_attrs(
    node_id: str,
    is_error: bool,
    path_name: str,
) -> list[dict[str, Any]]:
    http_status = "503" if is_error else "200"
    attrs: list[dict[str, Any]] = []

    if node_id in _DB_NODES:
        db = _DB_ATTRS.get(node_id, {})
        for k, v in db.items():
            attrs.append({"key": k, "value": {"stringValue": v}})

    elif node_id in _EXTERNAL_NODES:
        attrs.extend([
            {"key": "http.method", "value": {"stringValue": "POST"}},
            {"key": "http.status_code", "value": {"stringValue": http_status}},
            {"key": "peer.service", "value": {"stringValue": node_id}},
        ])
        host = _EXTERNAL_DEP_HOSTS.get(node_id)
        if host:
            attrs.append({"key": "server.address", "value": {"stringValue": host}})

    elif node_id in _QUEUE_NODES:
        attrs.extend([
            {"key": "messaging.system", "value": {"stringValue": "kafka"}},
            {"key": "messaging.destination", "value": {"stringValue": "order-created"}},
            {"key": "messaging.operation", "value": {"stringValue": "publish"}},
        ])

    else:
        # HTTP service span
        method = "GET" if path_name == "catalog" else "POST"
        attrs.extend([
            {"key": "http.method", "value": {"stringValue": method}},
            {"key": "http.status_code", "value": {"stringValue": http_status}},
            {"key": "http.route", "value": {"stringValue": _get_route(node_id, path_name)}},
        ])

    return attrs


def _make_exception_event(
    node_id: str,
    error_type: str,
    time_ns: int,
) -> dict[str, Any]:
    if node_id == "fraud-service":
        stacktrace = _HERO_STACKTRACE
        message = _HERO_MESSAGE
    else:
        svc = node_id.replace("-", ".")
        stacktrace = (
            f"com.hyperion.{svc}.ServiceHandler.process(ServiceHandler.java:42)\n"
            f"com.hyperion.{svc}.Controller.handle(Controller.java:78)"
        )
        message = f"{error_type}: downstream service failure propagated from dependency"

    return {
        "name": "exception",
        "timeUnixNano": str(time_ns),
        "attributes": [
            {"key": "exception.type", "value": {"stringValue": error_type}},
            {"key": "exception.message", "value": {"stringValue": message}},
            {"key": "exception.stacktrace", "value": {"stringValue": stacktrace}},
        ],
    }


# ---------------------------------------------------------------------------
# Core span builder
# ---------------------------------------------------------------------------

# Span entry: (node_id, span_dict, version_string)
_SpanEntry = tuple[str, dict[str, Any], str]


def _build_spans(
    call_node: _CallNode,
    trace_id: str,
    parent_span_id: str | None,
    start_ns: int,
    failure_mode: FailureMode | None,
    is_failure_active: bool,
    path_name: str,
) -> tuple[list[_SpanEntry], int, bool]:
    """
    Recursively build OTel span dicts for a call node and its children.

    Children run sequentially. The parent span wraps all children:
      duration = pre_overhead + sum(child_durations) + post_overhead

    Returns ([(node_id, span_dict, version), ...], end_ns, is_error).
    If any child errored, the parent is forced to error (cascade propagation).
    """
    node_id = call_node.node_id
    span_id = _make_span_id()

    # Own intrinsic processing time (not counting children's wall-clock time).
    # 40% before first child, 10% after last child; the rest is "during" child calls.
    intrinsic_ns = int(_sample_latency(node_id, failure_mode, is_failure_active) * 1_000_000)
    pre_ns = max(intrinsic_ns * 4 // 10, 100_000)   # at least 0.1 ms
    post_ns = max(intrinsic_ns // 10, 100_000)

    child_cursor_ns = start_ns + pre_ns
    all_spans: list[_SpanEntry] = []
    any_child_errored = False

    for child in call_node.children:
        child_result, child_end_ns, child_errored = _build_spans(
            child, trace_id, span_id, child_cursor_ns,
            failure_mode, is_failure_active, path_name,
        )
        all_spans.extend(child_result)
        child_cursor_ns = child_end_ns
        if child_errored:
            any_child_errored = True

    end_ns = child_cursor_ns + post_ns

    own_error_roll = (
        failure_mode is not None
        and is_failure_active
        and node_id in failure_mode.affected_node_ids
        and random.random() < failure_mode.error_rate
    )
    is_error = own_error_roll or any_child_errored

    events: list[dict[str, Any]] = []
    if is_error and failure_mode is not None:
        event_time_ns = start_ns + (end_ns - start_ns) // 2
        events.append(_make_exception_event(node_id, failure_mode.error_type, event_time_ns))

    version = _get_version(node_id, failure_mode, is_failure_active)

    span_dict: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": _get_operation(node_id, path_name),
        "kind": call_node.span_kind,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "status": {"code": 2 if is_error else 1},
        "attributes": _build_attrs(node_id, is_error, path_name),
        "events": events,
    }
    if parent_span_id:
        span_dict["parentSpanId"] = parent_span_id

    return [(node_id, span_dict, version)] + all_spans, end_ns, is_error


def _assemble_trace(span_entries: list[_SpanEntry]) -> dict[str, Any]:
    """Group span entries by service and build the OTel resourceSpans envelope."""
    by_service: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for node_id, span_dict, version in span_entries:
        if node_id not in by_service:
            by_service[node_id] = (version, [])
        by_service[node_id][1].append(span_dict)

    resource_spans = []
    for service_id, (version, spans) in by_service.items():
        resource_spans.append({
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": service_id}},
                    {"key": "service.version", "value": {"stringValue": version}},
                    {"key": "deployment.environment", "value": {"stringValue": "production"}},
                ]
            },
            "scopeSpans": [{"spans": spans}],
        })

    return {"resourceSpans": resource_spans}


def _generate_trace(
    path_root: _CallNode,
    path_name: str,
    trace_start_ns: int,
    failure_mode: FailureMode | None,
    is_failure_active: bool,
) -> dict[str, Any]:
    trace_id = _make_trace_id()
    span_entries, _, _ = _build_spans(
        call_node=path_root,
        trace_id=trace_id,
        parent_span_id=None,
        start_ns=trace_start_ns,
        failure_mode=failure_mode,
        is_failure_active=is_failure_active,
        path_name=path_name,
    )
    return _assemble_trace(span_entries)


def _pick_path() -> tuple[_CallNode, str]:
    r = random.random()
    if r < 0.60:
        return _CHECKOUT_PATH, "checkout"
    elif r < 0.90:
        return _CATALOG_PATH, "catalog"
    else:
        # cache miss ~20% of auth requests
        if random.random() < 0.20:
            return _AUTH_PATH_MISS, "auth"
        return _AUTH_PATH_HIT, "auth"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_normal(
    count: int = 100,
    start_time: datetime | None = None,
) -> list[dict[str, Any]]:
    """
    Generate count normal baseline traces.
    start_time defaults to datetime.now(UTC) - timedelta(hours=1).
    Traces are spread across the time window evenly with small jitter.
    Returns list of OTel trace JSON dicts.
    """
    if start_time is None:
        start_time = datetime.now(timezone.utc) - timedelta(hours=1)

    window_ns = int(3600 * 1e9)
    start_ns = int(start_time.timestamp() * 1e9)
    interval_ns = window_ns // max(count, 1)

    traces = []
    for i in range(count):
        jitter_ns = random.randint(-interval_ns // 4, interval_ns // 4)
        trace_start_ns = start_ns + i * interval_ns + jitter_ns
        path_root, path_name = _pick_path()
        traces.append(_generate_trace(path_root, path_name, trace_start_ns, None, False))

    return traces


def generate_failure(
    failure_mode: FailureMode,
    count: int = 100,
    start_time: datetime | None = None,
    failure_start_offset_seconds: int = 300,
) -> list[dict[str, Any]]:
    """
    Generate count traces with failure injection.
    First failure_start_offset_seconds worth of traces are normal (pre-failure baseline).
    After that, affected nodes show degraded behavior per failure_mode.
    Returns list of OTel trace JSON dicts.
    """
    if start_time is None:
        start_time = datetime.now(timezone.utc) - timedelta(hours=1)

    window_ns = int(3600 * 1e9)
    start_ns = int(start_time.timestamp() * 1e9)
    interval_ns = window_ns // max(count, 1)
    failure_start_ns = start_ns + int(failure_start_offset_seconds * 1e9)

    traces = []
    for i in range(count):
        jitter_ns = random.randint(-interval_ns // 4, interval_ns // 4)
        trace_start_ns = start_ns + i * interval_ns + jitter_ns
        is_failure_active = trace_start_ns >= failure_start_ns
        path_root, path_name = _pick_path()
        traces.append(
            _generate_trace(
                path_root, path_name, trace_start_ns, failure_mode, is_failure_active
            )
        )

    return traces


def get_hero_failure_mode() -> FailureMode:
    """Returns the hero demo failure mode for fraud-service."""
    return FailureMode(
        affected_node_ids=["fraud-service", "payment-service", "checkout-service"],
        latency_multiplier=8.5,
        error_rate=0.85,
        error_type="NullPointerException",
        failure_version="v1.8.1",
    )
