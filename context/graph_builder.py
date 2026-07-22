"""
Dependency graph builder.

Builds and maintains the typed service dependency graph from normalized Span
objects. Stateful within an incident window — spans are accumulated and the
graph is updated incrementally. Uses NetworkX for graph representation.
Node and edge types are defined in core/.
"""

from __future__ import annotations

import logging

import networkx as nx
from ingestion.simulator.topology import get_nodes, get_edges

from core.nodes import (
    Node, NodeType,
    ServiceNode, DatabaseNode, QueueNode, ExternalDepNode,
)
from core.edges import Edge, EdgeType
from core.span import Span, SpanKind
from core.change_event import (
    DeployEvent, ConfigChangeEvent, FeatureFlagChangeEvent,
)
from core.code_change import CodeChangeEvent

logger = logging.getLogger(__name__)


# Attribute keys (across OTel semantic-convention versions and vendor
# instrumentations) whose presence on a CLIENT span indicates its target's
# type. Detection only — never a source of node identity (e.g. db.system's
# value is a DB vendor name like "postgresql", shared by every database of
# that vendor, not a specific instance). Not exhaustive — extend per-type
# as real pilot traces are checked against this list.
_DEPENDENCY_ATTRIBUTE_ALIASES: dict[NodeType, tuple[str, ...]] = {
    NodeType.DATABASE: ("db.system", "db.name", "db.namespace"),
    NodeType.QUEUE: ("messaging.system", "messaging.destination", "messaging.destination.name"),
    NodeType.EXTERNAL_DEP: ("peer.service",),
}


def _dependency_target_id(node_type: NodeType, attrs: dict, service_id: str) -> str:
    """
    Extract a dependency's identity for a detected target type. Only fields
    that name a specific instance are used — never a type/vendor field like
    db.system or messaging.system. Falls back to a synthetic id derived from
    the emitter when no identity field is set.
    """
    if node_type == NodeType.DATABASE:
        return attrs.get("db.name") or attrs.get("db.namespace") or f"{service_id}-db"
    if node_type == NodeType.QUEUE:
        return (
            attrs.get("messaging.destination")
            or attrs.get("messaging.destination.name")
            or "unknown-queue"
        )
    return attrs["peer.service"]  # EXTERNAL_DEP: peer.service is itself the identity


def _infer_target_from_span(span: Span) -> tuple[str, NodeType] | None:
    """
    Infer the (id, type) of the dependency a CLIENT/PRODUCER span is calling,
    from the span's own attributes. Returns None if the span isn't a call to
    a database, queue, or external dependency.

    On a CLIENT/PRODUCER span these attributes describe the *target* being
    called, never the emitter (span.service_id) — see discover_nodes() for
    the one exception, where a source reports a dependency's own spans under
    the dependency's own name.
    """
    attrs = span.attributes
    if span.kind == SpanKind.CLIENT:
        for node_type in (NodeType.DATABASE, NodeType.QUEUE, NodeType.EXTERNAL_DEP):
            if any(alias in attrs for alias in _DEPENDENCY_ATTRIBUTE_ALIASES[node_type]):
                return _dependency_target_id(node_type, attrs, span.service_id), node_type
        return None
    if span.kind == SpanKind.PRODUCER:
        return _dependency_target_id(NodeType.QUEUE, attrs, span.service_id), NodeType.QUEUE
    return None


def _make_node(
    node_id: str,
    node_type: NodeType,
    span: Span | None = None,
) -> Node:
    """
    Create a typed Node object for a given node_id and node_type.
    """
    if node_type == NodeType.SERVICE:
        tags: dict = {}
        if span and span.environment:
            tags = {"env": span.environment}
        return ServiceNode(id=node_id, name=node_id, tags=tags)

    if node_type == NodeType.DATABASE:
        db_type = "unknown"
        version = None
        if span:
            db_type = span.attributes.get("db.system", "unknown")
            version = span.attributes.get("db.version")
        return DatabaseNode(id=node_id, name=node_id, db_type=db_type, version=version)

    if node_type == NodeType.QUEUE:
        queue_type = "unknown"
        topics: list[str] = []
        if span:
            queue_type = span.attributes.get("messaging.system", "unknown")
            dest = span.attributes.get("messaging.destination")
            if dest:
                topics = [dest]
        return QueueNode(id=node_id, name=node_id, queue_type=queue_type, topics=topics)

    # EXTERNAL_DEP. server.address (current semconv) / net.peer.name (legacy)
    # are hostname-only — deliberately not url.full/http.url (per-request,
    # can carry query-string secrets) or peer.service (that's the node's own
    # id, not an address — see _dependency_target_id).
    endpoint = None
    if span:
        endpoint = span.attributes.get("server.address") or span.attributes.get("net.peer.name")
    return ExternalDepNode(id=node_id, name=node_id, endpoint=endpoint)


def _get_or_create_node(
    graph: nx.DiGraph,
    node_id: str,
    node_type: NodeType,
    span: Span | None = None,
) -> Node:
    """
    Get existing node from graph or create and add a new one.
    First discovery wins — topology seeding takes priority.
    """
    if node_id in graph.nodes:
        return graph.nodes[node_id]["data"]
    node = _make_node(node_id, node_type, span)
    graph.add_node(node_id, data=node)
    return node


def discover_nodes(
    spans: list[Span],
    graph: nx.DiGraph | None = None,
) -> nx.DiGraph:
    """
    Discover all nodes from a list of Span objects and add them
    to a NetworkX directed graph.

    Args:
        spans: List of normalized Span objects from trace_ingester.
        graph: Existing graph to add nodes to. If None, creates
               a new empty DiGraph.

    Returns:
        DiGraph with all discovered nodes added.
        Each node stored as: graph.nodes[node_id]['data'] = Node object
    """
    if graph is None:
        graph = nx.DiGraph()

    for span in spans:
        try:
            target = _infer_target_from_span(span)

            # A span's own service_id is the emitter, and is a ServiceNode —
            # unless the emitter and the target of its own call are the same
            # id (a source reporting a dependency's own spans under the
            # dependency's own name, e.g. the simulator's DB/queue/external-
            # dep nodes). A real CLIENT/PRODUCER span never hits this: the
            # caller's service_id is never equal to what it calls.
            self_type = target[1] if target and target[0] == span.service_id else NodeType.SERVICE
            _get_or_create_node(graph, span.service_id, self_type, span)

            if target and target[0] != span.service_id:
                target_id, target_type = target
                _get_or_create_node(graph, target_id, target_type, span)

        except Exception as exc:
            logger.warning("Skipping anomalous span %s during node discovery: %s", span.span_id, exc)

    return graph


def _infer_edge_type(span: Span) -> EdgeType:
    """
    Infer edge type from span kind and attributes.
    """
    if span.kind == SpanKind.PRODUCER:
        return EdgeType.PUBLISHES_TO
    if span.kind == SpanKind.CONSUMER:
        return EdgeType.SUBSCRIBES_TO
    if span.kind == SpanKind.CLIENT:
        if "db.system" in span.attributes:
            db_op = span.attributes.get("db.operation", "").upper()
            if db_op in ("INSERT", "UPDATE", "DELETE", "WRITE"):
                return EdgeType.WRITES_TO
            return EdgeType.READS_FROM
        if "peer.service" in span.attributes:
            return EdgeType.CALLS
        return EdgeType.CALLS
    return EdgeType.DEPENDS_ON


def _add_edge(
    graph: nx.DiGraph,
    source_id: str,
    target_id: str,
    edge_type: EdgeType,
    span: Span,
) -> None:
    """Add edge if both nodes exist and edge is not already present."""
    if source_id not in graph.nodes or target_id not in graph.nodes:
        logger.debug(
            "Skipping edge %s→%s: node(s) not in graph", source_id, target_id
        )
        return
    if graph.has_edge(source_id, target_id):
        return
    graph.add_edge(
        source_id,
        target_id,
        data=Edge(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            metadata={
                "protocol": span.attributes.get(
                    "http.scheme",
                    span.attributes.get("rpc.system", "unknown"),
                )
            },
        ),
    )


def discover_edges(
    spans: list[Span],
    graph: nx.DiGraph,
) -> nx.DiGraph:
    """
    Discover edges from span relationships and add to graph.

    Handles CLIENT, PRODUCER, and CONSUMER spans explicitly.
    Also derives cross-service CALLS edges from parent-child span
    relationships for cases where explicit CLIENT spans are not emitted.
    """
    span_lookup: dict[str, Span] = {s.span_id: s for s in spans}

    for span in spans:
        try:
            if span.kind == SpanKind.CLIENT:
                source_id = span.service_id
                attrs = span.attributes
                target_id: str | None = None
                if "db.name" in attrs:
                    target_id = attrs["db.name"]
                elif "messaging.destination" in attrs:
                    target_id = attrs["messaging.destination"]
                elif "peer.service" in attrs:
                    target_id = attrs["peer.service"]
                # Otherwise: skip — cannot determine target
                if target_id and target_id != source_id:
                    _add_edge(graph, source_id, target_id, _infer_edge_type(span), span)

            elif span.kind == SpanKind.PRODUCER:
                source_id = span.service_id
                target_id = span.attributes.get("messaging.destination", "unknown-queue")
                if target_id != source_id:
                    _add_edge(graph, source_id, target_id, EdgeType.PUBLISHES_TO, span)

            elif span.kind == SpanKind.CONSUMER:
                source_id = span.attributes.get("messaging.destination", "unknown-queue")
                target_id = span.service_id
                if target_id != source_id:
                    _add_edge(graph, source_id, target_id, EdgeType.SUBSCRIBES_TO, span)

            # Derive cross-service CALLS edges from parent-child span relationships.
            # Covers inter-service calls that may not produce explicit CLIENT spans.
            if span.parent_span_id and span.parent_span_id in span_lookup:
                parent = span_lookup[span.parent_span_id]
                if parent.service_id != span.service_id:
                    _add_edge(graph, parent.service_id, span.service_id, EdgeType.CALLS, span)

        except Exception as exc:
            logger.warning(
                "Edge discovery error for span %s: %s", span.span_id, exc
            )

    return graph


def attach_change_events(
    graph: nx.DiGraph,
    deploy_events: list[DeployEvent] | None = None,
    config_change_events: list[ConfigChangeEvent] | None = None,
    feature_flag_events: list[FeatureFlagChangeEvent] | None = None,
    code_change_event: CodeChangeEvent | None = None,
) -> nx.DiGraph:
    """
    Attach change events to the ServiceNode objects they affected.
    """
    def _attach(service_id: str, apply) -> None:
        if service_id not in graph.nodes:
            logger.warning(
                "attach_change_events: node %r not found in graph — skipping", service_id
            )
            return
        node = graph.nodes[service_id]["data"]
        if not isinstance(node, ServiceNode):
            logger.warning(
                "attach_change_events: node %r is %s, not ServiceNode — skipping",
                service_id, type(node).__name__,
            )
            return
        apply(node)

    for event in (deploy_events or []):
        _attach(event.service_id, lambda n, e=event: n.recent_deploys.append(e))

    for event in (config_change_events or []):
        _attach(event.service_id, lambda n, e=event: n.recent_config_changes.append(e))

    for event in (feature_flag_events or []):
        _attach(event.service_id, lambda n, e=event: n.recent_feature_flag_changes.append(e))

    if code_change_event is not None:
        _attach(
            code_change_event.service_id,
            lambda n, e=code_change_event: n.recent_code_changes.append(e),
        )

    return graph


def seed_from_topology(
    graph: nx.DiGraph | None = None,
) -> nx.DiGraph:
    """
    Seed a graph with all known nodes and edges from the reference
    topology (ingestion/simulator/topology.py).

    This must be called BEFORE discover_nodes() so that node types
    are correct. Since _get_or_create_node uses first-discovery-wins,
    seeded nodes take priority over span-discovered nodes.

    Args:
        graph: Existing graph to seed. If None, creates new DiGraph.

    Returns:
        Graph with all 20 topology nodes and 19 edges added.
        Each node stored as: graph.nodes[node_id]['data'] = Node
        Each edge stored as: graph.edges[s,t]['data'] = Edge

    Never raises.
    """
    if graph is None:
        graph = nx.DiGraph()

    try:
        for node_id, node in get_nodes().items():
            if node_id not in graph.nodes:
                graph.add_node(node_id, data=node)

        for edge in get_edges():
            if not graph.has_edge(edge.source_id, edge.target_id):
                graph.add_edge(edge.source_id, edge.target_id, data=edge)
    except Exception as exc:
        logger.warning("seed_from_topology error: %s", exc)

    return graph


def build(
    spans: list[Span],
    deploy_events: list[DeployEvent] | None = None,
    config_change_events: list[ConfigChangeEvent] | None = None,
    feature_flag_events: list[FeatureFlagChangeEvent] | None = None,
    code_change_event: CodeChangeEvent | None = None,
    seed_topology: bool = True,
) -> nx.DiGraph:
    """
    Build the complete typed dependency graph from ingested data.

    This is the primary entry point for the reasoning engine.

    Pipeline:
        1. seed_from_topology() if seed_topology=True
        2. discover_nodes(spans, graph)
        3. discover_edges(spans, graph)
        4. attach_change_events(graph, ...)

    Never raises. Logs warnings for any issues encountered.
    """
    try:
        graph = seed_from_topology() if seed_topology else nx.DiGraph()
        discover_nodes(spans, graph)
        discover_edges(spans, graph)
        attach_change_events(
            graph,
            deploy_events=deploy_events,
            config_change_events=config_change_events,
            feature_flag_events=feature_flag_events,
            code_change_event=code_change_event,
        )
    except Exception as exc:
        logger.warning("build() encountered an error: %s", exc)
        raise

    return graph
