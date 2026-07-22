"""
OTel trace ingester.

Parses raw OpenTelemetry trace JSON payloads and emits normalized Span objects
(core.span.Span). Stateless — each call processes an independent batch of
trace data. Must handle missing/malformed fields gracefully without raising.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from core.span import Span, SpanKind

logger = logging.getLogger(__name__)


def _extract_attributes(raw_attrs: list[dict]) -> dict[str, str]:
    if not raw_attrs:
        return {}
    result: dict[str, str] = {}
    for attr in raw_attrs:
        key = attr.get("key")
        value_map = attr.get("value", {})
        if not key or not value_map:
            continue
        if "stringValue" in value_map:
            result[key] = str(value_map["stringValue"])
        elif "intValue" in value_map:
            result[key] = str(int(value_map["intValue"]))
        elif "boolValue" in value_map:
            result[key] = str(value_map["boolValue"]).lower()
        elif "doubleValue" in value_map:
            result[key] = str(value_map["doubleValue"])
        else:
            logger.debug("Skipping attribute %r: unrecognized value type %r", key, value_map)
    return result


_SPAN_KIND_MAP: dict[int, SpanKind] = {
    0: SpanKind.INTERNAL,
    1: SpanKind.INTERNAL,
    2: SpanKind.SERVER,
    3: SpanKind.CLIENT,
    4: SpanKind.PRODUCER,
    5: SpanKind.CONSUMER,
}


def _parse_span_kind(kind_int: int) -> SpanKind:
    kind = _SPAN_KIND_MAP.get(kind_int)
    if kind is None:
        logger.warning("Unknown span kind integer %d, defaulting to INTERNAL", kind_int)
        return SpanKind.INTERNAL
    return kind


def _parse_timestamp(nano_str: str) -> datetime:
    """
    Converts OTel nanosecond timestamp string to UTC datetime.
    Uses integer arithmetic throughout to avoid floating point
    precision loss on large nanosecond values.
    Raises ValueError on non-numeric input.
    """
    total_ns = int(nano_str)
    seconds, remainder_ns = divmod(total_ns, 1_000_000_000)
    microseconds = remainder_ns // 1_000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
        microsecond=microseconds
    )


def _parse_http_status(attributes: dict[str, str]) -> int | None:
    for key in ("http.status_code", "http.response.status_code"):
        raw = attributes.get(key)
        if raw is not None:
            try:
                return int(raw)
            except (ValueError, TypeError):
                pass
    return None


def _extract_resource_attributes(resource: dict) -> dict[str, str]:
    if not resource:
        return {}
    raw_attrs = resource.get("attributes")
    if not raw_attrs:
        return {}
    return _extract_attributes(raw_attrs)


def _normalize_events(raw_events: list[dict]) -> list[dict]:
    """
    Normalizes OTel span events so event attributes are flat dicts
    instead of OTel key-value list format.

    Input:
        [{"name": "exception", "timeUnixNano": "...", "attributes": [
            {"key": "exception.type", "value": {"stringValue": "NullPointerException"}},
            {"key": "exception.message", "value": {"stringValue": "..."}}
        ]}]

    Output:
        [{"name": "exception", "timeUnixNano": "...", "attributes": {
            "exception.type": "NullPointerException",
            "exception.message": "..."
        }}]

    Never raises. Returns empty list on None or empty input.
    Skips malformed events with a debug log.
    """
    if not raw_events:
        return []
    result = []
    for event in raw_events:
        try:
            normalized = dict(event)
            raw_attrs = event.get("attributes", [])
            normalized["attributes"] = (
                _extract_attributes(raw_attrs) if isinstance(raw_attrs, list) else raw_attrs
            )
            result.append(normalized)
        except Exception as exc:
            logger.debug("Skipping malformed event: %s", exc)
    return result


def _get_service_id(resource_attributes: dict[str, str]) -> str:
    for key in ("service.name", "service.instance.id"):
        value = resource_attributes.get(key)
        if value:
            return value
    logger.warning("Could not determine service ID from resource attributes, using 'unknown-service'")
    return "unknown-service"


def ingest(raw: dict) -> list[Span]:
    """
    Parse one raw OTel trace JSON dict into a list of Span objects.

    Args:
        raw: One OTel trace dict with 'resourceSpans' key.
             As produced by trace_generator.generate_normal() or
             trace_generator.generate_failure().

    Returns:
        List of normalized Span objects. May be empty if all spans
        are malformed. Never raises — malformed spans are skipped
        with a warning log.

    Design:
        - One resourceSpans entry = one service's spans in this trace
        - Resource attributes extracted once per resourceSpans entry
        - service_id derived from resource attributes via _get_service_id
        - parentSpanId absent or empty string → parent_span_id = None
        - duration_ms computed from start and end timestamps
        - Spans with endTimeUnixNano < startTimeUnixNano (clock skew /
          agent bugs) produce negative duration_ms and are dropped with
          a warning — negative durations must never reach the reasoning engine
        - otel_status_code from status.code (default 0 if missing)
        - http_status_code from _parse_http_status on span attributes
        - Each failed span parse is logged as warning and skipped
    """
    spans: list[Span] = []
    for resource_spans in raw.get("resourceSpans", []):
        resource_attributes = _extract_resource_attributes(
            resource_spans.get("resource", {})
        )
        service_id = _get_service_id(resource_attributes)
        for scope_spans in resource_spans.get("scopeSpans", []):
            for span_dict in scope_spans.get("spans", []):
                try:
                    trace_id = span_dict.get("traceId", "")
                    if not trace_id:
                        logger.warning("Skipping span: empty traceId")
                        continue
                    span_id = span_dict.get("spanId", "")
                    if not span_id:
                        logger.warning("Skipping span: empty spanId in trace %s", trace_id)
                        continue
                    if "startTimeUnixNano" not in span_dict or "endTimeUnixNano" not in span_dict:
                        logger.warning(
                            "Skipping span %s: missing timestamp fields", span_id
                        )
                        continue
                    start_time = _parse_timestamp(span_dict["startTimeUnixNano"])
                    end_time = _parse_timestamp(span_dict["endTimeUnixNano"])
                    duration_ms = (end_time - start_time).total_seconds() * 1000
                    # Clock-skew / agent-bug guard: endTimeUnixNano < startTimeUnixNano
                    # produces a negative duration. Such spans must be dropped here so
                    # that negative duration_ms never reaches the reasoning engine.
                    if duration_ms < 0:
                        logger.warning(
                            "Skipping span %s: negative duration (%.1f ms) — "
                            "clock skew or agent bug (end before start)",
                            span_id, duration_ms,
                        )
                        continue
                    attributes = _extract_attributes(span_dict.get("attributes", []))
                    spans.append(Span(
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=span_dict.get("parentSpanId") or None,
                        service_id=service_id,
                        operation_name=span_dict.get("name", "unknown"),
                        start_time=start_time,
                        end_time=end_time,
                        duration_ms=duration_ms,
                        kind=_parse_span_kind(span_dict.get("kind", 0)),
                        otel_status_code=span_dict.get("status", {}).get("code", 0),
                        http_status_code=_parse_http_status(attributes),
                        attributes=attributes,
                        resource_attributes=resource_attributes,
                        events=_normalize_events(span_dict.get("events", [])),
                    ))
                except ValueError as exc:
                    logger.warning("Skipping span due to timestamp parse error: %s", exc)
                except Exception as exc:
                    logger.warning("Skipping malformed span: %s", exc)
    return spans


def ingest_batch(raw_traces: list[dict]) -> list[Span]:
    """
    Parse a list of raw OTel trace dicts into a flat list of Span objects.

    Args:
        raw_traces: List of OTel trace dicts, as returned by
                    trace_generator.generate_normal() or
                    trace_generator.generate_failure().

    Returns:
        Flat list of all Span objects across all traces.
        Traces that fail entirely are skipped with a warning.
        Never raises.
    """
    spans: list[Span] = []
    for i, raw in enumerate(raw_traces):
        try:
            spans.extend(ingest(raw))
        except Exception as exc:
            logger.warning("Skipping trace at index %d: %s", i, exc)
    return spans


def validate(raw: dict) -> tuple[bool, str]:
    """
    Validate that a raw dict has the expected OTel trace structure.
    Does NOT validate individual span fields — only top-level structure.

    Returns:
        (True, "") if valid
        (False, reason) if invalid

    Checks in order:
    1. raw is a dict
    2. "resourceSpans" key exists
    3. "resourceSpans" is a non-empty list
    4. Each entry in resourceSpans has "scopeSpans" key
    5. Each scopeSpans entry has "spans" key

    Never raises. Returns (False, reason) on any unexpected input.
    """
    try:
        if not isinstance(raw, dict):
            return False, f"expected dict, got {type(raw).__name__}"
        if "resourceSpans" not in raw:
            return False, "missing 'resourceSpans' key"
        resource_spans = raw["resourceSpans"]
        if not isinstance(resource_spans, list) or len(resource_spans) == 0:
            return False, "'resourceSpans' must be a non-empty list"
        for i, rs in enumerate(resource_spans):
            if not isinstance(rs, dict) or "scopeSpans" not in rs:
                return False, f"resourceSpans[{i}] missing 'scopeSpans' key"
            for j, ss in enumerate(rs["scopeSpans"]):
                if not isinstance(ss, dict) or "spans" not in ss:
                    return False, f"resourceSpans[{i}].scopeSpans[{j}] missing 'spans' key"
        return True, ""
    except Exception as exc:
        return False, str(exc)


def ingest_from_file(path: str) -> list[Span]:
    """
    Load OTel trace JSON from a file and return Span objects.

    Supports two file formats:
    1. Single trace: {"resourceSpans": [...]}
    2. Batch of traces: [{"resourceSpans": [...]}, ...]

    Args:
        path: Path to JSON file on disk.

    Returns:
        List of Span objects. Empty list if file is invalid or empty.

    Behavior:
        - Logs error and returns [] if file not found
        - Logs error and returns [] if JSON parse fails
        - Validates structure before ingesting
        - Logs warning and skips invalid traces in batch mode
        - Logs error and returns [] if JSON root is neither dict nor list
        - Never raises
    """
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.error("Trace file not found: %s", path)
        return []
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse JSON from %s: %s", path, exc)
        return []
    except Exception as exc:
        logger.error("Failed to load trace file %s: %s", path, exc)
        return []

    if isinstance(data, list):
        spans: list[Span] = []
        for i, raw in enumerate(data):
            ok, reason = validate(raw)
            if not ok:
                logger.warning("Skipping trace at index %d in %s: %s", i, path, reason)
                continue
            spans.extend(ingest(raw))
        return spans

    if isinstance(data, dict):
        ok, reason = validate(data)
        if not ok:
            logger.warning("Invalid trace structure in %s: %s", path, reason)
            return []
        return ingest(data)

    # JSON root is neither dict nor list (e.g. bare number, string, null)
    logger.error(
        "Trace file %s has unexpected JSON root type %s — expected dict or list",
        path, type(data).__name__,
    )
    return []
