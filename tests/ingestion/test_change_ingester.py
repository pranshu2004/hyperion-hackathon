"""
Ingestion layer tests: change_ingester.

Scenarios simulate real CI/CD and feature-flag webhook failure modes:
  - A healthy canary rollout: deploy -> config change -> feature flag flip,
    all correlated to a single incident window
  - Malformed webhooks: empty service_id, missing required fields, bad
    timestamp formats, wrong-typed deploy_scope, unknown event_type
    (e.g. infra autoscaling events not yet modeled), missing event_type,
    and non-dict entries from a misbehaving webhook relay
  - File-level batch ingestion with validate()-then-ingest() two-pass logic
"""

from __future__ import annotations

import json
import logging

import pytest

from ingestion import change_ingester
from core.change_event import ConfigChangeEvent, DeployEvent, FeatureFlagChangeEvent
from .conftest import FIXTURES, _load


class TestCanaryRolloutBatch:
    """
    Realistic scenario: a canary deploy to checkout-service reduces a DB
    pool timeout, immediately followed by a config change on
    payments-service that shrinks its connection pool, and a feature flag
    flip enabling a new retry path. This is the change-event context that
    Hyperion's RCA engine correlates against the trace/metric anomaly in
    TestCascadingFailureTrace.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def events():
        raw = _load("changes_canary_rollout_batch.json")
        return change_ingester.ingest_batch(raw)

    def test_all_events_parsed(self, events):
        assert len(events) == 3

    def test_deploy_event_fields(self, events):
        deploy = next(e for e in events if isinstance(e, DeployEvent))
        assert deploy.service_id == "checkout-service"
        assert deploy.version == "2.4.1"
        assert deploy.deploy_scope == ["checkout-service"]
        assert deploy.author == "alice@hyperion.dev"
        assert "connection pool timeout" in deploy.diff_summary
        assert deploy.tags["rollout"] == "canary-25pct"

    def test_config_change_event_fields(self, events):
        cfg = next(e for e in events if isinstance(e, ConfigChangeEvent))
        assert cfg.service_id == "payments-service"
        assert cfg.key == "DB_POOL_MAX_CONNECTIONS"
        assert cfg.old_value == "60"
        assert cfg.new_value == "40"
        assert cfg.change_type == "env_var"

    def test_feature_flag_event_fields(self, events):
        ff = next(e for e in events if isinstance(e, FeatureFlagChangeEvent))
        assert ff.service_id == "checkout-service"
        assert ff.flag_key == "new-payment-retry-path"
        assert ff.old_value is False
        assert ff.new_value is True
        assert ff.author == "bob@hyperion.dev"

    def test_ingest_batch_preserves_insertion_order(self, events):
        # ingest_batch() appends in iteration order with no sorting.
        # The fixture events are already in chronological order (14:25, 14:26:30,
        # 14:27), so this verifies insertion-order preservation, not sorting behavior.
        timestamps = [e.timestamp for e in events]
        assert timestamps == sorted(timestamps)


class TestMalformedWebhookBatch:
    """
    Simulates a webhook relay forwarding a mixed batch of payloads:
    a healthy deploy, a deploy with empty service_id, a deploy missing
    `version`, a deploy with a non-ISO timestamp, a deploy whose
    deploy_scope is a string instead of a list, an unmodeled
    "infra_scaling" event type, a config_change missing its required `key`,
    an event with no event_type at all, and a raw string (non-dict) entry.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def raw():
        return _load("changes_malformed_webhook_batch.json")

    def test_does_not_raise(self, raw):
        change_ingester.ingest_batch(raw)

    def test_healthy_deploy_survives(self, raw):
        events = change_ingester.ingest_batch(raw)
        healthy = next((e for e in events if getattr(e, "event_id", None) == "deploy-aaaa1111"), None)
        assert healthy is not None
        assert isinstance(healthy, DeployEvent)
        assert healthy.service_id == "auth-service"
        assert healthy.version == "5.0.0"
        # deploy_scope defaults to [service_id] when omitted
        assert healthy.deploy_scope == ["auth-service"]

    def test_empty_service_id_dropped(self, raw, caplog):
        with caplog.at_level(logging.WARNING):
            events = change_ingester.ingest_batch(raw)
        assert not any(getattr(e, "event_id", None) == "deploy-bbbb2222" for e in events)
        assert any("service_id" in r.message for r in caplog.records)

    def test_missing_version_dropped(self, raw, caplog):
        with caplog.at_level(logging.WARNING):
            events = change_ingester.ingest_batch(raw)
        assert not any(getattr(e, "event_id", None) == "deploy-cccc3333" for e in events)
        assert any("Missing required field" in r.message for r in caplog.records)

    def test_non_iso_timestamp_dropped(self, raw, caplog):
        with caplog.at_level(logging.WARNING):
            events = change_ingester.ingest_batch(raw)
        assert not any(getattr(e, "event_id", None) == "deploy-dddd4444" for e in events)
        assert any("Unparseable timestamp" in r.message for r in caplog.records)

    def test_deploy_scope_wrong_type_dropped(self, raw, caplog):
        # deploy_scope="billing-service" (a string, not a list) must be
        # rejected rather than silently iterated character-by-character.
        with caplog.at_level(logging.WARNING):
            events = change_ingester.ingest_batch(raw)
        assert not any(getattr(e, "event_id", None) == "deploy-eeee5555" for e in events)
        assert any("deploy_scope" in r.message for r in caplog.records)

    def test_unknown_event_type_dropped(self, raw, caplog):
        # "infra_scaling" is not in _PARSERS - common in real systems where
        # autoscaling/infra events are forwarded to the same webhook before
        # Hyperion has a model for them.
        with caplog.at_level(logging.WARNING):
            events = change_ingester.ingest_batch(raw)
        assert not any(getattr(e, "event_id", None) == "scale-ffff6666" for e in events)
        assert any("unknown event_type" in r.message for r in caplog.records)

    def test_config_change_missing_key_dropped(self, raw, caplog):
        with caplog.at_level(logging.WARNING):
            events = change_ingester.ingest_batch(raw)
        assert not any(getattr(e, "event_id", None) == "cfg-1010aaaa" for e in events)
        assert any("'key'" in r.message for r in caplog.records)

    def test_missing_event_type_dropped(self, raw, caplog):
        with caplog.at_level(logging.WARNING):
            events = change_ingester.ingest_batch(raw)
        assert not any(getattr(e, "event_id", None) == "missing-event-type-0001" for e in events)
        assert any("missing 'event_type'" in r.message for r in caplog.records)

    def test_non_dict_entry_does_not_crash_batch(self, raw):
        """
        The last entry in the fixture is a bare string. ingest() guards on
        isinstance(raw, dict), so ingest_batch must skip it without raising.
        """
        events = change_ingester.ingest_batch(raw)
        # Only the one healthy deploy (deploy-aaaa1111) should survive.
        assert len(events) == 1
        assert events[0].event_id == "deploy-aaaa1111"


class TestValidateFunction:
    def test_validate_rejects_non_dict(self):
        ok, reason = change_ingester.validate("oops")  # type: ignore[arg-type]
        assert ok is False
        assert "expected dict" in reason

    def test_validate_rejects_missing_event_type(self):
        ok, reason = change_ingester.validate({"event_id": "x", "service_id": "y", "timestamp": "2025-01-01T00:00:00Z"})
        assert ok is False
        assert "missing 'event_type'" in reason

    def test_validate_rejects_unknown_event_type(self):
        ok, reason = change_ingester.validate({
            "event_type": "infra_scaling",
            "event_id": "x",
            "service_id": "y",
            "timestamp": "2025-01-01T00:00:00Z",
        })
        assert ok is False
        assert "unknown event_type" in reason

    def test_validate_rejects_empty_required_string_field(self):
        ok, reason = change_ingester.validate({
            "event_type": "deploy",
            "event_id": "",
            "service_id": "y",
            "timestamp": "2025-01-01T00:00:00Z",
        })
        assert ok is False
        assert "event_id" in reason


class TestFileIngestion:
    def test_ingest_from_missing_file(self, caplog):
        with caplog.at_level(logging.ERROR):
            result = change_ingester.ingest_from_file(str(FIXTURES / "does_not_exist.json"))
        assert result == []
        assert any("file not found" in r.message for r in caplog.records)

    def test_ingest_from_valid_batch_file(self):
        result = change_ingester.ingest_from_file(str(FIXTURES / "changes_canary_rollout_batch.json"))
        assert len(result) == 3

    def test_ingest_from_malformed_batch_file(self):
        """
        End-to-end: ingest_from_file() runs validate() then ingest() per entry.
        validate() rejects entries with empty service_id, unknown event_type,
        missing event_type, or non-dict shape. Entries that pass validate() but
        carry field-level errors (missing version, bad timestamp, deploy_scope
        as string, missing config key) are caught inside ingest(). Only the
        one fully healthy deploy survives.
        """
        result = change_ingester.ingest_from_file(str(FIXTURES / "changes_malformed_webhook_batch.json"))
        assert len(result) == 1
        assert result[0].event_id == "deploy-aaaa1111"

    def test_ingest_from_truncated_file(self, tmp_path, caplog):
        truncated = tmp_path / "truncated_changes.json"
        truncated.write_text('[{"event_type": "deploy", "event_id": "x"')
        with caplog.at_level(logging.ERROR):
            result = change_ingester.ingest_from_file(str(truncated))
        assert result == []
        assert any("JSON parse error" in r.message for r in caplog.records)

    def test_ingest_from_file_single_object_root(self, tmp_path):
        single = tmp_path / "single_event.json"
        single.write_text(json.dumps({
            "event_type": "feature_flag",
            "event_id": "ff-solo-0001",
            "service_id": "checkout-service",
            "timestamp": "2025-11-02T14:27:00.000Z",
            "flag_key": "solo-flag",
            "old_value": False,
            "new_value": True,
        }))
        result = change_ingester.ingest_from_file(str(single))
        assert len(result) == 1
        assert isinstance(result[0], FeatureFlagChangeEvent)

    def test_ingest_from_file_unexpected_root_type(self, tmp_path, caplog):
        bad = tmp_path / "bad_root.json"
        bad.write_text('"just a string"')
        with caplog.at_level(logging.ERROR):
            result = change_ingester.ingest_from_file(str(bad))
        assert result == []
        assert any("unexpected JSON root type" in r.message for r in caplog.records)
