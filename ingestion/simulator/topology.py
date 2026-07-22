"""
Reference service topology definition.

Defines the synthetic service graph used by the simulator, modeled after a
fintech/e-commerce ICP topology. Produces a static description of nodes and
edges that trace_generator.py and failure_injector.py use to emit realistic
OTel-format data.
"""

from __future__ import annotations

import copy

from core.edges import Edge, EdgeType
from core.nodes import DatabaseNode, ExternalDepNode, Node, QueueNode, ServiceNode

TOPOLOGY_VERSION = "1.0.0"

_NODES: dict[str, Node] = {
    "api-gateway": ServiceNode(id="api-gateway", name="API Gateway", code_access_enabled=True, tags={"tier": "edge"}),
    "frontend-service": ServiceNode(id="frontend-service", name="Frontend Service", code_access_enabled=True, tags={"tier": "frontend"}),
    "checkout-service": ServiceNode(id="checkout-service", name="Checkout Service", code_access_enabled=True, tags={"tier": "backend"}),
    "payment-service": ServiceNode(id="payment-service", name="Payment Service", code_access_enabled=True, tags={"tier": "backend", "criticality": "high"}),
    "inventory-service": ServiceNode(id="inventory-service", name="Inventory Service", code_access_enabled=True, tags={"tier": "backend"}),
    "fraud-service": ServiceNode(id="fraud-service", name="Fraud Service", code_access_enabled=True, tags={"tier": "backend", "criticality": "high"}),
    "auth-service": ServiceNode(id="auth-service", name="Auth Service", code_access_enabled=True, tags={"tier": "backend", "criticality": "high"}),
    "notification-service": ServiceNode(id="notification-service", name="Notification Service", code_access_enabled=True, tags={"tier": "backend"}),
    "catalog-service": ServiceNode(id="catalog-service", name="Catalog Service", code_access_enabled=True, tags={"tier": "backend"}),
    "postgres-payments": DatabaseNode(id="postgres-payments", name="Payments DB", db_type="postgres"),
    "postgres-inventory": DatabaseNode(id="postgres-inventory", name="Inventory DB", db_type="postgres"),
    "postgres-fraud": DatabaseNode(id="postgres-fraud", name="Fraud DB", db_type="postgres"),
    "postgres-catalog": DatabaseNode(id="postgres-catalog", name="Catalog DB", db_type="postgres"),
    "redis-cache": DatabaseNode(id="redis-cache", name="Frontend Cache", db_type="redis"),
    "redis-sessions": DatabaseNode(id="redis-sessions", name="Session Store", db_type="redis"),
    "order-queue": QueueNode(
        id="order-queue",
        name="Order Queue",
        queue_type="kafka",
        topics=["order-created", "order-confirmed", "order-failed"],
    ),
    "stripe-api": ExternalDepNode(id="stripe-api", name="Stripe API", endpoint="https://api.stripe.com"),
    "risk-api": ExternalDepNode(id="risk-api", name="Risk API", endpoint="https://api.risk-provider.com"),
    "sms-provider": ExternalDepNode(id="sms-provider", name="SMS Provider", endpoint="https://api.sms-provider.com"),
    "email-provider": ExternalDepNode(id="email-provider", name="Email Provider", endpoint="https://api.email-provider.com"),
}

_EDGES: list[Edge] = [
    Edge(source_id="api-gateway", target_id="frontend-service", edge_type=EdgeType.CALLS),
    Edge(source_id="api-gateway", target_id="auth-service", edge_type=EdgeType.CALLS),
    Edge(source_id="frontend-service", target_id="checkout-service", edge_type=EdgeType.CALLS),
    Edge(source_id="frontend-service", target_id="catalog-service", edge_type=EdgeType.CALLS),
    Edge(source_id="frontend-service", target_id="redis-cache", edge_type=EdgeType.READS_FROM),
    Edge(source_id="auth-service", target_id="redis-sessions", edge_type=EdgeType.READS_FROM),
    Edge(source_id="auth-service", target_id="sms-provider", edge_type=EdgeType.CALLS),
    Edge(source_id="checkout-service", target_id="payment-service", edge_type=EdgeType.CALLS),
    Edge(source_id="checkout-service", target_id="inventory-service", edge_type=EdgeType.CALLS),
    Edge(source_id="checkout-service", target_id="order-queue", edge_type=EdgeType.PUBLISHES_TO),
    Edge(source_id="payment-service", target_id="fraud-service", edge_type=EdgeType.CALLS),
    Edge(source_id="payment-service", target_id="postgres-payments", edge_type=EdgeType.WRITES_TO),
    Edge(source_id="fraud-service", target_id="stripe-api", edge_type=EdgeType.CALLS),
    Edge(source_id="fraud-service", target_id="risk-api", edge_type=EdgeType.CALLS),
    Edge(source_id="fraud-service", target_id="postgres-fraud", edge_type=EdgeType.READS_FROM),
    Edge(source_id="inventory-service", target_id="postgres-inventory", edge_type=EdgeType.READS_FROM),
    Edge(source_id="catalog-service", target_id="postgres-catalog", edge_type=EdgeType.READS_FROM),
    Edge(source_id="order-queue", target_id="notification-service", edge_type=EdgeType.SUBSCRIBES_TO),
    Edge(source_id="notification-service", target_id="email-provider", edge_type=EdgeType.CALLS),
]


def get_nodes() -> dict[str, Node]:
    # Deep copy — callers (seed_from_topology, attach_change_events) mutate
    # returned nodes in place (e.g. appending to recent_deploys). Returning
    # the shared module-level instances would leak state across requests
    # for the life of the process.
    return {node_id: copy.deepcopy(node) for node_id, node in _NODES.items()}


def get_edges() -> list[Edge]:
    return [copy.deepcopy(edge) for edge in _EDGES]
