"""
Stage 3 Domain RCA for QueueNode candidates.
Stub — queue RCA is not implemented for MVP.
Returns EnrichedCandidate with domain=QUEUE, failure_type=QUEUE_ERROR,
no evidence, priority unchanged.

Full implementation in V2: consumer lag detection, dead letter queue
analysis, producer/consumer rate mismatch, partition leader election.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import networkx as nx

from context.baseline_calculator import BaselineStats
from core.metric import Metric, MetricType
from core.nodes import QueueNode
from core.span import Span
from reasoning.contracts import (
    EnrichedCandidate,
    FailureType,
    RCADomain,
    RankedCandidate,
)


def investigate(
    candidate: RankedCandidate,
    node: QueueNode,
    graph: nx.DiGraph,
    spans: list[Span],
    metrics: list[Metric],
    baselines: dict[tuple[str, MetricType], BaselineStats],
    incident_start: datetime,
    llm_client: Any | None,
    iteration: int,
) -> EnrichedCandidate:
    """
    Investigate a QueueNode candidate.

    Stub — returns empty evidence with domain=QUEUE and failure_type=QUEUE_ERROR.
    Priority score is carried through unchanged.
    """
    # TODO (V2): implement queue RCA:
    # - Consumer lag detection from CONSUMER_LAG metrics
    # - Dead letter queue analysis
    # - Producer/consumer rate mismatch
    # - Partition leader election issues

    return EnrichedCandidate(
        node_id=candidate.candidate.node_id,
        node_type=candidate.candidate.node_type,
        domain=RCADomain.QUEUE,
        failure_type=FailureType.QUEUE_ERROR,
        evidence=[],
        is_potential_origin=candidate.candidate.is_potential_origin,
        priority_score=candidate.candidate.priority_score,
        causal_confidence=candidate.causal_confidence,
        iteration_found=iteration,
    )
