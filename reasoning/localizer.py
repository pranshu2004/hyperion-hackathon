"""
reasoning/localizer.py — Stage 1 of the reasoning pipeline.

Its only job is inclusion. It casts a wide net and returns every node worth
investigating. No scoring, no ranking, no judgement. Everything downstream
(causal model, domain RCA, scorer) handles pruning and ranking.

The localizer runs on every iteration of the reasoning loop. On iteration 1
only metric anomalies and change events are used. On iteration 2+ it also
accepts InvestigationHints from the previous Domain RCA and Neighbour Scan.

Named constant:
    CHANGE_EVENT_LOOKBACK_MINUTES = 60
    A change event is considered within the incident window if its timestamp
    falls within this many minutes before incident_start.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import networkx as nx

from context.baseline_calculator import DeviationResult
from core.metric import MetricType
from core.nodes import NodeType, ServiceNode
from core.span import Span
from reasoning.contracts import (
    CandidateNode,
    InclusionReason,
    InvestigationHint,
)

CHANGE_EVENT_LOOKBACK_MINUTES = 60

# Inclusion reason priority — lower value = higher priority
_REASON_PRIORITY = {
    InclusionReason.METRIC_ANOMALY: 0,
    InclusionReason.CHANGE_EVENT: 1,
    InclusionReason.INVESTIGATION_HINT: 2,
}

logger = logging.getLogger(__name__)


def localize(
    graph: nx.DiGraph,
    anomalous_nodes: list[DeviationResult],
    spans: list[Span],
    # spans: reserved for future span-based inclusion. Currently unused
    # by the localizer — span-level signals are handled by the Neighbour
    # Scan stage. Do not remove this parameter as it is part of the
    # public API contract.
    incident_start: datetime,
    hints: list[InvestigationHint] | None = None,
    known_candidate_ids: set[str] | None = None,
) -> list[CandidateNode]:
    """
    Identify every node worth investigating for this iteration.

    Returns a flat unranked list of CandidateNode with no duplicates.
    Priority order when a node qualifies under multiple criteria:
      METRIC_ANOMALY > CHANGE_EVENT > INVESTIGATION_HINT
    """
    hints = hints or []
    # In the V2 loop this is intended to mean nodes that have already gone
    # through full Domain RCA in prior iterations. The broader historical
    # name is kept for now to avoid churn while the engine is being built.
    known_candidate_ids = known_candidate_ids or set()

    # node_id -> InclusionReason
    included: dict[str, InclusionReason] = {}

    def _include(node_id: str, reason: InclusionReason) -> None:
        existing = included.get(node_id)
        if existing is None or _REASON_PRIORITY[reason] < _REASON_PRIORITY[existing]:
            included[node_id] = reason

    # ------------------------------------------------------------------
    # 1. METRIC_ANOMALY — error rate or latency anomaly from baselines
    # ------------------------------------------------------------------
    for deviation in anomalous_nodes:
        if deviation.node_id in known_candidate_ids:
            continue
        if deviation.metric_type in (MetricType.ERROR_RATE, MetricType.LATENCY_P99):
            _include(deviation.node_id, InclusionReason.METRIC_ANOMALY)

    # ------------------------------------------------------------------
    # 2. CHANGE_EVENT — ServiceNode with a recent change in the window
    # ------------------------------------------------------------------
    window_start = _to_utc(incident_start) - timedelta(minutes=CHANGE_EVENT_LOOKBACK_MINUTES)

    for node_id in graph.nodes:
        if node_id in known_candidate_ids:
            continue
        node_data = graph.nodes[node_id].get("data")
        if not isinstance(node_data, ServiceNode):
            continue
        if _has_change_event_in_window(node_data, window_start, incident_start):
            _include(node_id, InclusionReason.CHANGE_EVENT)

    # ------------------------------------------------------------------
    # 3. INVESTIGATION_HINT — from the previous iteration
    # ------------------------------------------------------------------
    for hint in hints:
        node_id = hint.node_id
        if node_id in known_candidate_ids:
            continue
        _include(node_id, InclusionReason.INVESTIGATION_HINT)

    # ------------------------------------------------------------------
    # Build candidate list without is_potential_origin (set below)
    # ------------------------------------------------------------------
    node_type_map = _build_node_type_map(graph)

    candidates: list[CandidateNode] = []
    for node_id, reason in included.items():
        node_type = node_type_map.get(node_id)
        if node_type is None:
            logger.warning(
                "localizer: skipping node %r — type unknown, "
                "not present in graph or missing data attribute",
                node_id,
            )
            # TODO: hint-surfaced nodes not present in the graph should be
            # investigated in V2 by fetching node metadata from a registry.
            continue
        candidates.append(
            CandidateNode(
                node_id=node_id,
                node_type=node_type,
                inclusion_reason=reason,
                is_potential_origin=False,  # populated below
            )
        )

    # ------------------------------------------------------------------
    # Heuristic: a candidate is a potential origin if none of its
    # downstream dependencies (graph successors) appear in the combined
    # set of ALL nodes investigated across all iterations — current
    # candidates plus nodes already investigated in prior iterations.
    #
    # We use known_candidate_ids | candidate_ids rather than:
    # - candidate_ids alone: too narrow in iteration 2+ where the current
    #   candidate list may only contain newly hinted nodes
    # - anomalous_nodes alone: misses hint-surfaced nodes that have no
    #   metric anomaly
    #
    # Known failure modes:
    # - Missing telemetry: a failing downstream node never surfaced as a
    #   candidate won't appear in this set
    # - Subthreshold degradation: a slow downstream node below z-score
    #   threshold won't appear unless surfaced by a hint
    # - Out-of-graph failures: external causes not in the topology
    # ------------------------------------------------------------------
    candidate_ids: set[str] = {c.node_id for c in candidates}
    all_investigated_ids: set[str] = known_candidate_ids | candidate_ids

    for candidate in candidates:
        anomalous_successors = [
            s for s in graph.successors(candidate.node_id)
            if s in all_investigated_ids
        ]
        candidate.is_potential_origin = len(anomalous_successors) == 0

    return candidates


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _to_utc(dt: datetime) -> datetime:
    """Return dt as UTC-aware. If naive, assumes UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _in_window(event_ts: datetime, window_start: datetime, incident_start: datetime) -> bool:
    """True if event_ts falls within [window_start, incident_start]."""
    ts = _to_utc(event_ts)
    return window_start <= ts <= incident_start


# MVP limitation: change events are only checked on ServiceNode.
# Future versions should support:
#   - Database schema migrations on DatabaseNode
#   - Queue/topic configuration changes on QueueNode
#   - External dependency version changes on ExternalDepNode
def _has_change_event_in_window(
    node: ServiceNode,
    window_start: datetime,
    incident_start: datetime,
) -> bool:
    """True if the node has at least one change event within the lookback window."""
    incident_start_utc = _to_utc(incident_start)
    for deploy in node.recent_deploys:
        if _in_window(deploy.timestamp, window_start, incident_start_utc):
            return True
    for cfg in node.recent_config_changes:
        if _in_window(cfg.timestamp, window_start, incident_start_utc):
            return True
    for ff in node.recent_feature_flag_changes:
        if _in_window(ff.timestamp, window_start, incident_start_utc):
            return True
    for cc in node.recent_code_changes:
        if _in_window(cc.timestamp, window_start, incident_start_utc):
            return True
    return False


def _build_node_type_map(graph: nx.DiGraph) -> dict[str, NodeType | None]:
    """Extract NodeType from each graph node's stored data object.
    Returns None for nodes whose type cannot be determined."""
    result: dict[str, NodeType | None] = {}
    for node_id in graph.nodes:
        node_data = graph.nodes[node_id].get("data")
        if node_data is not None and hasattr(node_data, "node_type"):
            result[node_id] = node_data.node_type
        else:
            result[node_id] = None
    return result
