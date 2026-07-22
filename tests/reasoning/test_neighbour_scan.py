"""
Tests for reasoning/neighbour_scan.py

All graphs and node objects are built inline — no simulator topology dependency.
Run with: pytest tests/reasoning/test_neighbour_scan.py
"""

from datetime import datetime, timedelta, timezone

import networkx as nx
import pytest

from core.change_event import DeployEvent
from core.nodes import ExternalDepNode, NodeType, ServiceNode
from core.span import Span, SpanKind
from reasoning.contracts import HintDirection, HintSignal, HintStrength
from reasoning.neighbour_scan import scan

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

INCIDENT_START = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _span(
    service_id: str,
    otel_status_code: int = 0,
    attributes: dict | None = None,
    offset_seconds: int = 10,
) -> Span:
    """Build a Span in the incident window for service_id."""
    ts = INCIDENT_START + timedelta(seconds=offset_seconds)
    return Span(
        trace_id="trace-1",
        span_id=f"span-{service_id}-{offset_seconds}",
        parent_span_id=None,
        service_id=service_id,
        operation_name="GET /",
        start_time=ts,
        end_time=ts + timedelta(milliseconds=50),
        duration_ms=50.0,
        kind=SpanKind.SERVER,
        otel_status_code=otel_status_code,
        attributes=attributes or {},
    )


def _error_span(service_id: str, attributes: dict | None = None, offset_seconds: int = 10) -> Span:
    return _span(service_id, otel_status_code=2, attributes=attributes, offset_seconds=offset_seconds)


def _service_node(node_id: str, deploys: list | None = None) -> ServiceNode:
    return ServiceNode(
        id=node_id,
        name=node_id,
        recent_deploys=deploys or [],
    )


def _deploy(service_id: str, minutes_before: float) -> DeployEvent:
    return DeployEvent(
        event_id=f"deploy-{service_id}",
        service_id=service_id,
        timestamp=INCIDENT_START - timedelta(minutes=minutes_before),
        version="v1.0.1",
        deploy_scope=[service_id],
    )


def _graph(*edges: tuple[str, str], node_data: dict | None = None) -> nx.DiGraph:
    """Build a DiGraph from (source, target) edge tuples with optional node data dict."""
    g = nx.DiGraph()
    for src, tgt in edges:
        g.add_edge(src, tgt)
    if node_data:
        for node_id, data in node_data.items():
            if node_id not in g.nodes:
                g.add_node(node_id)
            g.nodes[node_id]["data"] = data
    return g


# ---------------------------------------------------------------------------
# Test 1 — 503 on successor emits STRONG / ERROR_CODE_PATTERN
# ---------------------------------------------------------------------------

def test_503_on_successor_emits_strong():
    """Successor with an HTTP 503 span → STRONG ERROR_CODE_PATTERN hint."""
    g = _graph(("candidate", "dep"))
    dep_span = _span("dep", otel_status_code=2, attributes={"http.status_code": 503})
    spans = [dep_span]

    hints = scan(
        candidate_node_id="candidate",
        graph=g,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=INCIDENT_START,
        already_investigated=set(),
        iteration=1,
    )

    assert len(hints) == 1
    h = hints[0]
    assert h.hint_strength == HintStrength.STRONG
    assert h.signal == HintSignal.ERROR_CODE_PATTERN
    assert h.direction == HintDirection.SUCCESSOR
    assert h.node_id == "dep"
    assert h.suggesting_node_id == "candidate"


# ---------------------------------------------------------------------------
# Test 2 — Change event + error span on predecessor → STRONG / CHANGE_EVENT
# ---------------------------------------------------------------------------

def test_change_event_and_error_on_predecessor_emits_strong():
    """Predecessor with deploy 10 min ago and an error span → STRONG CHANGE_EVENT hint."""
    g = _graph(
        ("upstream", "candidate"),
        node_data={
            "upstream": _service_node("upstream", deploys=[_deploy("upstream", minutes_before=10)])
        },
    )
    spans = [_error_span("upstream")]

    hints = scan(
        candidate_node_id="candidate",
        graph=g,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=INCIDENT_START,
        already_investigated=set(),
        iteration=1,
    )

    assert len(hints) == 1
    h = hints[0]
    assert h.hint_strength == HintStrength.STRONG
    assert h.signal == HintSignal.CHANGE_EVENT
    assert h.direction == HintDirection.PREDECESSOR
    assert h.node_id == "upstream"


# ---------------------------------------------------------------------------
# Test 3 — Silent degradation on successor → STRONG / SILENT_DEGRADATION
# ---------------------------------------------------------------------------

def test_silent_degradation_on_successor_emits_strong():
    """Successor returns HTTP 200 (no OTel errors) but candidate has error spans → STRONG SILENT_DEGRADATION."""
    g = _graph(("candidate", "dep"))
    spans = [
        _error_span("candidate"),
        _span("dep", otel_status_code=0, attributes={"http.status_code": 200}),
    ]

    hints = scan(
        candidate_node_id="candidate",
        graph=g,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=INCIDENT_START,
        already_investigated=set(),
        iteration=1,
    )

    assert len(hints) >= 1
    dep_hints = [h for h in hints if h.node_id == "dep"]
    assert len(dep_hints) == 1
    h = dep_hints[0]
    assert h.signal == HintSignal.SILENT_DEGRADATION
    assert h.hint_strength == HintStrength.STRONG
    assert h.direction == HintDirection.SUCCESSOR


# ---------------------------------------------------------------------------
# Test 4 — Already investigated node is skipped
# ---------------------------------------------------------------------------

def test_already_investigated_node_is_skipped():
    """When dep is in already_investigated, scan returns nothing."""
    g = _graph(("candidate", "dep"))
    dep_span = _span("dep", otel_status_code=2, attributes={"http.status_code": 503})
    spans = [dep_span]

    hints = scan(
        candidate_node_id="candidate",
        graph=g,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=INCIDENT_START,
        already_investigated={"dep"},
        iteration=1,
    )

    assert hints == []


# ---------------------------------------------------------------------------
# Test 5 — No spans and no change event → no hint
# ---------------------------------------------------------------------------

def test_no_spans_no_change_event_emits_nothing():
    """Successor with no spans and no change events → no hint."""
    g = _graph(("candidate", "dep"))

    hints = scan(
        candidate_node_id="candidate",
        graph=g,
        spans=[],
        metrics=[],
        baselines={},
        incident_start=INCIDENT_START,
        already_investigated=set(),
        iteration=1,
    )

    assert hints == []


# ---------------------------------------------------------------------------
# Test 6 — Error spans only, no 503/429, no change event, no metrics → MODERATE / MISSING_TELEMETRY
# ---------------------------------------------------------------------------

def test_error_span_only_no_metrics_emits_moderate_missing_telemetry():
    """
    Successor with error span but no 503/429/timeout, no change event, no metrics
    → MODERATE MISSING_TELEMETRY (no metric coverage + error spans).
    """
    g = _graph(("candidate", "dep"))
    spans = [_error_span("dep")]

    hints = scan(
        candidate_node_id="candidate",
        graph=g,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=INCIDENT_START,
        already_investigated=set(),
        iteration=1,
    )

    assert len(hints) == 1
    h = hints[0]
    assert h.hint_strength == HintStrength.MODERATE
    assert h.signal == HintSignal.MISSING_TELEMETRY


# ---------------------------------------------------------------------------
# Test 7 — Both directions scanned
# ---------------------------------------------------------------------------

def test_both_directions_scanned():
    """upstream → candidate → dep: upstream has error span, dep has 429 → two hints."""
    g = _graph(("upstream", "candidate"), ("candidate", "dep"))
    spans = [
        _error_span("upstream"),
        _span("dep", otel_status_code=2, attributes={"http.status_code": 429}),
    ]

    hints = scan(
        candidate_node_id="candidate",
        graph=g,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=INCIDENT_START,
        already_investigated=set(),
        iteration=1,
    )

    assert len(hints) == 2

    pred_hints = [h for h in hints if h.direction == HintDirection.PREDECESSOR]
    succ_hints = [h for h in hints if h.direction == HintDirection.SUCCESSOR]

    assert len(pred_hints) == 1
    assert pred_hints[0].node_id == "upstream"

    assert len(succ_hints) == 1
    assert succ_hints[0].node_id == "dep"
    assert succ_hints[0].signal == HintSignal.ERROR_CODE_PATTERN
    assert succ_hints[0].hint_strength == HintStrength.STRONG


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------

def test_429_on_successor_emits_strong():
    """HTTP 429 is treated the same as 503 → STRONG ERROR_CODE_PATTERN."""
    g = _graph(("candidate", "dep"))
    spans = [_span("dep", otel_status_code=2, attributes={"http.status_code": 429})]

    hints = scan(
        candidate_node_id="candidate",
        graph=g,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=INCIDENT_START,
        already_investigated=set(),
        iteration=1,
    )

    assert len(hints) == 1
    assert hints[0].hint_strength == HintStrength.STRONG
    assert hints[0].signal == HintSignal.ERROR_CODE_PATTERN


def test_timeout_keyword_in_error_type_emits_strong():
    """error.type containing 'timeout' → STRONG ERROR_CODE_PATTERN."""
    g = _graph(("candidate", "dep"))
    spans = [_span("dep", otel_status_code=2, attributes={"error.type": "ReadTimeoutError"})]

    hints = scan(
        candidate_node_id="candidate",
        graph=g,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=INCIDENT_START,
        already_investigated=set(),
        iteration=1,
    )

    assert len(hints) == 1
    assert hints[0].hint_strength == HintStrength.STRONG
    assert hints[0].signal == HintSignal.ERROR_CODE_PATTERN


def test_no_hint_for_candidate_node_itself():
    """Self-loop: scan never emits a hint pointing at candidate_node_id."""
    g = nx.DiGraph()
    g.add_edge("candidate", "candidate")
    spans = [_error_span("candidate")]

    hints = scan(
        candidate_node_id="candidate",
        graph=g,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=INCIDENT_START,
        already_investigated=set(),
        iteration=1,
    )

    assert all(h.node_id != "candidate" for h in hints)


def test_span_outside_incident_window_not_counted():
    """A span that starts before incident_start is outside the window and ignored."""
    g = _graph(("candidate", "dep"))
    before_window = _span("dep", otel_status_code=2, offset_seconds=-60)  # 60s before start
    spans = [before_window]

    hints = scan(
        candidate_node_id="candidate",
        graph=g,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=INCIDENT_START,
        already_investigated=set(),
        iteration=1,
    )

    assert hints == []


def test_iteration_number_propagated():
    """iteration_found on returned hints matches the iteration parameter."""
    g = _graph(("candidate", "dep"))
    spans = [_span("dep", otel_status_code=2, attributes={"http.status_code": 503})]

    hints = scan(
        candidate_node_id="candidate",
        graph=g,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=INCIDENT_START,
        already_investigated=set(),
        iteration=3,
    )

    assert len(hints) == 1
    assert hints[0].iteration_found == 3


def test_non_service_node_no_change_event():
    """ExternalDepNode neighbours never trigger CHANGE_EVENT — no change_event tracking."""
    g = _graph(("candidate", "dep"))
    # dep has no data attribute — not a ServiceNode, so no change events possible
    # Give dep an error span only → should emit MISSING_TELEMETRY, not CHANGE_EVENT
    spans = [_error_span("dep")]

    hints = scan(
        candidate_node_id="candidate",
        graph=g,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=INCIDENT_START,
        already_investigated=set(),
        iteration=1,
    )

    assert len(hints) == 1
    assert hints[0].signal != HintSignal.CHANGE_EVENT
    assert hints[0].signal == HintSignal.MISSING_TELEMETRY


def test_change_event_outside_lookback_not_detected():
    """A deploy older than CHANGE_EVENT_LOOKBACK_MINUTES is not in the window."""
    g = _graph(
        ("upstream", "candidate"),
        node_data={
            "upstream": _service_node(
                "upstream",
                deploys=[_deploy("upstream", minutes_before=90)],  # 90 min ago — outside 60 min window
            )
        },
    )
    spans = [_error_span("upstream")]

    hints = scan(
        candidate_node_id="candidate",
        graph=g,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=INCIDENT_START,
        already_investigated=set(),
        iteration=1,
    )

    # Error spans exist but no change event → MISSING_TELEMETRY, not CHANGE_EVENT
    assert len(hints) == 1
    assert hints[0].signal == HintSignal.MISSING_TELEMETRY
    assert hints[0].hint_strength == HintStrength.MODERATE


def test_only_one_hint_per_neighbour():
    """A neighbour that qualifies under multiple signals produces exactly one hint."""
    g = _graph(("candidate", "dep"))
    # dep has both 503 AND error span AND (we could have change event too)
    spans = [
        _span("dep", otel_status_code=2, attributes={"http.status_code": 503}),
        _error_span("dep", offset_seconds=20),
    ]

    hints = scan(
        candidate_node_id="candidate",
        graph=g,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=INCIDENT_START,
        already_investigated=set(),
        iteration=1,
    )

    dep_hints = [h for h in hints if h.node_id == "dep"]
    assert len(dep_hints) == 1

# ---------------------------------------------------------------------------
# Tests deferred to V2 (listed here to prevent regression when implemented)
# ---------------------------------------------------------------------------
# - SUB_THRESHOLD_METRIC: change event + metric sample but no actual movement
#   should NOT emit SUB_THRESHOLD_METRIC once baseline comparison is added
# - True sub-threshold movement (below z-score threshold, above baseline) + change event
#   should emit SUB_THRESHOLD_METRIC
# - Metric movement alone (no span corroboration) must emit no hint
# - Error span + metric coverage in window should emit SPAN_ERRORS, not MISSING_TELEMETRY
# - Silent degradation false-positive: candidate errors unrelated to successor
# - Timeout encoded in exception.type or exception.message rather than error.type
# - Naive datetime inputs handled correctly by _to_utc
# - candidate_node_id absent from graph: decide and document policy (guard + warn, or caller contract)
# ---------------------------------------------------------------------------
