"""
Stage 3 Domain RCA for ExternalDepNode candidates.
Stub implementation — emits basic HTTP error and caller scope evidence.
Full implementation in V2: error code pattern analysis, retry storm
detection, temporal pattern analysis.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import networkx as nx

from context.baseline_calculator import BaselineStats
from core.metric import Metric, MetricType
from core.nodes import ExternalDepNode
from core.span import Span
from reasoning.contracts import (
    EnrichedCandidate,
    FailureType,
    RCADomain,
    RankedCandidate,
)
from reasoning.evidence_builders import (
    make_caller_scope_evidence,
    make_http_error_evidence,
    make_missing_telemetry_evidence,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

INCIDENT_WINDOW_SECONDS: int = 300
PRIORITY_BOOST_ALL_CALLERS:  float = 0.15
PRIORITY_BOOST_PARTIAL:      float = 0.05
PRIORITY_PENALTY_NO_EVIDENCE: float = 0.10


def investigate(
    candidate: RankedCandidate,
    node: ExternalDepNode,
    graph: nx.DiGraph,
    spans: list[Span],
    metrics: list[Metric],
    baselines: dict[tuple[str, MetricType], BaselineStats],
    incident_start: datetime,
    llm_client: Any | None,
    iteration: int,
) -> EnrichedCandidate:
    """
    Investigate an ExternalDepNode candidate using span HTTP error evidence
    and caller scope analysis.

    Identifies the most common 5xx status code from calling spans, checks
    which graph predecessors are affected, and classifies the failure type.
    Parameters metrics, baselines, and llm_client are reserved for V2.
    """
    # TODO (V2): implement full dependency RCA:
    # - Distinguish error code patterns (503 vs 429 vs 401 vs timeout)
    # - Detect retry storm signatures (escalating span timing)
    # - Analyse temporal pattern (sudden vs gradual degradation)
    # - Assess blast radius against all known callers in graph

    node_id = candidate.candidate.node_id
    inc_start_utc = _to_utc(incident_start)
    window_end = inc_start_utc + timedelta(seconds=INCIDENT_WINDOW_SECONDS)

    # Spans calling this dependency identified via peer.service attribute
    calling_spans = [
        s for s in spans
        if s.attributes.get("peer.service") == node_id
        and _in_window(s.start_time, inc_start_utc, window_end)
    ]

    # Count HTTP status codes across all calling spans
    status_counts: dict[int, int] = defaultdict(int)
    for span in calling_spans:
        raw = (
            span.attributes.get("http.status_code")
            or span.attributes.get("http.response.status_code")
        )
        if raw is not None:
            try:
                status_counts[int(raw)] += 1
            except (ValueError, TypeError):
                pass

    # Graph predecessors are the callers of this dependency
    predecessors = list(graph.predecessors(node_id)) if node_id in graph else []
    total_callers = len(predecessors)

    # Unique calling service_ids that have error spans
    affected_caller_ids = {
        s.service_id for s in calling_spans if s.otel_status_code == 2
    }
    affected_callers = len(affected_caller_ids)

    evidence = []
    has_5xx = False
    has_429 = False
    has_timeout = False

    # ---- HTTP 5xx error evidence ----------------------------------------
    five_xx = {
        code: count
        for code, count in status_counts.items()
        if 500 <= code < 600
    }
    if five_xx:
        most_common = max(five_xx, key=lambda c: five_xx[c])
        evidence.append(make_http_error_evidence(
            node_id=node_id,
            status_code=most_common,
            error_count=five_xx[most_common],
            total_count=len(calling_spans),
            duration_minutes=INCIDENT_WINDOW_SECONDS / 60.0,
        ))
        has_5xx = True

    if status_counts.get(429, 0) > 0:
        has_429 = True

    # ---- Timeout detection via error.type attribute ---------------------
    for span in calling_spans:
        error_type = str(span.attributes.get("error.type", "")).lower()
        if "timeout" in error_type or "connection" in error_type:
            has_timeout = True
            break

    # ---- Caller scope evidence -----------------------------------------
    evidence.append(make_caller_scope_evidence(
        dep_node_id=node_id,
        affected_callers=affected_callers,
        total_callers=total_callers,
    ))

    # ---- Missing telemetry fallback ------------------------------------
    if not calling_spans:
        evidence.append(make_missing_telemetry_evidence(
            node_id=node_id,
            span_count=0,
            has_metrics=False,
        ))

    # ---- Failure type assignment ----------------------------------------
    if has_5xx:
        failure_type = FailureType.UPSTREAM_OUTAGE
    elif has_429:
        failure_type = FailureType.RATE_LIMITED
    elif has_timeout:
        failure_type = FailureType.TIMEOUT
    else:
        failure_type = FailureType.EXTERNAL_DEP_FAILURE

    # ---- Priority update ------------------------------------------------
    has_error_evidence = has_5xx or has_429 or has_timeout
    all_callers_affected = total_callers > 0 and affected_callers == total_callers
    current_priority = candidate.candidate.priority_score

    if all_callers_affected and has_error_evidence:
        new_priority = min(1.0, current_priority + PRIORITY_BOOST_ALL_CALLERS)
    elif has_error_evidence:
        new_priority = min(1.0, current_priority + PRIORITY_BOOST_PARTIAL)
    else:
        new_priority = max(0.0, current_priority - PRIORITY_PENALTY_NO_EVIDENCE)

    return EnrichedCandidate(
        node_id=node_id,
        node_type=candidate.candidate.node_type,
        domain=RCADomain.DEPENDENCY,
        failure_type=failure_type,
        evidence=evidence,
        is_potential_origin=candidate.candidate.is_potential_origin,
        priority_score=new_priority,
        causal_confidence=candidate.causal_confidence,
        iteration_found=iteration,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _to_utc(dt: datetime) -> datetime:
    """Naive datetime → attach UTC. Aware datetime → convert to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _in_window(
    event_ts: datetime,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    """True if event_ts falls within [window_start, window_end]."""
    ts = _to_utc(event_ts)
    return window_start <= ts <= window_end
