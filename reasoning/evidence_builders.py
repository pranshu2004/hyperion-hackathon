"""
reasoning/evidence_builders.py

Factory functions that produce EvidenceItem objects for use by domain engines
in reasoning/. This file contains no scoring logic, no confidence weights,
and no pipeline logic — only builders.

Signal strings defined in this file:
    deploy_in_window          — deploy detected within the incident window
    no_deploy_in_window       — no deploy found; negative signal for code domain
    error_rate_spike          — metric anomaly: error rate above baseline
    latency_spike             — metric anomaly: latency above baseline
    stacktrace_match          — stack frame matched against a changed function
    code_change_in_window     — specific function/file changed in the deploy diff
    exception_in_spans        — exception type + message extracted from spans
    slow_query                — DB span duration anomaly with query pattern
    connection_exhaustion     — DB connection failures across multiple spans
    db_integrity_error        — database exception matched against integrity/constraint patterns
    caller_timeout            — predecessor service span carries timeout exception linked to DB candidate
    db_statement              — raw SQL statement captured from db.statement span attribute
    http_error_pattern        — sustained HTTP error code from an external dep
    all_callers_affected      — blast radius check across dep callers
    feature_flag_change       — FeatureFlagChangeEvent in the incident window
    config_change             — ConfigChangeEvent in the incident window
    missing_telemetry         — limited span/metric coverage on a node
    silent_degradation        — dep returns 200 but caller is erroring

Rule: builders own EvidenceStrength. Callers never pass strength as a
parameter. Strength is intrinsic to the signal type and observed values,
determined entirely within each builder.
"""

from reasoning.contracts import EvidenceItem, EvidenceStrength

# ---------------------------------------------------------------------------
# Tunable thresholds — product policy, expected to be adjusted over time
# ---------------------------------------------------------------------------

DEPLOY_STRONG_MINUTES        = 15.0
CONFIG_STRONG_MINUTES        = 5.0
CONFIG_MODERATE_MINUTES      = 15.0
ERROR_ZSCORE_STRONG          = 5.0
ERROR_ZSCORE_MODERATE        = 3.0
LATENCY_STRONG_MULTIPLIER    = 3.0
LATENCY_MODERATE_MULTIPLIER  = 1.5
SLOW_QUERY_STRONG_MULTIPLIER = 3.0
CONN_EXHAUSTION_STRONG_FRAC  = 0.5
HTTP_ERROR_STRONG_FRAC       = 0.5
HTTP_ERROR_STRONG_DURATION   = 2.0


def make_deploy_evidence(
    version: str,
    minutes_before: float,
    service_id: str,
) -> EvidenceItem:
    strength = EvidenceStrength.STRONG if minutes_before <= DEPLOY_STRONG_MINUTES else EvidenceStrength.MODERATE
    return EvidenceItem(
        signal="deploy_in_window",
        finding=f"{service_id} deployed {version} {minutes_before:.0f} minutes before incident",
        strength=strength,
        metadata={
            "version": version,
            "minutes_before": str(minutes_before),
            "service_id": service_id,
        },
    )


def make_no_deploy_evidence(service_id: str, lookback_minutes: float) -> EvidenceItem:
    return EvidenceItem(
        signal="no_deploy_in_window",
        finding=f"no deploy on {service_id} in the past {lookback_minutes:.0f} minutes",
        strength=EvidenceStrength.WEAK,
        metadata={
            "service_id": service_id,
            "lookback_minutes": str(lookback_minutes),
        },
    )


def make_error_rate_evidence(
    node_id: str,
    current_pct: float,
    baseline_pct: float,
    z_score: float,
) -> EvidenceItem:
    if z_score >= ERROR_ZSCORE_STRONG:
        strength = EvidenceStrength.STRONG
    elif z_score >= ERROR_ZSCORE_MODERATE:
        strength = EvidenceStrength.MODERATE
    else:
        strength = EvidenceStrength.WEAK
    return EvidenceItem(
        signal="error_rate_spike",
        finding=f"{node_id} error rate {current_pct:.1f}% vs baseline {baseline_pct:.1f}% (z={z_score:.1f})",
        strength=strength,
        metadata={
            "node_id": node_id,
            "current_pct": str(current_pct),
            "baseline_pct": str(baseline_pct),
            "z_score": str(z_score),
        },
    )


def make_latency_spike_evidence(
    node_id: str,
    current_ms: float,
    baseline_ms: float,
    z_score: float,
    metric_label: str,
) -> EvidenceItem:
    finding = f"{node_id} {metric_label} latency {current_ms:.0f}ms vs baseline {baseline_ms:.0f}ms (z={z_score:.1f})"
    if baseline_ms <= 0:
        strength = EvidenceStrength.MODERATE
        finding += " (baseline unavailable — comparison unreliable)"
    elif current_ms >= baseline_ms * LATENCY_STRONG_MULTIPLIER:
        strength = EvidenceStrength.STRONG
    elif current_ms >= baseline_ms * LATENCY_MODERATE_MULTIPLIER:
        strength = EvidenceStrength.MODERATE
    else:
        strength = EvidenceStrength.WEAK
    return EvidenceItem(
        signal="latency_spike",
        finding=finding,
        strength=strength,
        metadata={
            "node_id": node_id,
            "current_ms": str(current_ms),
            "baseline_ms": str(baseline_ms),
            "z_score": str(z_score),
            "metric_label": metric_label,
        },
    )


def make_stacktrace_evidence(
    function_name: str,
    file_path: str,
    line_number: int,
    match_type: str,
) -> EvidenceItem:
    if match_type == "exact_line":
        strength = EvidenceStrength.STRONG
    elif match_type == "function_match":
        strength = EvidenceStrength.MODERATE
    else:
        strength = EvidenceStrength.WEAK
    return EvidenceItem(
        signal="stacktrace_match",
        finding=f"exception traceback matches {function_name} at {file_path}:{line_number} (match: {match_type})",
        strength=strength,
        metadata={
            "function_name": function_name,
            "file_path": file_path,
            "line_number": str(line_number),
            "match_type": match_type,
        },
    )


def make_code_change_evidence(
    function_name: str,
    file_path: str,
    change_type: str,
    version: str,
) -> EvidenceItem:
    return EvidenceItem(
        signal="code_change_in_window",
        finding=f"{change_type} to {function_name} in {file_path} introduced in {version}",
        strength=EvidenceStrength.MODERATE,
        metadata={
            "function_name": function_name,
            "file_path": file_path,
            "change_type": change_type,
            "version": version,
        },
    )


def make_exception_evidence(
    exception_type: str,
    exception_message: str,
    service_id: str,
) -> EvidenceItem:
    return EvidenceItem(
        signal="exception_in_spans",
        finding=f"{service_id} throwing {exception_type}: {exception_message[:120]}",
        strength=EvidenceStrength.MODERATE,
        metadata={
            "exception_type": exception_type,
            "service_id": service_id,
            "exception_message": exception_message,
        },
    )


def make_slow_query_evidence(
    node_id: str,
    query_preview: str,
    mean_duration_ms: float,
    baseline_ms: float,
    pattern: str,
) -> EvidenceItem:
    finding = (
        f"{node_id} slow query ({pattern}): {mean_duration_ms:.0f}ms vs baseline "
        f"{baseline_ms:.0f}ms — {query_preview[:80]}"
    )
    if baseline_ms <= 0:
        strength = EvidenceStrength.MODERATE
        finding += " (baseline unavailable — comparison unreliable)"
    else:
        strength = (
            EvidenceStrength.STRONG
            if mean_duration_ms >= baseline_ms * SLOW_QUERY_STRONG_MULTIPLIER
            else EvidenceStrength.MODERATE
        )
    return EvidenceItem(
        signal="slow_query",
        finding=finding,
        strength=strength,
        metadata={
            "node_id": node_id,
            "pattern": pattern,
            "mean_duration_ms": str(mean_duration_ms),
            "baseline_ms": str(baseline_ms),
            "query_preview": query_preview[:80],
        },
    )


def make_connection_exhaustion_evidence(
    node_id: str,
    failed_connection_count: int,
    span_count: int,
) -> EvidenceItem:
    if span_count <= 0:
        return EvidenceItem(
            signal="connection_exhaustion",
            finding=f"{node_id} connection health unknown — no span data available",
            strength=EvidenceStrength.WEAK,
            metadata={
                "node_id": node_id,
                "failed_connection_count": "0",
                "span_count": "0",
            },
        )
    strength = (
        EvidenceStrength.STRONG
        if failed_connection_count / span_count >= CONN_EXHAUSTION_STRONG_FRAC
        else EvidenceStrength.MODERATE
    )
    return EvidenceItem(
        signal="connection_exhaustion",
        finding=(
            f"{node_id} connection exhaustion: {failed_connection_count} of "
            f"{span_count} spans failed to connect"
        ),
        strength=strength,
        metadata={
            "node_id": node_id,
            "failed_connection_count": str(failed_connection_count),
            "span_count": str(span_count),
        },
    )


def make_http_error_evidence(
    node_id: str,
    status_code: int,
    error_count: int,
    total_count: int,
    duration_minutes: float,
) -> EvidenceItem:
    if total_count <= 0:
        return EvidenceItem(
            signal="http_error_pattern",
            finding=f"{node_id} HTTP error assessment unavailable — no request data",
            strength=EvidenceStrength.WEAK,
            metadata={
                "node_id": node_id,
                "status_code": str(status_code),
                "error_count": "0",
                "total_count": "0",
                "duration_minutes": str(duration_minutes),
            },
        )
    strength = (
        EvidenceStrength.STRONG
        if error_count / total_count >= HTTP_ERROR_STRONG_FRAC and duration_minutes >= HTTP_ERROR_STRONG_DURATION
        else EvidenceStrength.MODERATE
    )
    return EvidenceItem(
        signal="http_error_pattern",
        finding=(
            f"{node_id} returning HTTP {status_code} on {error_count}/{total_count} "
            f"requests over {duration_minutes:.0f} minutes"
        ),
        strength=strength,
        metadata={
            "node_id": node_id,
            "status_code": str(status_code),
            "error_count": str(error_count),
            "total_count": str(total_count),
            "duration_minutes": str(duration_minutes),
        },
    )


def make_caller_scope_evidence(
    dep_node_id: str,
    affected_callers: int,
    total_callers: int,
) -> EvidenceItem:
    if total_callers <= 0:
        return EvidenceItem(
            signal="all_callers_affected",
            finding=f"caller scope for {dep_node_id} unknown — no caller data available",
            strength=EvidenceStrength.WEAK,
            metadata={
                "dep_node_id": dep_node_id,
                "affected_callers": "0",
                "total_callers": "0",
            },
        )
    strength = (
        EvidenceStrength.STRONG
        if affected_callers == total_callers
        else EvidenceStrength.MODERATE
    )
    scope = "full" if affected_callers == total_callers else "partial"
    return EvidenceItem(
        signal="all_callers_affected",
        finding=(
            f"{affected_callers} of {total_callers} callers of {dep_node_id} "
            f"affected — {scope} blast radius"
        ),
        strength=strength,
        metadata={
            "dep_node_id": dep_node_id,
            "affected_callers": str(affected_callers),
            "total_callers": str(total_callers),
        },
    )


def make_feature_flag_evidence(
    flag_key: str,
    old_value: str,
    new_value: str,
    minutes_before: float,
    service_id: str,
) -> EvidenceItem:
    if minutes_before <= CONFIG_STRONG_MINUTES:
        strength = EvidenceStrength.STRONG
    elif minutes_before <= CONFIG_MODERATE_MINUTES:
        strength = EvidenceStrength.MODERATE
    else:
        strength = EvidenceStrength.WEAK
    return EvidenceItem(
        signal="feature_flag_change",
        finding=(
            f"feature flag '{flag_key}' changed from '{old_value}' to '{new_value}' "
            f"on {service_id}, {minutes_before:.0f} minutes before incident"
        ),
        strength=strength,
        metadata={
            "flag_key": flag_key,
            "old_value": old_value,
            "new_value": new_value,
            "service_id": service_id,
            "minutes_before": str(minutes_before),
        },
    )


def make_config_change_evidence(
    config_key: str,
    old_value: str,
    new_value: str,
    minutes_before: float,
    service_id: str,
) -> EvidenceItem:
    if minutes_before <= CONFIG_STRONG_MINUTES:
        strength = EvidenceStrength.STRONG
    elif minutes_before <= CONFIG_MODERATE_MINUTES:
        strength = EvidenceStrength.MODERATE
    else:
        strength = EvidenceStrength.WEAK
    return EvidenceItem(
        signal="config_change",
        finding=(
            f"config '{config_key}' changed from '{old_value}' to '{new_value}' "
            f"on {service_id}, {minutes_before:.0f} minutes before incident"
        ),
        strength=strength,
        metadata={
            "config_key": config_key,
            "old_value": old_value,
            "new_value": new_value,
            "service_id": service_id,
            "minutes_before": str(minutes_before),
        },
    )


def make_missing_telemetry_evidence(
    node_id: str,
    span_count: int,
    has_metrics: bool,
) -> EvidenceItem:
    return EvidenceItem(
        signal="missing_telemetry",
        finding=(
            f"{node_id} has limited observability: {span_count} spans, "
            f"{'no' if not has_metrics else 'some'} metrics — analysis may be incomplete"
        ),
        strength=EvidenceStrength.MODERATE,
        metadata={
            "node_id": node_id,
            "span_count": str(span_count),
            "has_metrics": "true" if has_metrics else "false",
        },
    )


def make_caller_timeout_evidence(
    node_id: str,
    caller_node_id: str,
    timeout_exception_type: str,
) -> EvidenceItem:
    return EvidenceItem(
        signal="caller_timeout",
        finding=(
            f"{caller_node_id} spans carry {timeout_exception_type} "
            f"on calls to {node_id}"
        ),
        strength=EvidenceStrength.MODERATE,
        metadata={
            "node_id": node_id,
            "caller_node_id": caller_node_id,
            "timeout_exception_type": timeout_exception_type,
        },
    )


def make_db_statement_evidence(
    node_id: str,
    statement: str,
    db_system: str,
) -> EvidenceItem:
    return EvidenceItem(
        signal="db_statement",
        finding=f"{node_id} ({db_system}) executing: {statement[:120]}",
        strength=EvidenceStrength.WEAK,
        metadata={
            "node_id": node_id,
            "db_system": db_system,
            "statement": statement[:120],
        },
    )


def make_db_error_evidence(
    node_id: str,
    exception_type: str,
    exception_message: str,
) -> EvidenceItem:
    return EvidenceItem(
        signal="db_integrity_error",
        finding=f"{node_id} raising {exception_type}: {exception_message[:120]}",
        strength=EvidenceStrength.STRONG,
        metadata={
            "node_id": node_id,
            "exception_type": exception_type,
            "exception_message": exception_message[:120],
        },
    )


def make_silent_degradation_evidence(
    dep_node_id: str,
    caller_node_id: str,
    success_rate_pct: float,
) -> EvidenceItem:
    return EvidenceItem(
        signal="silent_degradation",
        finding=(
            f"{dep_node_id} returning HTTP 200 to {caller_node_id} but caller is erroring "
            f"— possible malformed responses ({success_rate_pct:.0f}% success rate on dep spans)"
        ),
        strength=EvidenceStrength.MODERATE,
        metadata={
            "dep_node_id": dep_node_id,
            "caller_node_id": caller_node_id,
            "success_rate_pct": str(success_rate_pct),
        },
    )
