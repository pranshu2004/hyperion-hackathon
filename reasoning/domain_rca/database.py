"""
Stage 3 Domain RCA for DatabaseNode candidates.
Stub implementation — emits basic metric evidence only.
Full implementation in V2: SQL parsing, query pattern analysis,
connection exhaustion detection, LLM SQL analysis.
"""

from __future__ import annotations

import logging
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone
from typing import Any

import networkx as nx

from context.baseline_calculator import BaselineStats
from core.metric import Metric, MetricType
from core.nodes import DatabaseNode
from core.span import Span
from reasoning.contracts import (
    EnrichedCandidate,
    EvidenceItem,
    EvidenceStrength,
    FailureType,
    RCADomain,
    RankedCandidate,
)
from reasoning.evidence_builders import (
    make_caller_timeout_evidence,
    make_connection_exhaustion_evidence,
    make_db_error_evidence,
    make_db_statement_evidence,
    make_error_rate_evidence,
    make_latency_spike_evidence,
    make_missing_telemetry_evidence,
    make_slow_query_evidence,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

SLOW_QUERY_DURATION_MS:      float = 100.0
INCIDENT_WINDOW_SECONDS:     int   = 300
DEPLOY_LOOKBACK_MINUTES:     int   = 30          # reserved — V2 deploy correlation
LATENCY_Z_SCORE_STRONG:      float = 3.0
LATENCY_Z_SCORE_MODERATE:    float = 1.0
MAX_CALLER_TIMEOUT_EVIDENCE: int   = 2

TIMEOUT_EXCEPTION_PATTERNS:   list[str] = ["Timeout", "ConnectionPool", "Exhausted"]
INTEGRITY_EXCEPTION_PATTERNS: list[str] = [
    "Constraint", "Unique", "ForeignKey", "Violation", "Deadlock",
]
RELATIONAL_DB_SYSTEMS: frozenset[str] = frozenset({"postgresql", "mysql", "mariadb"})
CACHE_DB_SYSTEMS:      frozenset[str] = frozenset({"redis", "memcached"})

PRIORITY_BOOST_STRONG:    float = 0.15
PRIORITY_BOOST_MODERATE:  float = 0.05
PRIORITY_PENALTY_MISSING: float = 0.10


def investigate(
    candidate: RankedCandidate,
    node: DatabaseNode,
    graph: nx.DiGraph,
    spans: list[Span],
    metrics: list[Metric],
    baselines: dict[tuple[str, MetricType], BaselineStats],
    incident_start: datetime,
    llm_client: Any | None,
    iteration: int,
) -> EnrichedCandidate:
    """
    Investigate a DatabaseNode candidate.

    Branches by db.system: relational (PostgreSQL/MySQL/MariaDB) →
    _investigate_relational(), cache (Redis/Memcached) → _investigate_cache(),
    unknown → _investigate_generic().

    node and llm_client are accepted for API symmetry. node is unused —
    db.system is read from spans (OTel authoritative source). llm_client
    is reserved for V2 SQL analysis.
    """
    node_id = candidate.candidate.node_id
    inc_start_utc = _to_utc(incident_start)
    window_end = inc_start_utc + timedelta(seconds=INCIDENT_WINDOW_SECONDS)

    try:
        error_spans = [
            s for s in spans
            if s.service_id == node_id
            and s.otel_status_code == 2
            and _in_window(s.start_time, inc_start_utc, window_end)
        ]
        db_spans = [
            s for s in spans
            if s.service_id == node_id
            and ("db.system" in s.attributes or "db.name" in s.attributes)
            and _in_window(s.start_time, inc_start_utc, window_end)
        ]

        db_system = _get_db_system(spans, node_id) or ""

        if db_system in RELATIONAL_DB_SYSTEMS:
            evidence, failure_type = _investigate_relational(
                node_id=node_id,
                db_spans=db_spans,
                error_spans=error_spans,
                all_spans=spans,
                metrics=metrics,
                baselines=baselines,
                graph=graph,
                inc_start_utc=inc_start_utc,
                window_end=window_end,
                db_system=db_system,
            )
        elif db_system in CACHE_DB_SYSTEMS:
            evidence, failure_type = _investigate_cache(
                node_id=node_id,
                error_spans=error_spans,
                all_spans=spans,
                metrics=metrics,
                baselines=baselines,
                inc_start_utc=inc_start_utc,
                window_end=window_end,
            )
        else:
            evidence, failure_type = _investigate_generic(
                node_id=node_id,
                error_spans=error_spans,
                all_spans=spans,
                metrics=metrics,
                baselines=baselines,
                inc_start_utc=inc_start_utc,
                window_end=window_end,
            )

        strong_count = sum(1 for e in evidence if e.strength == EvidenceStrength.STRONG)
        moderate_count = sum(1 for e in evidence if e.strength == EvidenceStrength.MODERATE)
        current_priority = candidate.candidate.priority_score

        if strong_count >= 1:
            new_priority = min(1.0, current_priority + PRIORITY_BOOST_STRONG)
        elif moderate_count >= 1:
            new_priority = min(1.0, current_priority + PRIORITY_BOOST_MODERATE)
        else:
            new_priority = max(0.0, current_priority - PRIORITY_PENALTY_MISSING)

        return EnrichedCandidate(
            node_id=node_id,
            node_type=candidate.candidate.node_type,
            domain=RCADomain.DATABASE,
            failure_type=failure_type,
            evidence=evidence,
            is_potential_origin=candidate.candidate.is_potential_origin,
            priority_score=new_priority,
            causal_confidence=candidate.causal_confidence,
            iteration_found=iteration,
        )

    except Exception:
        logger.exception("database investigation failed for %s", node_id)
        return EnrichedCandidate(
            node_id=node_id,
            node_type=candidate.candidate.node_type,
            domain=RCADomain.DATABASE,
            failure_type=FailureType.UNKNOWN,
            evidence=[make_missing_telemetry_evidence(
                node_id=node_id,
                span_count=0,
                has_metrics=False,
            )],
            is_potential_origin=candidate.candidate.is_potential_origin,
            priority_score=max(0.0, candidate.candidate.priority_score - PRIORITY_PENALTY_MISSING),
            causal_confidence=candidate.causal_confidence,
            iteration_found=iteration,
        )


# ---------------------------------------------------------------------------
# Sub-investigators
# ---------------------------------------------------------------------------

def _investigate_relational(
    node_id: str,
    db_spans: list[Span],
    error_spans: list[Span],
    all_spans: list[Span],
    metrics: list[Metric],
    baselines: dict[tuple[str, MetricType], BaselineStats],
    graph: nx.DiGraph,
    inc_start_utc: datetime,
    window_end: datetime,
    db_system: str,
) -> tuple[list[EvidenceItem], FailureType]:
    """
    Investigate a relational database (PostgreSQL, MySQL, MariaDB) candidate.

    Classification priority — first match wins:
      1. DB_ERROR               — integrity/constraint/deadlock exception on error spans
      2. DB_SLOW_QUERY          — latency z-score >= LATENCY_Z_SCORE_MODERATE or
                                  any DB span duration > SLOW_QUERY_DURATION_MS
      3. DB_CONNECTION_EXHAUSTION — error spans exist AND predecessors carry timeout
                                    exception types in their spans
      4. UNKNOWN                — fallback when evidence is insufficient

    Returns (evidence_items, failure_type).
    """
    # ------------------------------------------------------------------
    # Pre-compute — shared across classification and evidence emission
    # ------------------------------------------------------------------
    latency_baseline = baselines.get((node_id, MetricType.LATENCY_P99))
    current_samples = [
        m.value for m in metrics
        if m.node_id == node_id
        and m.metric_type == MetricType.LATENCY_P99
        and _in_window(m.timestamp, inc_start_utc, window_end)
    ]
    current_ms = sum(current_samples) / len(current_samples) if current_samples else 0.0
    baseline_ms = latency_baseline.mean if latency_baseline is not None else 0.0
    latency_z = (
        (current_ms - baseline_ms) / max(latency_baseline.std, 0.01)
        if latency_baseline is not None and current_samples
        else 0.0
    )

    all_node_spans = [
        s for s in all_spans
        if s.service_id == node_id
        and _in_window(s.start_time, inc_start_utc, window_end)
    ]
    total_spans = len(all_node_spans)
    error_pct = (len(error_spans) / total_spans * 100.0) if total_spans > 0 else 0.0
    error_baseline = baselines.get((node_id, MetricType.ERROR_RATE))
    baseline_error_pct = error_baseline.mean if error_baseline is not None else 0.0
    baseline_error_std = max(error_baseline.std, 0.01) if error_baseline is not None else 1.0
    error_z = (error_pct - baseline_error_pct) / baseline_error_std

    exc_counts = _get_exception_types(error_spans)
    timeout_signals = _get_predecessor_timeout_signals(
        graph, node_id, all_spans, inc_start_utc, window_end
    )

    # ------------------------------------------------------------------
    # Classify failure mechanism (priority order, first match wins)
    # ------------------------------------------------------------------
    failure_type = FailureType.UNKNOWN

    integrity_match: str | None = None
    for exc_type in sorted(exc_counts, key=lambda k: -exc_counts[k]):
        if any(p in exc_type for p in INTEGRITY_EXCEPTION_PATTERNS):
            integrity_match = exc_type
            break

    if integrity_match:
        failure_type = FailureType.DB_ERROR
    elif latency_z >= LATENCY_Z_SCORE_MODERATE or any(
        s.duration_ms > SLOW_QUERY_DURATION_MS for s in db_spans
    ):
        failure_type = FailureType.DB_SLOW_QUERY
    elif error_spans and timeout_signals:
        failure_type = FailureType.DB_CONNECTION_EXHAUSTION

    # ------------------------------------------------------------------
    # Emit evidence based on classification
    # ------------------------------------------------------------------
    evidence: list[EvidenceItem] = []

    if failure_type == FailureType.DB_ERROR:
        exc_message = ""
        for span in error_spans:
            for event in span.events:
                attrs = event.get("attributes", {})
                if attrs.get("exception.type") == integrity_match:
                    exc_message = str(attrs.get("exception.message", ""))
                    break
            if exc_message:
                break
        evidence.append(make_db_error_evidence(
            node_id=node_id,
            exception_type=integrity_match,
            exception_message=exc_message,
        ))
        evidence.append(make_error_rate_evidence(
            node_id=node_id,
            current_pct=error_pct,
            baseline_pct=baseline_error_pct,
            z_score=error_z,
        ))
        error_stmt = _get_error_db_statement(error_spans)
        if error_stmt is not None:
            evidence.append(make_db_statement_evidence(
                node_id=node_id,
                statement=error_stmt[0],
                db_system=db_system,
            ))

    elif failure_type == FailureType.DB_SLOW_QUERY:
        if current_samples:
            evidence.append(make_latency_spike_evidence(
                node_id=node_id,
                current_ms=current_ms,
                baseline_ms=baseline_ms,
                z_score=latency_z,
                metric_label="p99",
            ))
        slowest = _get_slowest_db_statement(db_spans)
        if slowest is not None:
            statement, avg_ms, _ = slowest
            evidence.append(make_slow_query_evidence(
                node_id=node_id,
                query_preview=statement,
                mean_duration_ms=avg_ms,
                baseline_ms=baseline_ms,
                pattern="high duration",
            ))
        if error_spans:
            evidence.append(make_error_rate_evidence(
                node_id=node_id,
                current_pct=error_pct,
                baseline_pct=baseline_error_pct,
                z_score=error_z,
            ))

    elif failure_type == FailureType.DB_CONNECTION_EXHAUSTION:
        evidence.append(make_connection_exhaustion_evidence(
            node_id=node_id,
            failed_connection_count=len(error_spans),
            span_count=len(db_spans) if db_spans else total_spans,
        ))
        for caller_id, exc_type in timeout_signals[:MAX_CALLER_TIMEOUT_EVIDENCE]:
            evidence.append(make_caller_timeout_evidence(
                node_id=node_id,
                caller_node_id=caller_id,
                timeout_exception_type=exc_type,
            ))
        evidence.append(make_error_rate_evidence(
            node_id=node_id,
            current_pct=error_pct,
            baseline_pct=baseline_error_pct,
            z_score=error_z,
        ))
        if current_samples and latency_z > 0.0:
            evidence.append(make_latency_spike_evidence(
                node_id=node_id,
                current_ms=current_ms,
                baseline_ms=baseline_ms,
                z_score=latency_z,
                metric_label="p99",
            ))

    else:
        evidence.append(make_missing_telemetry_evidence(
            node_id=node_id,
            span_count=total_spans,
            has_metrics=latency_baseline is not None,
        ))

    return evidence, failure_type


def _investigate_cache(
    node_id: str,
    error_spans: list[Span],
    all_spans: list[Span],
    metrics: list[Metric],
    baselines: dict[tuple[str, MetricType], BaselineStats],
    inc_start_utc: datetime,
    window_end: datetime,
) -> tuple[list[EvidenceItem], FailureType]:
    """
    Investigate a cache database (Redis, Memcached) candidate.

    No statement analysis — cache DBs do not expose db.statement.
    Latency and error rate only.
    """
    latency_baseline = baselines.get((node_id, MetricType.LATENCY_P99))
    current_samples = [
        m.value for m in metrics
        if m.node_id == node_id
        and m.metric_type == MetricType.LATENCY_P99
        and _in_window(m.timestamp, inc_start_utc, window_end)
    ]
    current_ms = sum(current_samples) / len(current_samples) if current_samples else 0.0
    baseline_ms = latency_baseline.mean if latency_baseline is not None else 0.0

    all_node_spans = [
        s for s in all_spans
        if s.service_id == node_id
        and _in_window(s.start_time, inc_start_utc, window_end)
    ]
    total_spans = len(all_node_spans)

    evidence: list[EvidenceItem] = []
    has_latency = False
    has_errors = False

    if current_samples and latency_baseline is not None:
        latency_z = (current_ms - baseline_ms) / max(latency_baseline.std, 0.01)
        if latency_z >= LATENCY_Z_SCORE_MODERATE:
            evidence.append(make_latency_spike_evidence(
                node_id=node_id,
                current_ms=current_ms,
                baseline_ms=baseline_ms,
                z_score=latency_z,
                metric_label="p99",
            ))
            has_latency = True

    if error_spans:
        error_pct = (len(error_spans) / total_spans * 100.0) if total_spans > 0 else 100.0
        error_baseline = baselines.get((node_id, MetricType.ERROR_RATE))
        baseline_error_pct = error_baseline.mean if error_baseline is not None else 0.0
        baseline_error_std = max(error_baseline.std, 0.01) if error_baseline is not None else 1.0
        error_z = (error_pct - baseline_error_pct) / baseline_error_std
        evidence.append(make_error_rate_evidence(
            node_id=node_id,
            current_pct=error_pct,
            baseline_pct=baseline_error_pct,
            z_score=error_z,
        ))
        has_errors = True

    if not evidence:
        evidence.append(make_missing_telemetry_evidence(
            node_id=node_id,
            span_count=total_spans,
            has_metrics=latency_baseline is not None,
        ))

    if has_latency:
        failure_type = FailureType.DB_SLOW_QUERY
    elif has_errors:
        failure_type = FailureType.DB_ERROR
    else:
        failure_type = FailureType.UNKNOWN

    return evidence, failure_type


def _investigate_generic(
    node_id: str,
    error_spans: list[Span],
    all_spans: list[Span],
    metrics: list[Metric],
    baselines: dict[tuple[str, MetricType], BaselineStats],
    inc_start_utc: datetime,
    window_end: datetime,
) -> tuple[list[EvidenceItem], FailureType]:
    """
    Fallback investigation for database candidates with unknown db.system.

    Conservative: latency and error-rate evidence are both capped at MODERATE via
    dc_replace, so no single evidence item can assign STRONG for an unclassified DB
    type. FailureType is always UNKNOWN — specific classification without DB type
    knowledge is unreliable.

    # V2: per-DB-type strength ceilings once db.system is known.
    """
    latency_baseline = baselines.get((node_id, MetricType.LATENCY_P99))
    current_samples = [
        m.value for m in metrics
        if m.node_id == node_id
        and m.metric_type == MetricType.LATENCY_P99
        and _in_window(m.timestamp, inc_start_utc, window_end)
    ]
    current_ms = sum(current_samples) / len(current_samples) if current_samples else 0.0
    baseline_ms = latency_baseline.mean if latency_baseline is not None else 0.0

    all_node_spans = [
        s for s in all_spans
        if s.service_id == node_id
        and _in_window(s.start_time, inc_start_utc, window_end)
    ]
    total_spans = len(all_node_spans)

    evidence: list[EvidenceItem] = []

    if current_samples and latency_baseline is not None:
        latency_z = (current_ms - baseline_ms) / max(latency_baseline.std, 0.01)
        if latency_z >= LATENCY_Z_SCORE_MODERATE:
            ev = make_latency_spike_evidence(
                node_id=node_id,
                current_ms=current_ms,
                baseline_ms=baseline_ms,
                z_score=latency_z,
                metric_label="p99",
            )
            if ev.strength == EvidenceStrength.STRONG:
                ev = dc_replace(ev, strength=EvidenceStrength.MODERATE)
            evidence.append(ev)

    if error_spans:
        error_pct = (len(error_spans) / total_spans * 100.0) if total_spans > 0 else 100.0
        error_baseline = baselines.get((node_id, MetricType.ERROR_RATE))
        baseline_error_pct = error_baseline.mean if error_baseline is not None else 0.0
        baseline_error_std = max(error_baseline.std, 0.01) if error_baseline is not None else 1.0
        error_z = (error_pct - baseline_error_pct) / baseline_error_std
        err_ev = make_error_rate_evidence(
            node_id=node_id,
            current_pct=error_pct,
            baseline_pct=baseline_error_pct,
            z_score=error_z,
        )
        if err_ev.strength == EvidenceStrength.STRONG:
            err_ev = dc_replace(err_ev, strength=EvidenceStrength.MODERATE)
        evidence.append(err_ev)

    if not evidence:
        evidence.append(make_missing_telemetry_evidence(
            node_id=node_id,
            span_count=total_spans,
            has_metrics=latency_baseline is not None,
        ))

    return evidence, FailureType.UNKNOWN


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_exception_types(error_spans: list[Span]) -> dict[str, int]:
    """
    Extract exception.type → occurrence count from span events on error spans.

    OTel encodes exceptions as span events (span.events[i]["attributes"]
    ["exception.type"]), not as top-level span attributes. neighbour_scan
    checks error.type at the top level — this helper reads the richer
    event-level encoding that DB drivers typically use.

    Returns a frequency dict so callers can both pattern-match and count.
    """
    counts: dict[str, int] = {}
    for span in error_spans:
        for event in span.events:
            exc_type = event.get("attributes", {}).get("exception.type")
            if exc_type:
                key = str(exc_type)
                counts[key] = counts.get(key, 0) + 1
    return counts


def _get_slowest_db_statement(
    db_spans: list[Span],
) -> tuple[str, float, int] | None:
    """
    Return (statement, avg_duration_ms, count) for the db.statement with the
    highest average duration across all DB spans that carry it.

    Ranks by avg_duration_ms only — total time (avg × count) is not used as
    the ranking key because it conflates frequency with slowness.

    # V2: blend avg_duration with count so high-volume moderately-slow queries
    # are not invisible to this ranking.
    """
    buckets: dict[str, list[float]] = {}
    for span in db_spans:
        statement = span.attributes.get("db.statement")
        if statement:
            key = str(statement)
            buckets.setdefault(key, []).append(span.duration_ms)

    if not buckets:
        return None

    best = max(buckets.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
    durations = best[1]
    return best[0], sum(durations) / len(durations), len(durations)


def _get_error_db_statement(
    error_spans: list[Span],
) -> tuple[str, int] | None:
    """
    Return (statement, error_count) for the db.statement that appears most
    frequently across error spans.

    Frequency of erroring is the ranking key — the statement that consistently
    produces errors is more likely the root cause than a one-off failure.
    """
    counts: dict[str, int] = {}
    for span in error_spans:
        statement = span.attributes.get("db.statement")
        if statement:
            key = str(statement)
            counts[key] = counts.get(key, 0) + 1

    if not counts:
        return None

    best = max(counts.items(), key=lambda kv: kv[1])
    return best[0], best[1]


def _get_predecessor_timeout_signals(
    graph: nx.DiGraph,
    node_id: str,
    spans: list[Span],
    window_start: datetime,
    window_end: datetime,
) -> list[tuple[str, str]]:
    """
    Return (caller_node_id, exception_type) for each predecessor that has a
    timeout exception type in its spans during the incident window.

    At most one pair per predecessor — the most frequent matching exception
    type for that caller. Used to corroborate DB_CONNECTION_EXHAUSTION:
    callers seeing timeout exceptions while the DB has high error rate is
    strong evidence the DB is pool-exhausted or overloaded.
    """
    results: list[tuple[str, str]] = []

    for pred_id in graph.predecessors(node_id):
        pred_spans = [
            s for s in spans
            if s.service_id == pred_id
            and _in_window(s.start_time, window_start, window_end)
        ]
        if not pred_spans:
            continue

        exc_counts = _get_exception_types(pred_spans)

        best_match: str | None = None
        best_count = 0
        for exc_type, count in exc_counts.items():
            if any(pattern in exc_type for pattern in TIMEOUT_EXCEPTION_PATTERNS):
                if count > best_count:
                    best_match = exc_type
                    best_count = count

        if best_match is not None:
            results.append((pred_id, best_match))

    return results


def _get_db_system(spans: list[Span], node_id: str) -> str | None:
    """Return the lowercased db.system value from the first span that carries it."""
    for span in spans:
        if span.service_id == node_id:
            value = span.attributes.get("db.system")
            if value:
                return str(value).lower()
    return None


def _to_utc(dt: datetime) -> datetime:
    """Naive datetime → attach UTC. Aware datetime → convert to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _in_window(
    event_ts: datetime,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    """True if event_ts falls within [window_start, window_end]."""
    ts = _to_utc(event_ts)
    return window_start <= ts <= window_end
