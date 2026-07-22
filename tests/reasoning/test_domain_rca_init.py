"""
Smoke tests for reasoning/domain_rca/__init__.py (investigate_all).

Tests dispatch correctness, neighbour scan hint collection, already_investigated
filtering, candidate ordering, and graceful handling of missing graph nodes.
No external dependencies — all graphs and nodes are built inline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import networkx as nx

from core.nodes import (
    DatabaseNode,
    ExternalDepNode,
    NodeType,
    QueueNode,
    ServiceNode,
)
from core.span import Span, SpanKind
from reasoning.contracts import (
    CandidateNode,
    DomainRCAResult,
    InclusionReason,
    RCADomain,
    RankedCandidate,
)
from reasoning.domain_rca import investigate_all

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_service_candidate(
    node_id: str,
    priority: float = 0.5,
    causal_confidence: float = 0.5,
    is_potential_origin: bool = True,
) -> RankedCandidate:
    return RankedCandidate(
        candidate=CandidateNode(
            node_id=node_id,
            node_type=NodeType.SERVICE,
            inclusion_reason=InclusionReason.METRIC_ANOMALY,
            is_potential_origin=is_potential_origin,
            priority_score=priority,
        ),
        causal_confidence=causal_confidence,
    )


def _make_db_candidate(node_id: str, priority: float = 0.5) -> RankedCandidate:
    return RankedCandidate(
        candidate=CandidateNode(
            node_id=node_id,
            node_type=NodeType.DATABASE,
            inclusion_reason=InclusionReason.METRIC_ANOMALY,
            is_potential_origin=True,
            priority_score=priority,
        ),
        causal_confidence=0.5,
    )


def _make_dep_candidate(node_id: str, priority: float = 0.5) -> RankedCandidate:
    return RankedCandidate(
        candidate=CandidateNode(
            node_id=node_id,
            node_type=NodeType.EXTERNAL_DEP,
            inclusion_reason=InclusionReason.METRIC_ANOMALY,
            is_potential_origin=True,
            priority_score=priority,
        ),
        causal_confidence=0.5,
    )


def _make_queue_candidate(node_id: str, priority: float = 0.5) -> RankedCandidate:
    return RankedCandidate(
        candidate=CandidateNode(
            node_id=node_id,
            node_type=NodeType.QUEUE,
            inclusion_reason=InclusionReason.METRIC_ANOMALY,
            is_potential_origin=True,
            priority_score=priority,
        ),
        causal_confidence=0.5,
    )


def _error_span(service_id: str, offset_seconds: float = 0.0) -> Span:
    t = _T0 + timedelta(seconds=offset_seconds)
    return Span(
        trace_id="trace1",
        span_id=f"span-{service_id}-{offset_seconds}",
        parent_span_id=None,
        service_id=service_id,
        operation_name="GET /api",
        start_time=t,
        end_time=t + timedelta(seconds=1),
        duration_ms=1000.0,
        kind=SpanKind.SERVER,
        otel_status_code=2,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_service_candidate_dispatches_to_service_engine():
    """SERVICE candidate returns EnrichedCandidate with domain=APPLICATION_CODE."""
    G = nx.DiGraph()
    G.add_node("svc", data=ServiceNode(id="svc", name="My Service"))

    result = investigate_all(
        ranked_candidates=[_make_service_candidate("svc")],
        graph=G,
        spans=[],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=None,
        already_investigated=set(),
        iteration=1,
    )

    assert isinstance(result, DomainRCAResult)
    assert len(result.candidates) == 1
    assert result.candidates[0].domain == RCADomain.APPLICATION_CODE


def test_database_candidate_dispatches_to_database_engine():
    """DATABASE candidate returns EnrichedCandidate with domain=DATABASE."""
    G = nx.DiGraph()
    G.add_node("pg", data=DatabaseNode(id="pg", name="Postgres", db_type="postgresql"))

    result = investigate_all(
        ranked_candidates=[_make_db_candidate("pg")],
        graph=G,
        spans=[],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=None,
        already_investigated=set(),
        iteration=1,
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].domain == RCADomain.DATABASE


def test_external_dep_candidate_dispatches_to_dependency_engine():
    """EXTERNAL_DEP candidate returns EnrichedCandidate with domain=DEPENDENCY."""
    G = nx.DiGraph()
    G.add_node("stripe", data=ExternalDepNode(id="stripe", name="Stripe API"))

    result = investigate_all(
        ranked_candidates=[_make_dep_candidate("stripe")],
        graph=G,
        spans=[],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=None,
        already_investigated=set(),
        iteration=1,
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].domain == RCADomain.DEPENDENCY


def test_queue_candidate_dispatches_to_queue_engine():
    """QUEUE candidate returns EnrichedCandidate with domain=QUEUE and empty evidence."""
    G = nx.DiGraph()
    G.add_node("q", data=QueueNode(id="q", name="Order Queue", queue_type="kafka"))

    result = investigate_all(
        ranked_candidates=[_make_queue_candidate("q")],
        graph=G,
        spans=[],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=None,
        already_investigated=set(),
        iteration=1,
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].domain == RCADomain.QUEUE
    assert result.candidates[0].evidence == []


def test_node_not_in_graph_is_skipped():
    """A RankedCandidate whose node_id is absent from the graph is skipped silently."""
    G = nx.DiGraph()  # empty — "ghost" not in graph

    result = investigate_all(
        ranked_candidates=[_make_service_candidate("ghost")],
        graph=G,
        spans=[],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=None,
        already_investigated=set(),
        iteration=1,
    )

    assert result.candidates == []
    assert result.hints == []


def test_neighbour_scan_hints_collected():
    """
    Upstream predecessor with error spans in the incident window produces
    a non-empty hints list after investigate_all.
    """
    G = nx.DiGraph()
    G.add_node("upstream")
    G.add_node("candidate", data=ServiceNode(id="candidate", name="Candidate"))
    G.add_node("downstream")
    G.add_edge("upstream", "candidate")
    G.add_edge("candidate", "downstream")

    spans = [_error_span("upstream", offset_seconds=10)]

    result = investigate_all(
        ranked_candidates=[_make_service_candidate("candidate")],
        graph=G,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=None,
        already_investigated=set(),
        iteration=1,
    )

    assert len(result.candidates) == 1
    assert len(result.hints) > 0
    hint_node_ids = {h.node_id for h in result.hints}
    assert "upstream" in hint_node_ids


def test_already_investigated_nodes_not_re_hinted():
    """
    Nodes in already_investigated are skipped by neighbour scan —
    no hint is emitted for them even when they have error spans.
    """
    G = nx.DiGraph()
    G.add_node("upstream")
    G.add_node("candidate", data=ServiceNode(id="candidate", name="Candidate"))
    G.add_node("downstream")
    G.add_edge("upstream", "candidate")
    G.add_edge("candidate", "downstream")

    spans = [_error_span("upstream", offset_seconds=10)]

    result = investigate_all(
        ranked_candidates=[_make_service_candidate("candidate")],
        graph=G,
        spans=spans,
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=None,
        already_investigated={"upstream"},
        iteration=1,
    )

    hint_node_ids = {h.node_id for h in result.hints}
    assert "upstream" not in hint_node_ids


def test_multiple_candidates_processed_in_order():
    """
    Two candidates (SERVICE then DATABASE) are returned in the same order
    with correct domains.
    """
    G = nx.DiGraph()
    G.add_node("svc", data=ServiceNode(id="svc", name="Service"))
    G.add_node("pg", data=DatabaseNode(id="pg", name="Postgres", db_type="postgresql"))

    result = investigate_all(
        ranked_candidates=[
            _make_service_candidate("svc"),
            _make_db_candidate("pg"),
        ],
        graph=G,
        spans=[],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=None,
        already_investigated=set(),
        iteration=1,
    )

    assert len(result.candidates) == 2
    assert result.candidates[0].node_id == "svc"
    assert result.candidates[0].domain == RCADomain.APPLICATION_CODE
    assert result.candidates[1].node_id == "pg"
    assert result.candidates[1].domain == RCADomain.DATABASE
