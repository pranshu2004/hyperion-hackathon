"""
Ingestion layer tests: metric_ingester.

Scenarios simulate real metrics-pipeline failure modes seen in production:
  - Mixed valid/invalid records in a single scrape batch
  - Exporter version skew (unknown metric_type / node_type)
  - NaN / non-numeric values from broken instrumentation
  - Negative values from counter resets
  - Clock format drift between collector versions
  - Zero/missing window_seconds from misconfigured scrape intervals
  - Crashed-exporter mid-write producing truncated JSON files
"""

from __future__ import annotations

import json
import logging
import math

import pytest

from ingestion import metric_ingester
from core.metric import MetricType
from core.nodes import NodeType
from .conftest import FIXTURES, _load


class TestValidIngestion:
    def test_ingest_batch_all_valid(self):
        raw = _load("metrics_valid_batch.json")
        metrics = metric_ingester.ingest_batch(raw)

        assert len(metrics) == 3
        assert metrics[0].node_id == "checkout-service"
        assert metrics[0].metric_type == MetricType.LATENCY_P99
        assert metrics[0].node_type == NodeType.SERVICE
        assert metrics[0].value == pytest.approx(842.5)

    def test_timestamp_formats_both_supported(self):
        raw = _load("metrics_valid_batch.json")
        metrics = metric_ingester.ingest_batch(raw)
        # raw[0] has fractional seconds, raw[1] does not - both must parse
        assert metrics[0].timestamp.year == 2025
        assert metrics[1].timestamp.year == 2025


class TestPartialCorruptionBatch:
    """
    Simulates a real scrape batch where one exporter is healthy and another
    is emitting garbage (NaN values, unknown metric types from a newer
    exporter version, empty node_id, negative values from counter resets,
    inconsistent timestamp formats, and zero/missing window_seconds).

    The ingester must be resilient: drop bad records, keep good ones,
    never raise.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def raw():
        return _load("metrics_partial_corruption_batch.json")

    def test_does_not_raise(self, raw):
        # Should never throw regardless of how malformed individual records are
        metric_ingester.ingest_batch(raw)

    def test_only_valid_records_survive(self, raw):
        metrics = metric_ingester.ingest_batch(raw)
        node_ids = {m.node_id for m in metrics}

        # checkout-service latency_p99 (record 0) is valid
        assert "checkout-service" in node_ids
        # billing-queue consumer_lag (record 9) is also structurally valid
        assert "billing-queue" in node_ids
        # All other records (1-9) are malformed in some way and must be dropped:
        #   record 1  — value="NaN" (string) rejected by validate(); even if
        #               float("NaN") were reached, math.isfinite() now drops it
        #   records 2-8 — various other malformed fields (see fixture comments)
        assert len(metrics) == 2  # records 0 and 9 only

    def test_nan_value_is_rejected(self, raw, caplog):
        """
        Record index 1 has value="NaN" (a JSON string).
        validate() rejects it via its isinstance(value, (int, float)) check,
        so it is dropped before ingest() is called in the file-loading path.
        When ingest_batch() is called directly (bypassing validate()), the
        float("NaN") conversion now hits the math.isfinite() guard added to
        ingest(), so it is also dropped there.

        This test asserts the FIXED behavior: no NaN-valued Metric must leak
        into the result list.
        """
        with caplog.at_level(logging.WARNING):
            metrics = metric_ingester.ingest_batch(raw)
        nan_metrics = [m for m in metrics if not math.isfinite(m.value)]
        assert len(nan_metrics) == 0, (
            "No Metric with a non-finite value should reach callers. "
            "The math.isfinite() guard in ingest() must drop them."
        )
        assert any("Non-finite value" in r.message for r in caplog.records)

    def test_unknown_metric_type_dropped(self, raw, caplog):
        # record index 2: metric_type = "gpu_utilization" (exporter version skew)
        with caplog.at_level(logging.WARNING):
            metrics = metric_ingester.ingest_batch(raw)
        assert all(m.metric_type != "gpu_utilization" for m in metrics)
        assert any("Unknown metric_type" in r.message for r in caplog.records)

    def test_empty_node_id_dropped(self, raw):
        # record index 3: node_id = ""
        metrics = metric_ingester.ingest_batch(raw)
        assert all(m.node_id != "" for m in metrics)

    def test_negative_value_dropped(self, raw, caplog):
        # record index 4: auth-service error_rate = -0.01 (counter reset artifact)
        with caplog.at_level(logging.WARNING):
            metrics = metric_ingester.ingest_batch(raw)
        assert not any(
            m.node_id == "auth-service" and m.metric_type == MetricType.ERROR_RATE
            for m in metrics
        )

    def test_non_iso_timestamp_dropped(self, raw):
        # record index 5: timestamp = "2025-11-02 14:31:00" (no T, no Z)
        metrics = metric_ingester.ingest_batch(raw)
        assert not any(
            m.node_id == "auth-service" and m.metric_type == MetricType.REQUEST_RATE
            and m.unit == "rps"
            and m.window_seconds == 60
            for m in metrics
        )

    def test_zero_window_seconds_dropped(self, raw):
        # record index 6: window_seconds = 0
        metrics = metric_ingester.ingest_batch(raw)
        assert all(m.window_seconds > 0 for m in metrics)

    def test_missing_window_seconds_dropped(self, raw):
        # record index 7: window_seconds key absent entirely
        metrics = metric_ingester.ingest_batch(raw)
        assert not any(
            m.node_id == "auth-service" and m.metric_type == MetricType.REQUEST_RATE
            for m in metrics
        )

    def test_unknown_node_type_dropped(self, raw, caplog):
        # record index 8: node_type = "vm_instance" (not in _NODE_TYPE_MAP)
        with caplog.at_level(logging.WARNING):
            metrics = metric_ingester.ingest_batch(raw)
        assert any("Unknown node_type" in r.message for r in caplog.records)

    def test_queue_consumer_lag_survives(self, raw):
        # record index 9 (billing-queue consumer_lag) is structurally valid
        # and must survive alongside the checkout-service record.
        metrics = metric_ingester.ingest_batch(raw)
        queue_metrics = [m for m in metrics if m.node_id == "billing-queue"]
        assert len(queue_metrics) == 1
        assert queue_metrics[0].metric_type == MetricType.CONSUMER_LAG
        assert queue_metrics[0].value == pytest.approx(15400.0)


class TestBoolCoercionInIngest:
    """
    bool is a subclass of int in Python, so value=True or window_seconds=True
    slip through isinstance(x, (int, float)) and int() checks silently.
    ingest() must explicitly reject booleans before any numeric coercion.
    These tests document the required behavior at the ingest() layer, where
    the raw parsed-JSON value arrives before validate() has run.
    """

    _BASE = {
        "node_id": "checkout-service",
        "node_type": "service",
        "metric_type": "latency_p99",
        "timestamp": "2025-11-02T14:31:00.000Z",
        "unit": "ms",
    }

    def test_ingest_rejects_bool_value(self):
        """value=True must be rejected; isinstance(True, int) must not fool ingest()."""
        raw = {**self._BASE, "value": True, "window_seconds": 60}
        assert metric_ingester.ingest(raw) is None

    def test_ingest_rejects_bool_window_seconds(self):
        """window_seconds=True must be rejected; True > 0 is True in Python."""
        raw = {**self._BASE, "value": 1.0, "window_seconds": True}
        assert metric_ingester.ingest(raw) is None


class TestNonFiniteValueInIngest:
    """
    math.isfinite() guard in ingest() — not just validate()'s incidental
    string-type rejection.  Both paths by which NaN can arrive must be caught:
      - A quoted "NaN" string is caught by validate()'s isinstance check.
      - An unquoted JSON NaN token (float('nan') after json.loads) currently
        passes the `value < 0` guard since nan < 0 is False.
    These tests exercise the ingest() layer directly to pin the required fix.
    """

    _BASE = {
        "node_id": "checkout-service",
        "node_type": "service",
        "metric_type": "latency_p99",
        "timestamp": "2025-11-02T14:31:00.000Z",
        "window_seconds": 60,
        "unit": "ms",
    }

    def test_ingest_rejects_nan_float(self):
        """float('nan') passed as value must be caught by math.isfinite()."""
        raw = {**self._BASE, "value": float("nan")}
        result = metric_ingester.ingest(raw)
        assert result is None, (
            "ingest() should reject non-finite values via math.isfinite(). "
            "Add: if not math.isfinite(value): return None"
        )

    def test_ingest_rejects_positive_infinity(self):
        """float('inf') is non-finite and must also be rejected."""
        raw = {**self._BASE, "value": float("inf")}
        result = metric_ingester.ingest(raw)
        assert result is None

    def test_ingest_rejects_negative_infinity(self):
        raw = {**self._BASE, "value": float("-inf")}
        result = metric_ingester.ingest(raw)
        assert result is None


class TestNonDictInput:
    def test_ingest_rejects_non_dict(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = metric_ingester.ingest(["not", "a", "dict"])  # type: ignore[arg-type]
        assert result is None
        assert any("non-dict input" in r.message for r in caplog.records)

    def test_ingest_batch_empty_list(self):
        assert metric_ingester.ingest_batch([]) == []


class TestMissingRequiredField:
    """
    Verifies that ingest() rejects a record missing a required field.
    Kept separate from TestNonDictInput because the input here is a valid
    dict — the failure mode is a missing key, not a wrong container type.

    Implementation note: metric_ingester should derive required fields
    from the Metric dataclass rather than duplicating them in validate():\n
        REQUIRED_METRIC_FIELDS: tuple[str, ...] = tuple(
            f.name for f in dataclasses.fields(Metric)
            if f.default is dataclasses.MISSING
            and f.default_factory is dataclasses.MISSING
        )

    This avoids the validate() / Metric definition going out of sync when
    fields are added or made optional.
    """

    def test_ingest_rejects_missing_window_seconds(self):
        raw = {
            "node_id": "checkout-service",
            "node_type": "service",
            "metric_type": "latency_p99",
            "value": 100.0,
            "timestamp": "2025-11-02T14:31:00.000Z",
            # window_seconds missing entirely
            "unit": "ms",
        }
        assert metric_ingester.ingest(raw) is None


class TestFileIngestion:
    def test_ingest_from_missing_file(self, caplog):
        with caplog.at_level(logging.ERROR):
            result = metric_ingester.ingest_from_file(str(FIXTURES / "does_not_exist.json"))
        assert result == []
        assert any("not found" in r.message for r in caplog.records)

    def test_ingest_from_truncated_file(self, caplog):
        """
        Simulates a metrics exporter that crashed mid-write, leaving an
        invalid/truncated JSON file on disk for the ingester to pick up
        on its next poll cycle.
        """
        with caplog.at_level(logging.ERROR):
            result = metric_ingester.ingest_from_file(str(FIXTURES / "metrics_truncated_write.json"))
        assert result == []
        assert any("Failed to parse JSON" in r.message for r in caplog.records)

    def test_ingest_from_valid_file(self):
        result = metric_ingester.ingest_from_file(str(FIXTURES / "metrics_valid_batch.json"))
        assert len(result) == 3

    def test_ingest_from_file_with_partial_corruption(self):
        """
        validate() runs before ingest() in ingest_from_file(), and rejects
        non-numeric `value` (e.g. the string "NaN") outright via its
        isinstance check. Additionally, even if a float NaN bypassed
        validate(), the math.isfinite() guard in ingest() would drop it.
        2 records survive: checkout latency_p99 (record 0) and billing-queue
        consumer_lag (record 9).
        """
        result = metric_ingester.ingest_from_file(str(FIXTURES / "metrics_partial_corruption_batch.json"))
        assert len(result) == 2
        node_ids = {m.node_id for m in result}
        assert node_ids == {"checkout-service", "billing-queue"}

    def test_ingest_from_file_single_object_root(self, tmp_path):
        """A file containing a single metric dict (not wrapped in a list)
        must also be handled — common when a single-event webhook writes
        one JSON object to disk."""
        single = tmp_path / "single_metric.json"
        single.write_text(json.dumps({
            "node_id": "checkout-service",
            "node_type": "service",
            "metric_type": "latency_p99",
            "value": 100.0,
            "timestamp": "2025-11-02T14:31:00.000Z",
            "window_seconds": 60,
            "unit": "ms",
        }))
        result = metric_ingester.ingest_from_file(str(single))
        assert len(result) == 1

    def test_ingest_from_file_unexpected_root_type(self, tmp_path, caplog):
        """A file whose JSON root is neither dict nor list (e.g. a bare
        number or string written by a buggy exporter)."""
        bad = tmp_path / "bad_root.json"
        bad.write_text("42")
        with caplog.at_level(logging.ERROR):
            result = metric_ingester.ingest_from_file(str(bad))
        assert result == []
        assert any("Unexpected JSON structure" in r.message for r in caplog.records)


class TestValidateFunction:
    def test_validate_rejects_non_dict(self):
        ok, reason = metric_ingester.validate("not a dict")  # type: ignore[arg-type]
        assert ok is False
        assert "Expected dict" in reason

    def test_validate_rejects_non_numeric_value(self):
        raw = {
            "node_id": "x",
            "node_type": "service",
            "metric_type": "latency_p99",
            "value": "high",
            "timestamp": "2025-11-02T14:31:00.000Z",
            "window_seconds": 60,
            "unit": "ms",
        }
        ok, reason = metric_ingester.validate(raw)
        assert ok is False
        assert "value must be a number" in reason

    def test_validate_rejects_bool_value(self):
        """bool is a subclass of int, so isinstance(True, (int, float)) is True.
        validate() must reject it before ingest() does — the two must agree."""
        raw = {
            "node_id": "x",
            "node_type": "service",
            "metric_type": "latency_p99",
            "value": True,
            "timestamp": "2025-11-02T14:31:00.000Z",
            "window_seconds": 60,
            "unit": "ms",
        }
        ok, reason = metric_ingester.validate(raw)
        assert ok is False
        assert "value must be a number" in reason

    def test_validate_rejects_bool_window_seconds(self):
        """bool is a subclass of int, so isinstance(True, int) and True > 0 are
        both True. validate() must reject it to keep the validate/ingest contract."""
        raw = {
            "node_id": "x",
            "node_type": "service",
            "metric_type": "latency_p99",
            "value": 1.0,
            "timestamp": "2025-11-02T14:31:00.000Z",
            "window_seconds": True,
            "unit": "ms",
        }
        ok, reason = metric_ingester.validate(raw)
        assert ok is False
        assert "window_seconds" in reason
