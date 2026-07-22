"""
Ingestion layer tests: trace_ingester.

Scenarios simulate real OTel collector / instrumentation failure modes:
  - Cascading failure across services (DB connection pool exhaustion
    propagating up through payments-service -> checkout-service)
  - Spans missing spanId / traceId (collector bugs, sampling artifacts)
  - Spans missing end timestamps (in-flight spans flushed on crash)
  - Unknown span.kind integers (newer OTel SDK versions)
  - Missing resource attributes (sidecar misconfiguration ->
    "unknown-service")
  - Non-OTel payloads hitting the trace endpoint (misrouted agent traffic)
  - File-level batch ingestion with a mix of valid/invalid traces
  - Clock-skew artifacts producing negative duration_ms
"""

from __future__ import annotations

import json
import logging

import pytest

from ingestion import trace_ingester
from core.span import SpanKind
from .conftest import FIXTURES, _load


class TestCascadingFailureTrace:
    """
    Realistic incident: checkout-service's POST /checkout times out (504)
    because payments-service's DB query exhausted its connection pool.
    This is the canonical RCA scenario Hyperion is built to localize.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def spans():
        raw = _load("trace_cascading_db_pool_exhaustion.json")
        return trace_ingester.ingest(raw)

    def test_all_spans_parsed(self, spans):
        assert len(spans) == 3

    def test_service_ids_extracted_from_resource_attrs(self, spans):
        service_ids = {s.service_id for s in spans}
        assert service_ids == {"checkout-service", "payments-service"}

    def test_root_span_is_checkout_entrypoint(self, spans):
        root = next(s for s in spans if s.is_root)
        assert root.service_id == "checkout-service"
        assert root.operation_name == "POST /checkout"
        assert root.kind == SpanKind.SERVER

    def test_root_span_marked_as_server_error(self, spans):
        root = next(s for s in spans if s.is_root)
        assert root.http_status_code == 504
        assert root.is_server_error is True
        assert root.is_error is True  # otel_status_code == 2

    def test_db_span_carries_pool_exhaustion_exception_event(self, spans):
        db_span = next(s for s in spans if s.operation_name == "query payments-db")
        assert db_span.is_error is True
        assert len(db_span.events) == 1
        event = db_span.events[0]
        assert event["name"] == "exception"
        # _normalize_events must flatten OTel kv-list attrs to a plain dict
        assert event["attributes"]["exception.type"] == "ConnectionPoolTimeout"
        assert "pool exhausted" in event["attributes"]["exception.message"]

    def test_duration_ms_computed_correctly(self, spans):
        root = next(s for s in spans if s.is_root)
        # 1730558465200000000 - 1730558460000000000 ns = 5_200_000_000 ns = 5200 ms
        assert root.duration_ms == pytest.approx(5200.0)

    def test_parent_child_linkage_preserved(self, spans):
        by_id = {s.span_id: s for s in spans}
        checkout_call = by_id["a1b2c3d4e5f60718"]
        db_query = by_id["f1e2d3c4b5a60718"]
        assert checkout_call.parent_span_id == "00f067aa0ba902b7"
        assert db_query.parent_span_id == "a1b2c3d4e5f60718"

    def test_service_version_and_environment_resolved(self, spans):
        root = next(s for s in spans if s.is_root)
        assert root.service_version == "2.4.1"
        assert root.environment == "production"


class TestNegativeDurationClockSkew:
    """
    Span.duration_ms has no validation anywhere, so a clock-skew artifact
    (endTimeUnixNano < startTimeUnixNano) silently produces a negative
    duration that would flow straight into the reasoning engine.

    The ingester must detect and drop such spans, or at minimum warn.
    This test exercises the fix for that gap.
    """

    def test_span_with_inverted_timestamps_is_dropped(self, caplog):
        """
        A span where endTimeUnixNano < startTimeUnixNano produces a
        negative duration_ms. The ingester must drop it and log a warning
        rather than emitting a Span with duration_ms < 0.
        """
        raw = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "checkout-service"}}
                        ]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "abc123",
                                    "spanId": "deadbeef00000001",
                                    "parentSpanId": "",
                                    "name": "GET /health",
                                    "kind": 2,
                                    "startTimeUnixNano": "1730558465000000000",
                                    # end before start: clock skew / agent bug
                                    "endTimeUnixNano": "1730558460000000000",
                                    "status": {"code": 1},
                                    "attributes": [],
                                    "events": [],
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        with caplog.at_level(logging.WARNING):
            spans = trace_ingester.ingest(raw)
        assert not any(s.span_id == "deadbeef00000001" for s in spans), (
            "A span with endTimeUnixNano < startTimeUnixNano must be dropped; "
            "negative duration_ms must not reach the reasoning engine."
        )
        assert any(
            "negative duration" in r.message or "clock skew" in r.message
            for r in caplog.records
        )


class TestMalformedCollectorBatch:
    """
    Simulates a degraded OTel collector emitting a batch of two traces with
    several structurally-broken spans: empty spanId, missing end timestamp,
    an unknown span.kind integer, an orphaned span with empty traceId, and a
    resourceSpans entry with no resource attributes at all.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def all_spans():
        raw_traces = _load("trace_malformed_collector_batch.json")
        return trace_ingester.ingest_batch(raw_traces)

    def test_does_not_raise_on_malformed_batch(self, all_spans):
        # If we got here without an exception, the resilience contract holds.
        assert isinstance(all_spans, list)

    def test_span_with_empty_span_id_is_skipped(self, caplog):
        raw_traces = _load("trace_malformed_collector_batch.json")
        with caplog.at_level(logging.WARNING):
            spans = trace_ingester.ingest_batch(raw_traces)
        assert not any(s.span_id == "" for s in spans)
        assert any("empty spanId" in r.message for r in caplog.records)

    def test_span_missing_end_timestamp_is_skipped(self, caplog):
        raw_traces = _load("trace_malformed_collector_batch.json")
        with caplog.at_level(logging.WARNING):
            spans = trace_ingester.ingest_batch(raw_traces)
        # bb11cc22dd33ee44 has no endTimeUnixNano and must be dropped
        assert not any(s.span_id == "bb11cc22dd33ee44" for s in spans)
        assert any("missing timestamp fields" in r.message for r in caplog.records)

    def test_unknown_span_kind_defaults_to_internal(self, caplog):
        raw_traces = _load("trace_malformed_collector_batch.json")
        with caplog.at_level(logging.WARNING):
            spans = trace_ingester.ingest_batch(raw_traces)
        redis_span = next((s for s in spans if s.span_id == "cc22dd33ee44ff55"), None)
        assert redis_span is not None
        assert redis_span.kind == SpanKind.INTERNAL
        assert any("Unknown span kind integer" in r.message for r in caplog.records)

    def test_span_with_empty_trace_id_is_skipped(self, caplog):
        raw_traces = _load("trace_malformed_collector_batch.json")
        with caplog.at_level(logging.WARNING):
            spans = trace_ingester.ingest_batch(raw_traces)
        assert not any(s.span_id == "dd33ee44ff556677" for s in spans)
        assert any("empty traceId" in r.message for r in caplog.records)

    def test_missing_resource_attributes_falls_back_to_unknown_service(self, caplog):
        raw_traces = _load("trace_malformed_collector_batch.json")
        with caplog.at_level(logging.WARNING):
            spans = trace_ingester.ingest_batch(raw_traces)
        kafka_span = next((s for s in spans if s.span_id == "ee44ff5566778899"), None)
        assert kafka_span is not None
        assert kafka_span.service_id == "unknown-service"
        assert kafka_span.kind == SpanKind.CONSUMER
        assert any("Could not determine service ID" in r.message for r in caplog.records)

    def test_surviving_spans_count(self, all_spans):
        # First trace has 4 span definitions: empty spanId (dropped),
        # missing endTimeUnixNano (dropped), redis.get with unknown kind
        # (kept, kind defaults to INTERNAL), orphaned span with empty
        # traceId (dropped). Second trace has 1 span with no resource
        # attributes (kept, service_id falls back to "unknown-service").
        # Net survivors: 2.
        assert len(all_spans) == 2
        span_ids = {s.span_id for s in all_spans}
        assert span_ids == {"cc22dd33ee44ff55", "ee44ff5566778899"}


class TestInvalidTopLevelStructure:
    def test_validate_rejects_non_otel_payload(self):
        raw = _load("trace_invalid_structure.json")
        ok, reason = trace_ingester.validate(raw)
        assert ok is False
        assert "resourceSpans" in reason

    def test_ingest_from_file_with_invalid_structure_returns_empty(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = trace_ingester.ingest_from_file(str(FIXTURES / "trace_invalid_structure.json"))
        assert result == []
        assert any("Invalid trace structure" in r.message for r in caplog.records)

    def test_validate_rejects_non_dict(self):
        ok, reason = trace_ingester.validate(["not", "a", "dict"])  # type: ignore[arg-type]
        assert ok is False
        assert "expected dict" in reason

    def test_validate_rejects_empty_resource_spans(self):
        ok, reason = trace_ingester.validate({"resourceSpans": []})
        assert ok is False
        assert "non-empty list" in reason


class TestFileIngestion:
    def test_ingest_from_missing_file(self, caplog):
        with caplog.at_level(logging.ERROR):
            result = trace_ingester.ingest_from_file(str(FIXTURES / "does_not_exist.json"))
        assert result == []
        assert any("not found" in r.message for r in caplog.records)

    def test_ingest_from_valid_single_trace_file(self):
        result = trace_ingester.ingest_from_file(str(FIXTURES / "trace_cascading_db_pool_exhaustion.json"))
        assert len(result) == 3

    def test_ingest_from_batch_file_skips_invalid_entries(self, caplog):
        """
        A batch file (list of traces) where each entry is independently
        validated. Reuses the malformed-collector fixture as the batch.
        Both top-level entries DO satisfy validate()'s structural checks
        (each has resourceSpans -> scopeSpans -> spans), so both are
        ingested; per-span issues are handled inside ingest().
        """
        with caplog.at_level(logging.WARNING):
            result = trace_ingester.ingest_from_file(str(FIXTURES / "trace_malformed_collector_batch.json"))
        assert len(result) == 2

    def test_ingest_from_truncated_file(self, tmp_path, caplog):
        """Simulates a collector that crashed mid-flush, leaving a
        truncated JSON trace export on disk."""
        truncated = tmp_path / "truncated_trace.json"
        truncated.write_text('{"resourceSpans": [{"resource": {"attributes": [')
        with caplog.at_level(logging.ERROR):
            result = trace_ingester.ingest_from_file(str(truncated))
        assert result == []
        assert any("Failed to parse JSON" in r.message for r in caplog.records)

    def test_ingest_from_file_unexpected_root_type(self, tmp_path, caplog):
        """
        A file whose JSON root is neither dict nor list (e.g. a bare number
        or string written by a buggy exporter or misrouted agent). The
        ingester must log an error and return [] rather than raising.
        """
        bad = tmp_path / "bad_root.json"
        bad.write_text('"just a string"')
        with caplog.at_level(logging.ERROR):
            result = trace_ingester.ingest_from_file(str(bad))
        assert result == []
        assert any("unexpected JSON root type" in r.message for r in caplog.records)