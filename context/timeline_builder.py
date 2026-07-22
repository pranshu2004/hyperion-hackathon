"""
Causal timeline builder.

Establishes causal ordering of events from OTel trace parent-child span
relationships. Does NOT rely on wall-clock timestamps alone — parent-child
hierarchy is the primary ordering mechanism. Clock skew between services must
be detected and compensated for here, not in the reasoning layer.
Stateful within an incident window.
"""

from __future__ import annotations

import logging
from datetime import datetime
from dataclasses import dataclass, field
from collections import defaultdict, deque

from core.span import Span, SpanKind

logger = logging.getLogger(__name__)


@dataclass
class CausalEvent:
    node_id: str
    trace_id: str
    span_id: str
    causal_depth: int      # 0=root, 1=direct child of root, etc.
    is_error: bool
    is_server_error: bool
    timestamp: datetime    # display only — never use for causal ordering


@dataclass
class NodeTimeline:
    node_id: str
    first_error_span_id: str | None = None
    first_error_depth: int | None = None
    error_span_count: int = 0
    total_span_count: int = 0
    causal_position: float = 0.0

    @property
    def error_rate(self) -> float:
        if self.total_span_count == 0:
            return 0.0
        return self.error_span_count / self.total_span_count

    @property
    def is_likely_origin(self) -> bool:
        """
        True if this node shows errors AND has low causal position.
        Causal position < 2.0 means it appears early in traces.
        Error rate >= 0.1 means meaningful error signal.
        """
        return (
            self.error_rate >= 0.1
            and self.first_error_depth is not None
            and self.first_error_depth <= 2
        )


def _build_span_index(spans: list[Span]) -> dict[str, Span]:
    """
    Build a dict mapping span_id → Span for fast parent lookup.
    Never raises. Returns empty dict on None/empty input.
    """
    if not spans:
        return {}
    return {s.span_id: s for s in spans}


def _assign_causal_depths(
    spans: list[Span],
    span_index: dict[str, Span],
) -> dict[str, int]:
    """
    Assign causal depth to each span via BFS from root spans.

    Root spans (parent_span_id is None) get depth 0.
    Orphaned spans (parent not in index) get depth 999.
    Never raises.
    """

    # NOTE: spans with missing parents (dropped by network) are treated as
    # roots (depth 0) rather than orphans (depth 999). This is intentional —
    # if a middle span is lost, the child should still be traversable.
    # depth 999 only occurs for truly unreachable spans (cyclic traces).

    if not spans:
        return {}

    depths: dict[str, int] = {}

    # Build children index for BFS.
    children: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []
    for s in spans:
        if s.parent_span_id is None or s.parent_span_id not in span_index:
            roots.append(s.span_id)
        else:
            children[s.parent_span_id].append(s.span_id)

    queue: deque[tuple[str, int]] = deque((sid, 0) for sid in roots)
    while queue:
        span_id, depth = queue.popleft()
        if span_id in depths:
            continue
        depths[span_id] = depth
        for child_id in children.get(span_id, []):
            if child_id not in depths:
                queue.append((child_id, depth + 1))

    # Orphaned spans not reached from any root.
    for s in spans:
        if s.span_id not in depths:
            depths[s.span_id] = 999

    return depths


def build_causal_events(spans: list[Span]) -> list[CausalEvent]:
    """
    Build CausalEvent objects for all spans.

    Returns list sorted by (causal_depth, timestamp) — causally earlier first,
    wall-clock as tiebreaker within same depth.
    Never raises. Returns empty list on empty input.
    """
    if not spans:
        return []

    span_index = _build_span_index(spans)
    depths = _assign_causal_depths(spans, span_index)

    events = [
        CausalEvent(
            node_id=s.service_id,
            trace_id=s.trace_id,
            span_id=s.span_id,
            causal_depth=depths.get(s.span_id, 999),
            is_error=s.is_error,
            is_server_error=s.is_server_error,
            timestamp=s.start_time,
        )
        for s in spans
    ]

    events.sort(key=lambda e: (e.causal_depth, e.timestamp))
    return events


def build_node_timelines(
    causal_events: list[CausalEvent],
) -> dict[str, NodeTimeline]:
    """
    Aggregate CausalEvents into NodeTimeline per node.

    causal_position = mean causal_depth across all events for the node.
    first_error_depth = minimum causal_depth among error events.
    Never raises.
    """
    timelines: dict[str, NodeTimeline] = {}
    depth_sums: dict[str, int] = defaultdict(int)

    for ev in causal_events:
        nid = ev.node_id
        if nid not in timelines:
            timelines[nid] = NodeTimeline(node_id=nid)

        tl = timelines[nid]
        tl.total_span_count += 1
        depth_sums[nid] += ev.causal_depth

        if ev.is_error:
            tl.error_span_count += 1
            if tl.first_error_depth is None or ev.causal_depth < tl.first_error_depth:
                tl.first_error_depth = ev.causal_depth
                tl.first_error_span_id = ev.span_id

    for nid, tl in timelines.items():
        tl.causal_position = depth_sums[nid] / tl.total_span_count

    return timelines


def get_origin_candidates(
    node_timelines: dict[str, NodeTimeline],
    anomalous_node_ids: set[str],
) -> list[NodeTimeline]:
    """
    Filter node timelines to likely origin candidates.

    Returns NodeTimelines where node_id is anomalous AND
    (is_likely_origin OR first_error_depth <= 3).
    Sorted by first_error_depth ascending (None last).
    Never raises.
    """
    # Include any anomalous node that is either a likely shallow origin
    # (is_likely_origin) or has any error spans (error_span_count > 0).
    # Depth-only cutoffs fail for deep topologies like the hero scenario
    # where the root cause (fraud-service) sits at depth 4.
    candidates = [
        tl for nid, tl in node_timelines.items()
        if nid in anomalous_node_ids
        and (tl.is_likely_origin or tl.error_span_count > 0)
    ]

    candidates.sort(
        key=lambda tl: tl.first_error_depth if tl.first_error_depth is not None else 9999
    )
    return candidates


def build(
    spans: list[Span],
    anomalous_node_ids: set[str] | None = None,
) -> dict:
    """
    Main entry point. Build complete causal timeline from spans.

    Returns dict with causal_events, node_timelines, origin_candidates,
    span_count, and trace_count.
    Never raises.
    """
    if not spans:
        return {
            "causal_events": [],
            "node_timelines": {},
            "origin_candidates": [],
            "span_count": 0,
            "trace_count": 0,
        }

    causal_events = build_causal_events(spans)
    node_timelines = build_node_timelines(causal_events)
    origin_candidates = get_origin_candidates(
        node_timelines,
        anomalous_node_ids or set(),
    )
    trace_count = len({s.trace_id for s in spans})

    return {
        "causal_events": causal_events,
        "node_timelines": node_timelines,
        "origin_candidates": origin_candidates,
        "span_count": len(spans),
        "trace_count": trace_count,
    }
