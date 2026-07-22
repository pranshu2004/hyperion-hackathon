"""
reasoning/neighbour_scan.py — Stage 3b of the reasoning pipeline.

Lightweight signal check on the 1-hop predecessors and successors of each
Domain RCA candidate. Runs during Domain RCA investigation, not after. Emits
InvestigationHint objects for neighbours that warrant investigation.

Does not: parse SQL, read diffs, call the LLM, compute causal confidence,
or run forward simulation.

MVP limitations (documented here; logic changes deferred to V2):
- baselines parameter is accepted but unused. Future use: compare incident-window
  metrics against baselines to detect true sub-threshold movement.
- SUB_THRESHOLD_METRIC hint fires when a neighbour has a change event AND any
  ERROR_RATE or LATENCY_P99 sample in the incident window. This is a broader proxy
  than actual sub-threshold movement detection — it does not compare against baseline.
  True movement detection requires baseline comparison and is deferred to V2.
- Silent degradation detection does not verify the candidate's errors are causally
  linked to calls to that specific successor. Co-occurrence only. V2 should correlate
  parent-child span IDs and peer.service attributes.
- Timeout detection checks error.type only. Real traces may encode timeout signals in
  exception.type, exception.message, span status description, or OTel event attributes.
  Widen in V2.
- Change event lookback uses CHANGE_EVENT_LOOKBACK_MINUTES = 60 before incident_start,
  matching localizer.py. Spec says "in the incident window" but product behavior means
  "recent change before incident start." This is intentional and consistent.
- graph.predecessors and graph.successors raise NetworkXError if candidate_node_id is
  not in the graph. Domain RCA is expected to only call scan() for graph-present
  candidates. Guard with warning log is deferred to V2.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import networkx as nx

from context.baseline_calculator import BaselineStats
from core.metric import Metric, MetricType
from core.nodes import ServiceNode
from core.span import Span
from reasoning.contracts import (
    HintDirection,
    HintSignal,
    HintStrength,
    InvestigationHint,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named constants — must stay in sync with localizer.py
# ---------------------------------------------------------------------------

CHANGE_EVENT_LOOKBACK_MINUTES: int = 60
INCIDENT_WINDOW_SECONDS: int = 300
HTTP_ERROR_STATUS_CODES: set[int] = {503, 429}
TIMEOUT_KEYWORDS: set[str] = {"timeout", "connection"}
HTTP_SUCCESS_STATUS_CODE: int = 200


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan(
    candidate_node_id: str,
    graph: nx.DiGraph,
    spans: list[Span],
    metrics: list[Metric],
    baselines: dict[tuple[str, MetricType], BaselineStats],
    incident_start: datetime,
    already_investigated: set[str],
    iteration: int,
) -> list[InvestigationHint]:
    """
    Check 1-hop neighbours (predecessors and successors) of candidate_node_id
    for signals that pure node-level analysis would miss.

    Returns a flat list of InvestigationHint — one per neighbour at most.
    Never emits a hint for a node in already_investigated.
    Never emits a hint whose node_id == candidate_node_id.
    Empty list is a valid return.
    """
    inc_start_utc = _to_utc(incident_start)

    candidate_spans = _get_neighbour_spans(candidate_node_id, spans, inc_start_utc)
    candidate_has_errors = _has_error_spans(candidate_spans)

    hints: list[InvestigationHint] = []

    for pred_id in graph.predecessors(candidate_node_id):
        if pred_id == candidate_node_id or pred_id in already_investigated:
            continue
        hint = _scan_neighbour(
            candidate_node_id=candidate_node_id,
            neighbour_id=pred_id,
            direction=HintDirection.PREDECESSOR,
            graph=graph,
            spans=spans,
            metrics=metrics,
            inc_start_utc=inc_start_utc,
            candidate_has_errors=candidate_has_errors,
            iteration=iteration,
        )
        if hint is not None:
            hints.append(hint)

    for succ_id in graph.successors(candidate_node_id):
        if succ_id == candidate_node_id or succ_id in already_investigated:
            continue
        hint = _scan_neighbour(
            candidate_node_id=candidate_node_id,
            neighbour_id=succ_id,
            direction=HintDirection.SUCCESSOR,
            graph=graph,
            spans=spans,
            metrics=metrics,
            inc_start_utc=inc_start_utc,
            candidate_has_errors=candidate_has_errors,
            iteration=iteration,
        )
        if hint is not None:
            hints.append(hint)

    return hints


# ---------------------------------------------------------------------------
# Private: per-neighbour evaluation
# ---------------------------------------------------------------------------

def _scan_neighbour(
    candidate_node_id: str,
    neighbour_id: str,
    direction: HintDirection,
    graph: nx.DiGraph,
    spans: list[Span],
    metrics: list[Metric],
    inc_start_utc: datetime,
    candidate_has_errors: bool,
    iteration: int,
) -> InvestigationHint | None:
    neighbour_spans = _get_neighbour_spans(neighbour_id, spans, inc_start_utc)
    has_errors = _has_error_spans(neighbour_spans)
    has_change = _has_change_event(neighbour_id, graph, inc_start_utc)
    has_metric = _has_metric_in_window(neighbour_id, metrics, inc_start_utc)
    http_codes = _get_http_status_codes(neighbour_spans)
    has_http_error_code = bool(http_codes & HTTP_ERROR_STATUS_CODES)
    has_timeout = _has_timeout_signal(neighbour_spans)
    is_silent_deg = (
        direction == HintDirection.SUCCESSOR
        and _is_silent_degradation(candidate_has_errors, neighbour_spans)
    )

    # ------------------------------------------------------------------
    # STRONG conditions — table priority order (top = highest priority)
    # ------------------------------------------------------------------
    strength: HintStrength | None = None
    signal: HintSignal | None = None

    if has_http_error_code or has_timeout:
        strength = HintStrength.STRONG
        signal = HintSignal.ERROR_CODE_PATTERN
    elif is_silent_deg:
        strength = HintStrength.STRONG
        signal = HintSignal.SILENT_DEGRADATION
    elif has_change and has_errors:
        strength = HintStrength.STRONG
        signal = HintSignal.CHANGE_EVENT

    # ------------------------------------------------------------------
    # MODERATE conditions (only if no STRONG was triggered)
    # Priority: MISSING_TELEMETRY > SUB_THRESHOLD_METRIC > SPAN_ERRORS
    #
    # MISSING_TELEMETRY and SPAN_ERRORS are mutually exclusive by definition:
    # - MISSING_TELEMETRY: error spans present, no metric coverage at all
    # - SPAN_ERRORS:       error spans present, some metric coverage exists
    #
    # When has_change and has_errors are both true, STRONG already fired
    # (CHANGE_EVENT), so SUB_THRESHOLD_METRIC only triggers when there
    # are no error spans (change event + sub-threshold metric movement only).
    # ------------------------------------------------------------------
    if strength is None:
        if not has_metric and has_errors:
            strength = HintStrength.MODERATE
            signal = HintSignal.MISSING_TELEMETRY
        elif has_change and has_metric:
            strength = HintStrength.MODERATE
            signal = HintSignal.SUB_THRESHOLD_METRIC
        elif has_errors:
            strength = HintStrength.MODERATE
            signal = HintSignal.SPAN_ERRORS

    if strength is None or signal is None:
        return None

    reason = _build_reason(
        neighbour_id=neighbour_id,
        signal=signal,
        neighbour_spans=neighbour_spans,
        candidate_node_id=candidate_node_id,
    )

    return InvestigationHint(
        node_id=neighbour_id,
        signal=signal,
        direction=direction,
        hint_strength=strength,
        reason=reason,
        suggesting_node_id=candidate_node_id,
        iteration_found=iteration,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _to_utc(dt: datetime) -> datetime:
    """Naive datetime → attach UTC tzinfo. Aware datetime → convert to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _in_window(event_ts: datetime, window_start: datetime, window_end: datetime) -> bool:
    """True if event_ts falls within [window_start, window_end]."""
    ts = _to_utc(event_ts)
    return window_start <= ts <= window_end


def _get_neighbour_spans(
    neighbour_id: str,
    spans: list[Span],
    incident_start_utc: datetime,
) -> list[Span]:
    """Return spans for neighbour_id whose start_time falls in the incident window."""
    window_end = incident_start_utc + timedelta(seconds=INCIDENT_WINDOW_SECONDS)
    return [
        s for s in spans
        if s.service_id == neighbour_id
        and _in_window(s.start_time, incident_start_utc, window_end)
    ]


def _has_error_spans(spans: list[Span]) -> bool:
    """True if any span has otel_status_code == 2."""
    return any(s.otel_status_code == 2 for s in spans)


def _get_http_status_codes(spans: list[Span]) -> set[int]:
    """Collect HTTP status codes from span attributes."""
    codes: set[int] = set()
    for span in spans:
        raw = (
            span.attributes.get("http.status_code")
            or span.attributes.get("http.response.status_code")
        )
        if raw is not None:
            try:
                codes.add(int(raw))
            except (ValueError, TypeError):
                pass
    return codes


def _has_timeout_signal(spans: list[Span]) -> bool:
    """True if any span has a timeout or connection keyword in error.type.

    # MVP limitation: checks error.type only. Real traces may encode timeout/connection
    # signals in exception.type, exception.message, span status description, or OTel
    # event attributes. Widen attribute checks in V2.
    """
    for span in spans:
        error_type = str(span.attributes.get("error.type", "")).lower()
        if any(kw in error_type for kw in TIMEOUT_KEYWORDS):
            return True
    return False


def _has_metric_in_window(
    neighbour_id: str,
    metrics: list[Metric],
    incident_start_utc: datetime,
) -> bool:
    """True if any ERROR_RATE or LATENCY_P99 metric for neighbour exists in the incident window."""
    window_end = incident_start_utc + timedelta(seconds=INCIDENT_WINDOW_SECONDS)
    # MVP: checks for presence of any metric sample in the window, not actual movement
    # vs baseline. SUB_THRESHOLD_METRIC callers depend on this. V2: compare against
    # baselines to detect movement that exists but does not cross the anomaly threshold.
    return any(
        m.node_id == neighbour_id
        and m.metric_type in (MetricType.ERROR_RATE, MetricType.LATENCY_P99)
        and _in_window(m.timestamp, incident_start_utc, window_end)
        for m in metrics
    )


def _has_change_event(
    neighbour_id: str,
    graph: nx.DiGraph,
    incident_start_utc: datetime,
) -> bool:
    """
    True if neighbour is a ServiceNode with at least one change event
    (deploy, config, feature flag, or code change) within
    CHANGE_EVENT_LOOKBACK_MINUTES before incident_start_utc.

    Non-ServiceNode neighbours always return False — change events are
    only tracked on ServiceNodes in the current MVP.
    """
    if neighbour_id not in graph.nodes:
        return False
    node_data = graph.nodes[neighbour_id].get("data")
    if not isinstance(node_data, ServiceNode):
        return False
    window_start = incident_start_utc - timedelta(minutes=CHANGE_EVENT_LOOKBACK_MINUTES)
    for deploy in node_data.recent_deploys:
        if _in_window(deploy.timestamp, window_start, incident_start_utc):
            return True
    for cfg in node_data.recent_config_changes:
        if _in_window(cfg.timestamp, window_start, incident_start_utc):
            return True
    for ff in node_data.recent_feature_flag_changes:
        if _in_window(ff.timestamp, window_start, incident_start_utc):
            return True
    for cc in node_data.recent_code_changes:
        if _in_window(cc.timestamp, window_start, incident_start_utc):
            return True
    return False


def _is_silent_degradation(candidate_has_errors: bool, neighbour_spans: list[Span]) -> bool:
    """
    True when a successor returns HTTP 200 (appears healthy) but the
    candidate has error spans — the successor may be returning malformed
    responses that look successful from its own perspective.

    Conditions: candidate has errors, neighbour has spans, neighbour
    spans carry HTTP 200 status codes, and neighbour itself shows no
    OTel errors (otel_status_code != 2 on all its spans).

    # MVP limitation: does not verify the candidate was erroring specifically on calls
    # to this successor. Co-occurrence of candidate errors + successor HTTP 200s is used
    # as a proxy. V2: correlate span parent-child IDs or peer.service attributes.
    """
    if not candidate_has_errors or not neighbour_spans:
        return False
    http_codes = _get_http_status_codes(neighbour_spans)
    return HTTP_SUCCESS_STATUS_CODE in http_codes and not _has_error_spans(neighbour_spans)


def _build_reason(
    neighbour_id: str,
    signal: HintSignal,
    neighbour_spans: list[Span],
    candidate_node_id: str,
) -> str:
    """Generate a plain-English reason string specific enough for an SRE."""
    if signal == HintSignal.ERROR_CODE_PATTERN:
        http_codes = _get_http_status_codes(neighbour_spans)
        matching = sorted(http_codes & HTTP_ERROR_STATUS_CODES)
        has_timeout = _has_timeout_signal(neighbour_spans)
        parts: list[str] = []
        if matching:
            parts.append(f"HTTP {'/'.join(str(c) for c in matching)} spans in incident window")
        if has_timeout:
            parts.append("connection timeout signals in incident window")
        return f"{neighbour_id} has {' and '.join(parts)}"

    if signal == HintSignal.SILENT_DEGRADATION:
        return (
            f"{neighbour_id} returning HTTP 200 in incident window but "
            f"{candidate_node_id} has error spans — possible silent degradation "
            f"(malformed or semantically incorrect responses)"
        )

    if signal == HintSignal.CHANGE_EVENT:
        return (
            f"{neighbour_id} has a change event and error spans in the incident window"
        )

    if signal == HintSignal.MISSING_TELEMETRY:
        return (
            f"{neighbour_id} has error spans in the incident window but no metric coverage "
            f"— possible missing telemetry masking the real root cause"
        )

    if signal == HintSignal.SUB_THRESHOLD_METRIC:
        return (
            f"{neighbour_id} has a recent change event and metric movement in the incident window "
            f"below the anomaly detection threshold"
        )

    if signal == HintSignal.SPAN_ERRORS:
        return f"{neighbour_id} has error spans in the incident window"

    return f"{neighbour_id} shows signals warranting investigation"