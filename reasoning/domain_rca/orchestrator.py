"""
reasoning/domain_rca/orchestrator.py — Stage 3 orchestrator.

Dispatches each RankedCandidate to the correct domain engine, runs the
neighbour scan per candidate, and returns a DomainRCAResult.

Contains no scoring logic, no causal modeling, no evidence construction.
One public function: investigate_all().
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import networkx as nx

from context.baseline_calculator import BaselineStats
from core.code_change import CodeChangeEvent
from core.metric import Metric, MetricType
from core.nodes import DatabaseNode, ExternalDepNode, NodeType, QueueNode, ServiceNode
from core.span import Span
from reasoning import neighbour_scan
from reasoning.contracts import (
    DomainRCAResult,
    EnrichedCandidate,
    FailureType,
    InvestigationHint,
    RCADomain,
    RankedCandidate,
)
from . import database, dependency, queue, service

logger = logging.getLogger(__name__)


def investigate_all(
    ranked_candidates: list[RankedCandidate],
    graph: nx.DiGraph,
    spans: list[Span],
    metrics: list[Metric],
    baselines: dict[tuple[str, MetricType], BaselineStats],
    incident_start: datetime,
    code_change_event: CodeChangeEvent | None,
    llm_client: Any | None,
    already_investigated: set[str],
    iteration: int,
) -> DomainRCAResult:
    enriched_candidates: list[EnrichedCandidate] = []
    all_hints: list[InvestigationHint] = []

    for candidate in ranked_candidates:
        node_id = candidate.candidate.node_id

        # Step 1: Resolve node data from graph
        if node_id not in graph.nodes:
            logger.warning(
                "investigate_all: node %r not in graph — skipping candidate",
                node_id,
            )
            continue

        node_data = graph.nodes[node_id].get("data")
        if node_data is None:
            logger.warning(
                "investigate_all: node %r has no data attribute in graph"
                " — skipping candidate",
                node_id,
            )
            continue

        # Step 2: Dispatch to domain engine
        node_type = candidate.candidate.node_type
        try:
            if node_type == NodeType.SERVICE:
                if not isinstance(node_data, ServiceNode):
                    logger.warning(
                        "investigate_all: node %r has node_type SERVICE but "
                        "data is %r — skipping",
                        node_id, type(node_data).__name__,
                    )
                    continue
                enriched = service.investigate(
                    candidate=candidate,
                    node=node_data,
                    graph=graph,
                    spans=spans,
                    metrics=metrics,
                    baselines=baselines,
                    incident_start=incident_start,
                    code_change_event=code_change_event,
                    llm_client=llm_client,
                    iteration=iteration,
                )
            elif node_type == NodeType.DATABASE:
                if not isinstance(node_data, DatabaseNode):
                    logger.warning(
                        "investigate_all: node %r has node_type DATABASE but "
                        "data is %r — skipping",
                        node_id, type(node_data).__name__,
                    )
                    continue
                enriched = database.investigate(
                    candidate=candidate,
                    node=node_data,
                    graph=graph,
                    spans=spans,
                    metrics=metrics,
                    baselines=baselines,
                    incident_start=incident_start,
                    llm_client=llm_client,
                    iteration=iteration,
                )
            elif node_type == NodeType.EXTERNAL_DEP:
                if not isinstance(node_data, ExternalDepNode):
                    logger.warning(
                        "investigate_all: node %r has node_type EXTERNAL_DEP but "
                        "data is %r — skipping",
                        node_id, type(node_data).__name__,
                    )
                    continue
                enriched = dependency.investigate(
                    candidate=candidate,
                    node=node_data,
                    graph=graph,
                    spans=spans,
                    metrics=metrics,
                    baselines=baselines,
                    incident_start=incident_start,
                    llm_client=llm_client,
                    iteration=iteration,
                )
            elif node_type == NodeType.QUEUE:
                if not isinstance(node_data, QueueNode):
                    logger.warning(
                        "investigate_all: node %r has node_type QUEUE but "
                        "data is %r — skipping",
                        node_id, type(node_data).__name__,
                    )
                    continue
                enriched = queue.investigate(
                    candidate=candidate,
                    node=node_data,
                    graph=graph,
                    spans=spans,
                    metrics=metrics,
                    baselines=baselines,
                    incident_start=incident_start,
                    llm_client=llm_client,
                    iteration=iteration,
                )
            else:
                logger.warning(
                    "investigate_all: unknown node_type %r for candidate %r — skipping",
                    node_type, node_id,
                )
                continue
        except Exception as exc:
            logger.warning(
                "investigate_all: domain engine failed for %r: %s",
                node_id, exc, exc_info=True,
            )
            enriched = EnrichedCandidate(
                node_id=candidate.candidate.node_id,
                node_type=candidate.candidate.node_type,
                domain=RCADomain.UNKNOWN,
                failure_type=FailureType.UNKNOWN,
                evidence=[],
                is_potential_origin=candidate.candidate.is_potential_origin,
                priority_score=candidate.candidate.priority_score,
                causal_confidence=candidate.causal_confidence,
                iteration_found=iteration,
            )

        # Step 3: Neighbour scan (failure does not block candidate enrichment)
        try:
            hints = neighbour_scan.scan(
                candidate_node_id=node_id,
                graph=graph,
                spans=spans,
                metrics=metrics,
                baselines=baselines,
                incident_start=incident_start,
                already_investigated=already_investigated,
                iteration=iteration,
            )
            all_hints.extend(hints)
        except Exception as exc:
            logger.warning(
                "investigate_all: neighbour scan failed for %r: %s",
                node_id, exc, exc_info=True,
            )

        # Step 4: Collect enriched candidate preserving ranked_candidates order
        enriched_candidates.append(enriched)

    return DomainRCAResult(candidates=enriched_candidates, hints=all_hints)
