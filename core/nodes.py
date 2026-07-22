"""
Shared node data models for the Hyperion dependency graph.

Defines NodeType enum and Node dataclasses (Service, Database, Queue,
ExternalDep). These are the vertices of the causal graph used throughout
the reasoning engine. DeployEvent and ConfigChangeEvent are attached as
temporal attributes on ServiceNode, NOT as standalone graph nodes.

No dependencies on any other internal Hyperion module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.change_event import (
        ConfigChangeEvent,
        DeployEvent,
        FeatureFlagChangeEvent,
    )
    from core.code_change import CodeChangeEvent


class NodeType(Enum):
    SERVICE = "service"
    DATABASE = "database"
    QUEUE = "queue"
    EXTERNAL_DEP = "external_dep"

    def __str__(self) -> str:
        return self.value


@dataclass
class ServiceNode:
    id: str
    name: str
    node_type: NodeType = NodeType.SERVICE
    recent_deploys: list[DeployEvent] = field(default_factory=list)
    recent_config_changes: list[ConfigChangeEvent] = field(default_factory=list)
    recent_feature_flag_changes: list[FeatureFlagChangeEvent] = field(
        default_factory=list
    )
    code_access_enabled: bool = False
    repo_url: str | None = None
    recent_code_changes: list[CodeChangeEvent] = field(default_factory=list)
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass
class DatabaseNode:
    id: str
    name: str
    db_type: str
    version: str | None = None
    node_type: NodeType = NodeType.DATABASE
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass
class QueueNode:
    id: str
    name: str
    queue_type: str
    topics: list[str] = field(default_factory=list)
    node_type: NodeType = NodeType.QUEUE
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExternalDepNode:
    id: str
    name: str
    endpoint: str | None = None
    node_type: NodeType = NodeType.EXTERNAL_DEP
    tags: dict[str, Any] = field(default_factory=dict)


Node = ServiceNode | DatabaseNode | QueueNode | ExternalDepNode
