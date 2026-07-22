"""
reasoning/causal_model.py — Stage 2 of the reasoning pipeline.

PURPOSE
    Prioritisation filter. Takes the flat unranked candidate list from the
    Localizer and produces a ranked list of RankedCandidate objects sorted by
    causal_confidence descending. The top K candidates are forwarded to Domain
    RCA.

CAUSAL CONFIDENCE IS USED FOR TWO THINGS ONLY
    1. Ranking candidates for Domain RCA investigation order.
    2. Stored on RankedCandidate for frontend display alongside domain_confidence.

    It is NEVER used for final RCA confidence scoring. The Scorer owns that
    entirely. causal_confidence from this stage is preserved separately on each
    candidate and must not influence any domain scorer.

CORE ALGORITHMS
    Ported from reasoning/causal_model.py:
      _to_utc                 — centralised timezone normalisation
      _pearson                — Pearson correlation with min-sample guard
      _build_edge_weight_map  — per-edge propagation weight via Pearson corr
      _build_observed_impacts — per-node observed error rate + latency from metrics
      simulate_forward        — BFS forward simulation via predecessors()
      counterfactual          — removes candidate and measures residual explanation

    Differences from reasoning/causal_model.py:
      - No class wrapper: single public function rank_candidates().
      - failure_severity derived internally per candidate; localizer no longer
        provides it.
      - CAUSAL_ALPHA and CAUSAL_BETA are module-level constants, not instance
        params.
      - No EvidencePack/EvidenceItem — output is RankedCandidate only.
      - _build_observed_impacts is the single source of truth; localizer no
        longer duplicates this logic.

TODO (V2): split into separate modules:
  edge_weights.py       — Pearson/coupling/propagation weight estimation
  observed_impact.py    — incident-window impact extraction
  forward_simulation.py — predicted impact propagation
  counterfactual.py     — graph counterfactual heuristic
  causal_scoring.py     — fit score, severity, confidence composition
  causal_types.py       — internal Stage 2 dataclasses
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import networkx as nx
from scipy.stats import pearsonr

from context.baseline_calculator import BaselineStats
from core.metric import Metric, MetricType
from core.span import Span
from reasoning.contracts import CandidateNode, RankedCandidate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public algorithm weights
# ---------------------------------------------------------------------------

CAUSAL_ALPHA: float = 0.6  # weight for fit_score
CAUSAL_BETA: float  = 0.4  # weight for counterfactual.confidence

# ---------------------------------------------------------------------------
# Pearson correlation constants
# ---------------------------------------------------------------------------

PEARSON_MIN_SAMPLES: int = 3
# PEARSON_FALLBACK = 0.5: moderate coupling assumed under data sparsity.
# Conservative — neither rules out nor strongly asserts propagation.
PEARSON_FALLBACK: float = 0.5
PEARSON_BUCKET_SECONDS: int = 60

# ---------------------------------------------------------------------------
# Edge weight composition constants
# ---------------------------------------------------------------------------

EDGE_WEIGHT_ERROR_COUPLING: float   = 0.4
EDGE_WEIGHT_LATENCY_COUPLING: float = 0.4
EDGE_WEIGHT_CALL_VOLUME: float      = 0.2
CALL_VOLUME_NORMALIZATION: float    = 1000.0
# DEFAULT_CALL_VOLUME: low-traffic default when request-rate data is missing.
# Produces low volume contribution to propagation_weight as expected for
# unknown traffic.
DEFAULT_CALL_VOLUME: float = 10.0
# EDGE_WEIGHT_FALLBACK: used when no metric data exists for an edge.
# Moderate assumption: neither rules out nor asserts propagation.
EDGE_WEIGHT_FALLBACK: float = 0.5

# ---------------------------------------------------------------------------
# Forward simulation constants
# ---------------------------------------------------------------------------

LATENCY_SEVERITY_FACTOR: float = 5.0
FIT_SCORE_ERROR_WEIGHT: float  = 0.7
FIT_SCORE_LATENCY_WEIGHT: float = 0.3

# ---------------------------------------------------------------------------
# Observed impact / counterfactual constants
# ---------------------------------------------------------------------------

OBSERVED_IMPACT_LOOKBACK_SECONDS: int = 300
ANOMALY_ERROR_RATE_MULTIPLIER: float  = 2.0
ANOMALY_MIN_ERROR_RATE_PCT: float     = 1.0
ANOMALY_LATENCY_MULTIPLIER: float     = 2.0
COUNTERFACTUAL_NECESSITY_THRESHOLD: float = 0.25

# Fallback severity for latency-only anomalies with no observed error rate.
# Set above median to avoid underweighting latency incidents.
# V2: derive from latency multiplier magnitude.
LATENCY_ONLY_SEVERITY_FALLBACK: float = 0.7


# ---------------------------------------------------------------------------
# Internal dataclasses (intermediate state; not exported to contracts.py)
# ---------------------------------------------------------------------------

@dataclass
class PredictedNodeImpact:
    node_id: str
    predicted_error_rate: float          # percentage [0.0–100.0]
    predicted_latency_multiplier: float  # ratio [1.0+]; 1.0 = baseline
    hop_distance: int
    confidence: float


@dataclass
class PredictedSystemImpact:
    root_candidate_id: str
    failure_severity: float
    node_impacts: list[PredictedNodeImpact]
    propagation_paths: list[list[str]]


@dataclass
class ObservedImpact:
    node_id: str
    observed_error_rate: float           # percentage [0.0–100.0]
    observed_latency_multiplier: float   # ratio [1.0+]; 1.0 = baseline
    baseline_error_rate: float           # percentage [0.0–100.0]
    baseline_latency: float | None       # milliseconds; None when no pre-incident baseline data


@dataclass
class CounterfactualResult:
    candidate_id: str
    is_causally_necessary: bool
    residual_explanation: float
    confidence: float


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rank_candidates(
    candidates: list[CandidateNode],
    graph: nx.DiGraph,
    spans: list[Span],
    metrics: list[Metric],
    baselines: dict[tuple[str, MetricType], BaselineStats],
    incident_start: datetime,
    top_k: int = 5,
) -> list[RankedCandidate]:
    """
    Rank candidates by causal_confidence and return the top K.

    causal_confidence = CAUSAL_ALPHA * fit_score + CAUSAL_BETA * counterfactual.confidence

    The returned list is sorted descending by causal_confidence. Length is
    min(top_k, len(candidates)). An empty candidate list returns [].
    """
    assert nx.is_directed_acyclic_graph(graph), \
        "graph must be a DAG — do not add reverse edges"

    if not candidates:
        return []

    edge_weight_map = _build_edge_weight_map(graph, spans, metrics, incident_start)
    observed_impacts = _build_observed_impacts(
        candidates, spans, metrics, baselines, incident_start
    )

    ranked: list[RankedCandidate] = []
    for candidate in candidates:
        failure_severity = _derive_failure_severity(candidate, observed_impacts)

        predicted = simulate_forward(
            candidate.node_id, graph, edge_weight_map, failure_severity, observed_impacts
        )
        cf = counterfactual(candidate.node_id, graph, observed_impacts)
        fit = _compute_fit_score(predicted, observed_impacts)

        cc = min(1.0, max(0.0, CAUSAL_ALPHA * fit + CAUSAL_BETA * cf.confidence))

        logger.debug(
            "rank_candidates: candidate=%s fit=%.3f cf=%.3f causal_confidence=%.3f",
            candidate.node_id, fit, cf.confidence, cc,
        )

        ranked.append(RankedCandidate(candidate=candidate, causal_confidence=cc))

    ranked.sort(key=lambda r: r.causal_confidence, reverse=True)
    return ranked[:top_k]


# ---------------------------------------------------------------------------
# Algorithm implementations (ported from reasoning/causal_model.py)
# ---------------------------------------------------------------------------

def _to_utc(dt: datetime) -> datetime:
    """
    Normalise a datetime to UTC.
    - If timezone-aware: convert to UTC via astimezone()
    - If timezone-naive: assume UTC, attach timezone info
    Never raises.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _pearson(
    xs: list[tuple[datetime, float]],
    ys: list[tuple[datetime, float]],
    fallback: float = PEARSON_FALLBACK,
) -> float:
    """
    Pearson correlation between two (timestamp, value) time series.

    Aligns both series to PEARSON_BUCKET_SECONDS grid before correlating.
    Returns fallback when either series has fewer than PEARSON_MIN_SAMPLES
    points, when the aligned overlap is too small, or on any computation
    failure.

    Result is clipped to [0.0, 1.0] — positive only. Negative correlation
    is treated as no coupling (0.0) rather than anti-coupling.
    """
    if len(xs) < PEARSON_MIN_SAMPLES or len(ys) < PEARSON_MIN_SAMPLES:
        return fallback
    try:
        def bucket_key(ts: datetime) -> int:
            return int(ts.timestamp()) // PEARSON_BUCKET_SECONDS

        x_buckets: dict[int, list[float]] = {}
        for ts, val in xs:
            k = bucket_key(ts)
            x_buckets.setdefault(k, []).append(val)

        y_buckets: dict[int, list[float]] = {}
        for ts, val in ys:
            k = bucket_key(ts)
            y_buckets.setdefault(k, []).append(val)

        common_keys = sorted(set(x_buckets) & set(y_buckets))
        if len(common_keys) < PEARSON_MIN_SAMPLES:
            return fallback

        x_aligned = [sum(x_buckets[k]) / len(x_buckets[k]) for k in common_keys]
        y_aligned = [sum(y_buckets[k]) / len(y_buckets[k]) for k in common_keys]

        corr, _ = pearsonr(x_aligned, y_aligned)
        corr = float(corr)
        if corr != corr:  # NaN check
            return fallback
        return max(0.0, min(1.0, corr))
    except Exception:
        return fallback


def _build_edge_weight_map(
    graph: nx.DiGraph,
    spans: list[Span],
    metrics: list[Metric],
    incident_start: datetime,
) -> dict[tuple[str, str], float]:
    """
    Compute a propagation weight for every edge in the graph.

    Weight = error_coupling * 0.4 + latency_coupling * 0.4 + call_volume_norm * 0.2

    Both coupling terms are Pearson correlations clipped to [0, 1]. Call volume
    is normalised against CALL_VOLUME_NORMALIZATION and capped at 1.0.

    CAUSAL MODELING INVARIANT: only pre-incident metrics are used.
    Including incident-window data would leak the failure cascade into
    edge weights, making victims appear causally coupled to the root cause.

    spans is reserved for future span-based coupling enhancements; it is not
    consumed by the current implementation.

    TODO (V2): split into signal-specific edge weights for error rate,
    latency, and request rate propagation separately. Current blended
    weight misses cases where only one signal propagates (e.g. latency
    propagates but errors do not, or retry storms change request rate
    independently).
    """
    inc_start = _to_utc(incident_start)

    def _values(node_id: str, mt: MetricType) -> list[tuple[datetime, float]]:
        return [
            (_to_utc(m.timestamp), m.value)
            for m in metrics
            if m.node_id == node_id
            and m.metric_type == mt
            and _to_utc(m.timestamp) < inc_start
        ]

    result: dict[tuple[str, str], float] = {}
    for source_id, target_id in graph.edges():
        rr_vals = _values(source_id, MetricType.REQUEST_RATE)
        rr_floats = [v for _, v in rr_vals]
        call_volume = sum(rr_floats) / len(rr_floats) if rr_floats else DEFAULT_CALL_VOLUME

        src_err = _values(source_id, MetricType.ERROR_RATE)
        tgt_err = _values(target_id, MetricType.ERROR_RATE)
        error_coupling = _pearson(src_err, tgt_err)

        src_lat = _values(source_id, MetricType.LATENCY_P99)
        tgt_lat = _values(target_id, MetricType.LATENCY_P99)
        latency_coupling = _pearson(src_lat, tgt_lat)

        propagation_weight = float(min(1.0, max(0.0,
            EDGE_WEIGHT_ERROR_COUPLING   * error_coupling
            + EDGE_WEIGHT_LATENCY_COUPLING * latency_coupling
            + EDGE_WEIGHT_CALL_VOLUME      * min(1.0, call_volume / CALL_VOLUME_NORMALIZATION)
        )))

        result[(source_id, target_id)] = propagation_weight

    return result


def _build_observed_impacts(
    candidates: list[CandidateNode],
    spans: list[Span],
    metrics: list[Metric],
    baselines: dict[tuple[str, MetricType], BaselineStats],
    incident_start: datetime,
) -> list[ObservedImpact]:
    """
    Build ObservedImpact for every node that has metric data in the incident
    window [incident_start, incident_start + OBSERVED_IMPACT_LOOKBACK_SECONDS].

    Uses mean of LATENCY_P99 samples in the window. This is the mean of
    pre-aggregated p99 values, not p99 computed from raw request data.

    candidates and spans are reserved for candidate-scoped filtering in V2;
    they are not consumed by the current implementation.
    """
    inc_start_utc = _to_utc(incident_start)
    inc_end_utc   = inc_start_utc + timedelta(seconds=OBSERVED_IMPACT_LOOKBACK_SECONDS)

    post: dict[str, dict[MetricType, list[float]]] = defaultdict(lambda: defaultdict(list))
    for m in metrics:
        ts = _to_utc(m.timestamp)
        if inc_start_utc <= ts <= inc_end_utc:
            post[m.node_id][m.metric_type].append(m.value)

    result: list[ObservedImpact] = []
    for node_id, type_vals in post.items():
        err_vals = type_vals.get(MetricType.ERROR_RATE, [])
        lat_vals = type_vals.get(MetricType.LATENCY_P99, [])
        if not err_vals and not lat_vals:
            continue

        b_err = baselines.get((node_id, MetricType.ERROR_RATE))
        b_lat = baselines.get((node_id, MetricType.LATENCY_P99))

        baseline_err = b_err.mean if b_err else 0.0
        baseline_lat = b_lat.mean if b_lat else None
        observed_err = sum(err_vals) / len(err_vals) if err_vals else 0.0
        observed_lat = sum(lat_vals) / len(lat_vals) if lat_vals else baseline_lat
        observed_latency_multiplier = (
            observed_lat / baseline_lat
            if baseline_lat and baseline_lat > 0
            else 1.0   # no anomaly when baseline unknown
        )

        result.append(ObservedImpact(
            node_id=node_id,
            observed_error_rate=observed_err,
            observed_latency_multiplier=observed_latency_multiplier,
            baseline_error_rate=baseline_err,
            baseline_latency=baseline_lat,
        ))

    return result


def simulate_forward(
    candidate_id: str,
    graph: nx.DiGraph,
    edge_weight_map: dict[tuple[str, str], float],
    failure_severity: float,
    observed_impacts: list[ObservedImpact],
) -> PredictedSystemImpact:
    """
    Forward causal simulation from candidate_id via predecessors().

    Edges go A → B (A calls B). When B fails, its callers (predecessors) are
    impacted. BFS walks the predecessor chain; severity attenuates per hop.

    failure_severity: float in [0.0, 1.0] ratio — NOT a percentage.
    predicted_error_rate in PredictedNodeImpact is percentage [0.0–100.0].
    Hop confidence decay: max(0.1, 1.0 - hop * 0.15).

    observed_impacts is reserved for future simulation refinement; not consumed
    by the current implementation.

    MVP limitations:
    - Uses one failure_severity to predict both error rate and latency
      multiplier — assumes all failures produce coupled propagation.
    - Only models callee-to-caller propagation via predecessors(). Does
      not model caller-to-callee failure modes (bad payloads, overload).
    - BFS visited set suppresses alternate paths; multi-path impact is
      not combined.
    - Propagation paths enumerate all simple paths but predicted impact
      uses first-visited BFS path only — can be inconsistent.
    TODO (V2): split into separate error rate and latency SCMs.
    """
    node_impacts: list[PredictedNodeImpact] = []
    affected_nodes: set[str] = set()
    visited: set[str] = {candidate_id}

    # Candidate itself at hop 0
    node_impacts.append(PredictedNodeImpact(
        node_id=candidate_id,
        predicted_error_rate=failure_severity * 100.0,
        predicted_latency_multiplier=1.0 + failure_severity * LATENCY_SEVERITY_FACTOR,
        hop_distance=0,
        confidence=1.0,
    ))

    # BFS upstream via predecessors (callers are impacted by callee failure)
    queue: deque[tuple[str, int, float]] = deque([(candidate_id, 0, failure_severity)])
    while queue:
        current_id, depth, current_severity = queue.popleft()
        for pred_id in graph.predecessors(current_id):
            if pred_id in visited:
                continue
            visited.add(pred_id)
            affected_nodes.add(pred_id)

            # Edge pred_id → current_id: pred calls current
            weight = edge_weight_map.get((pred_id, current_id), EDGE_WEIGHT_FALLBACK)
            attenuated = current_severity * weight
            hop = depth + 1

            node_impacts.append(PredictedNodeImpact(
                node_id=pred_id,
                predicted_error_rate=attenuated * 100.0,
                predicted_latency_multiplier=1.0 + attenuated * LATENCY_SEVERITY_FACTOR,
                hop_distance=hop,
                confidence=max(0.1, 1.0 - hop * 0.15),
            ))
            queue.append((pred_id, hop, attenuated))

    # Propagation paths: candidate → each affected node via reversed graph
    propagation_paths: list[list[str]] = []
    reversed_view = graph.reverse(copy=False)
    for affected_id in affected_nodes:
        try:
            for path in nx.all_simple_paths(
                reversed_view, candidate_id, affected_id, cutoff=5
            ):
                propagation_paths.append(list(path))
        except (nx.NetworkXError, nx.NodeNotFound):
            pass

    return PredictedSystemImpact(
        root_candidate_id=candidate_id,
        failure_severity=failure_severity,
        node_impacts=node_impacts,
        propagation_paths=propagation_paths,
    )


def counterfactual(
    candidate_id: str,
    graph: nx.DiGraph,
    observed_impacts: list[ObservedImpact],
) -> CounterfactualResult:
    """
    Counterfactual query: would the incident have occurred without candidate?

    Algorithm:
    1. Find "true roots" — anomalous nodes with no anomalous callees in the
       original graph (they fail independently, not via cascade).
    2. Remove the candidate from a copy of the graph.
    3. BFS from surviving true roots via predecessors (callers) to find all
       remaining anomalous nodes the surviving roots still explain.
    4. residual_explanation = explained / total_anomalous_nodes.
       If the candidate was the only true root, removing it leaves 0
       explained → residual → 0.0 → causally necessary.

    Anomaly definition (dual threshold):
      error anomalous: observed_error_rate > baseline * 2.0 AND > 1%
      latency anomalous: observed_latency_multiplier > 2.0
    Note: this definition differs from baseline_calculator (z-score >= 3.0).

    Known limitations:
    - Assumes downstream telemetry is present and complete.
    - Misses errors swallowed by catch blocks.
    - Cannot detect root causes outside the graph.
    """
    anomalous_ids: set[str] = set()
    for o in observed_impacts:
        is_error_anomalous = (
            o.observed_error_rate > o.baseline_error_rate * ANOMALY_ERROR_RATE_MULTIPLIER
            and o.observed_error_rate > ANOMALY_MIN_ERROR_RATE_PCT
        )
        is_latency_anomalous = (
            o.baseline_latency is not None
            and o.observed_latency_multiplier > ANOMALY_LATENCY_MULTIPLIER
        )
        if is_error_anomalous or is_latency_anomalous:
            anomalous_ids.add(o.node_id)

    total_anomalous = len(anomalous_ids)

    if total_anomalous == 0:
        # No observed anomalies — we have no evidence to assess causal
        # necessity. Return confidence=0.0, not 1.0.
        return CounterfactualResult(
            candidate_id=candidate_id,
            is_causally_necessary=False,
            residual_explanation=0.0,
            confidence=0.0,
        )

    # Heuristic origin candidates: anomalous nodes with no anomalous
    # callees in the original graph. Not confirmed root causes —
    # may miss nodes with missing telemetry or out-of-graph failures.
    origin_candidates: set[str] = {
        node_id for node_id in anomalous_ids
        if not (set(graph.successors(node_id)) & anomalous_ids)
    }

    # Remove candidate from graph copy (never mutate original).
    graph_copy = graph.copy()
    if candidate_id in graph_copy:
        graph_copy.remove_node(candidate_id)

    remaining_anomalous = anomalous_ids - {candidate_id}
    surviving_roots = origin_candidates & remaining_anomalous

    # BFS outward from surviving roots via predecessors (callers impacted
    # by each root's failure) to find all explained nodes.
    explained_nodes: set[str] = set(surviving_roots)
    bfs: deque[str] = deque(surviving_roots)
    while bfs:
        current = bfs.popleft()
        for pred_id in graph_copy.predecessors(current):
            if pred_id in remaining_anomalous and pred_id not in explained_nodes:
                explained_nodes.add(pred_id)
                bfs.append(pred_id)

    residual_explanation = len(explained_nodes) / total_anomalous
    is_causally_necessary = residual_explanation < COUNTERFACTUAL_NECESSITY_THRESHOLD
    confidence = min(1.0, max(0.0, 1.0 - residual_explanation))

    return CounterfactualResult(
        candidate_id=candidate_id,
        is_causally_necessary=is_causally_necessary,
        residual_explanation=residual_explanation,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _compute_fit_score(
    predicted: PredictedSystemImpact,
    observed: list[ObservedImpact],
) -> float:
    """
    Compare predicted impact against observed impact per node.

    fit_score = 1 - mean(per-node weighted prediction error)
    Error = 70% error rate component + 30% latency component.
    Nodes present in observed but absent from predicted count as full error (1.0).
    Returns float in [0.0, 1.0].

    TODO (V2): weight nodes by traffic volume and impact magnitude.
    Currently all observed nodes count equally regardless of request
    volume or business criticality.
    """
    if not observed:
        return 0.5  # Sparse telemetry — neutral score, not penalised as zero evidence.

    predicted_map = {ni.node_id: ni for ni in predicted.node_impacts}
    errors: list[float] = []

    for obs in observed:
        pred = predicted_map.get(obs.node_id)
        if pred is None:
            errors.append(1.0)
        else:
            error_rate_error = abs(
                pred.predicted_error_rate - obs.observed_error_rate
            ) / max(1.0, obs.observed_error_rate)
            error_rate_error = min(1.0, error_rate_error)

            latency_error = abs(
                pred.predicted_latency_multiplier - obs.observed_latency_multiplier
            ) / max(1.0, obs.observed_latency_multiplier)
            latency_error = min(1.0, latency_error)

            node_error = (
                FIT_SCORE_ERROR_WEIGHT   * error_rate_error
                + FIT_SCORE_LATENCY_WEIGHT * latency_error
            )
            errors.append(min(1.0, max(0.0, node_error)))

    mean_error = sum(errors) / len(errors)
    return min(1.0, max(0.0, 1.0 - mean_error))


def _derive_failure_severity(
    candidate: CandidateNode,
    observed_impacts: list[ObservedImpact],
) -> float:
    """
    Derive failure_severity in [0.0, 1.0] for this candidate from its observed
    impact data. The localizer does not provide severity; it is computed here.

    Returns max(error_severity, latency_severity) so that whichever signal is
    stronger drives the simulation. Falls back to 0.5 (neutral prior) when
    both signals are absent or no observed data exists.
    """
    impact_map = {o.node_id: o for o in observed_impacts}
    obs = impact_map.get(candidate.node_id)

    if obs is None:
        return 0.5

    error_severity = (
        min(1.0, obs.observed_error_rate / 100.0)
        if obs.observed_error_rate > 0
        else 0.0
    )
    latency_severity = (
        min(1.0, (obs.observed_latency_multiplier - 1.0) / LATENCY_SEVERITY_FACTOR)
        if obs.observed_latency_multiplier > 1.0
        else 0.0
    )
    combined = max(error_severity, latency_severity)
    return combined if combined > 0.0 else 0.5
