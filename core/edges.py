"""
Shared edge data models for the Hyperion dependency graph.

Defines EdgeType enum and Edge dataclass representing typed relationships
between nodes (calls, publishes_to, subscribes_to, reads_from, writes_to,
depends_on). Edges are the directed connections in the causal graph.

No dependencies on any other internal Hyperion module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EdgeType(str, Enum):
    CALLS = "calls"
    PUBLISHES_TO = "publishes_to"
    SUBSCRIBES_TO = "subscribes_to"
    READS_FROM = "reads_from"
    WRITES_TO = "writes_to"
    DEPENDS_ON = "depends_on"


@dataclass
class Edge:
    source_id: str
    target_id: str
    edge_type: EdgeType
    metadata: dict[str, Any] = field(default_factory=dict)
