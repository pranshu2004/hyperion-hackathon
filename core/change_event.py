"""
DeployEvent, ConfigChangeEvent, and FeatureFlagChangeEvent dataclasses.

Represents temporal change signals attached to nodes in the dependency graph.
These are node attributes, NOT standalone graph nodes. DeployEvent captures
a service deployment (version, timestamp, author, deploy_scope). ConfigChangeEvent captures
a configuration change and FeatureFlagChangeEvent captures a feature-flag change.

No dependencies on any other internal Hyperion module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class DeployEvent:
    event_id: str
    service_id: str
    timestamp: datetime
    version: str
    deploy_scope: list[str]
    author: str = ""
    diff_summary: str = ""
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConfigChangeEvent:
    event_id: str
    service_id: str
    timestamp: datetime
    change_type: str
    key: str
    new_value: Any
    old_value: Any = None
    author: str = ""
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass
class FeatureFlagChangeEvent:
    event_id: str
    service_id: str
    timestamp: datetime
    flag_key: str
    old_value: Any
    new_value: Any
    author: str = ""
    tags: dict[str, Any] = field(default_factory=dict)
