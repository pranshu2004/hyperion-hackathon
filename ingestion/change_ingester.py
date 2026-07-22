"""
Change event ingester.

Parses raw deploy and configuration change event payloads and emits normalized
DeployEvent, ConfigChangeEvent, or FeatureFlagChangeEvent objects
(core.change_event). Stateless. Intended to consume webhooks from
GitHub/GitLab/CircleCI and feature-flag systems.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from core.change_event import (
    ConfigChangeEvent,
    DeployEvent,
    FeatureFlagChangeEvent,
)

logger = logging.getLogger(__name__)

ChangeEvent = DeployEvent | ConfigChangeEvent | FeatureFlagChangeEvent


def _parse_timestamp(ts_str: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unparseable timestamp: {ts_str!r}")


def _parse_tags(raw_tags: object) -> dict[str, str]:
    if not isinstance(raw_tags, dict):
        return {}
    return {k: str(v) for k, v in raw_tags.items()}


def _parse_deploy_event(raw: dict) -> DeployEvent:
    for field in ("event_id", "service_id", "timestamp", "version"):
        if not raw.get(field):
            raise ValueError(f"Missing required field: {field!r}")

    deploy_scope = raw.get("deploy_scope")
    if deploy_scope is None:
        deploy_scope = [raw["service_id"]]
    elif not isinstance(deploy_scope, list):
        raise ValueError(f"deploy_scope must be a list, got {type(deploy_scope)}")

    return DeployEvent(
        event_id=raw["event_id"],
        service_id=raw["service_id"],
        timestamp=_parse_timestamp(raw["timestamp"]),
        version=raw["version"],
        deploy_scope=deploy_scope,
        author=raw.get("author") or None,
        diff_summary=raw.get("diff_summary") or None,
        tags=_parse_tags(raw.get("tags")),
    )


def _parse_config_change_event(raw: dict) -> ConfigChangeEvent:
    for field in ("event_id", "service_id", "timestamp", "change_type", "key"):
        if not raw.get(field):
            raise ValueError(f"Missing required field: {field!r}")

    return ConfigChangeEvent(
        event_id=raw["event_id"],
        service_id=raw["service_id"],
        timestamp=_parse_timestamp(raw["timestamp"]),
        change_type=raw["change_type"],
        key=raw["key"],
        old_value=raw.get("old_value"),
        new_value=raw.get("new_value"),
        author=raw.get("author") or None,
        tags=_parse_tags(raw.get("tags")),
    )


def _parse_feature_flag_event(raw: dict) -> FeatureFlagChangeEvent:
    for field in ("event_id", "service_id", "timestamp", "flag_key"):
        if not raw.get(field):
            raise ValueError(f"Missing required field: {field!r}")

    return FeatureFlagChangeEvent(
        event_id=raw["event_id"],
        service_id=raw["service_id"],
        timestamp=_parse_timestamp(raw["timestamp"]),
        flag_key=raw["flag_key"],
        old_value=raw.get("old_value"),
        new_value=raw.get("new_value"),
        author=raw.get("author") or None,
        tags=_parse_tags(raw.get("tags")),
    )


_PARSERS = {
    "deploy": _parse_deploy_event,
    "config_change": _parse_config_change_event,
    "feature_flag": _parse_feature_flag_event,
}


def ingest(raw: dict) -> ChangeEvent | None:
    if not isinstance(raw, dict):
        logger.warning("ingest() received non-dict input: %r", type(raw))
        return None

    event_type = raw.get("event_type")
    if not event_type:
        logger.warning("ingest() missing 'event_type' field")
        return None

    parser = _PARSERS.get(event_type)
    if parser is None:
        logger.warning("ingest() unknown event_type: %r", event_type)
        return None

    try:
        return parser(raw)
    except (ValueError, KeyError) as exc:
        logger.warning("ingest() failed to parse %r event: %s", event_type, exc)
        return None


_VALID_EVENT_TYPES = frozenset(_PARSERS)


def ingest_batch(raw_events: list[dict]) -> list[ChangeEvent]:
    if not raw_events:
        return []
    results = []
    for raw in raw_events:
        event = ingest(raw)
        if event is not None:
            results.append(event)
    return results


def validate(raw: dict) -> tuple[bool, str]:
    if not isinstance(raw, dict):
        return False, f"expected dict, got {type(raw).__name__}"

    event_type = raw.get("event_type")
    if event_type is None:
        return False, "missing 'event_type'"
    if event_type not in _VALID_EVENT_TYPES:
        return False, f"unknown event_type: {event_type!r}"

    for field in ("event_id", "service_id", "timestamp"):
        value = raw.get(field)
        if not isinstance(value, str) or not value:
            return False, f"missing or empty required field: {field!r}"

    return True, ""


def ingest_from_file(path: str) -> list[ChangeEvent]:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.error("ingest_from_file() file not found: %r", path)
        return []
    except json.JSONDecodeError as exc:
        logger.error("ingest_from_file() JSON parse error in %r: %s", path, exc)
        return []

    if isinstance(data, dict):
        raw_events = [data]
    elif isinstance(data, list):
        raw_events = data
    else:
        logger.error("ingest_from_file() unexpected JSON root type in %r: %r", path, type(data))
        return []

    results = []
    for raw in raw_events:
        ok, reason = validate(raw)
        if not ok:
            logger.warning("ingest_from_file() skipping invalid event: %s", reason)
            continue
        event = ingest(raw)
        if event is not None:
            results.append(event)
    return results
