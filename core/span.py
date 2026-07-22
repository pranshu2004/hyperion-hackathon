"""
Normalized OpenTelemetry Span dataclass.

Represents a single OTel span after ingestion normalization. This is the
canonical span model used throughout Hyperion — all trace ingesters must
produce this type. Carries trace/span/parent IDs, service name, operation,
timing, status, and key attributes.

No dependencies on any other internal Hyperion module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SpanKind(str, Enum):
    SERVER = "server"
    CLIENT = "client"
    PRODUCER = "producer"
    CONSUMER = "consumer"
    INTERNAL = "internal"


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    service_id: str
    operation_name: str
    start_time: datetime
    end_time: datetime
    duration_ms: float
    kind: SpanKind
    otel_status_code: int
    http_status_code: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    resource_attributes: dict[str, str] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.otel_status_code == 2

    @property
    def is_server_error(self) -> bool:
        return self.http_status_code is not None and self.http_status_code >= 500

    @property
    def is_client_error(self) -> bool:
        return self.http_status_code is not None and 400 <= self.http_status_code < 500

    @property
    def service_version(self) -> str | None:
        return self.resource_attributes.get("service.version")

    @property
    def environment(self) -> str | None:
        return self.resource_attributes.get("deployment.environment")

    @property
    def is_root(self) -> bool:
        return self.parent_span_id is None
