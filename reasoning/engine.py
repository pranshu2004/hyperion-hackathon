"""
reasoning/engine.py — Orchestrator for the reasoning pipeline.

Owns the reasoning loop. Calls each stage in order, collects hints,
decides whether to iterate, and produces the final RCAResult.

Contains no scoring logic, no domain analysis, no causal modeling.
Only orchestrates.

Constants:
    MAX_ITERATIONS: int = 3
    CONFIDENCE_THRESHOLD: float = 0.70
    MULTI_FACTOR_THRESHOLD: float = 0.65
"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime
from typing import Any

import networkx as nx

from context.baseline_calculator import BaselineStats, DeviationResult
from core.code_change import CodeChangeEvent
from core.metric import Metric, MetricType
from core.span import Span
from reasoning import causal_model, evidence_arbitrator, localizer, multi_factor
from reasoning import scorer
from reasoning.contracts import (
    ArbiterResult,
    DataQuality,
    EnrichedCandidate,
    InvestigationHint,
    RCAResult,
    RCAVerdict,
    ScoredCandidate,
)
from reasoning.domain_rca import investigate_all as domain_rca_investigate_all

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_ITERATIONS: int = 3
CONFIDENCE_THRESHOLD: float = 0.70
MULTI_FACTOR_THRESHOLD: float = 0.65


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze(
    graph: nx.DiGraph,
    spans: list[Span],
    metrics: list[Metric],
    baselines: dict[tuple[str, MetricType], BaselineStats],
    anomalous_nodes: list[DeviationResult],
    node_timelines: dict,
    incident_start: datetime,
    incident_id: str = "",
    incident_end: datetime | None = None,
    code_change_event: CodeChangeEvent | None = None,
    llm_client: Any | None = None,
    top_k: int = 5,
) -> RCAResult:
    """
    Orchestrate the reasoning pipeline and return a structured RCAResult.

    The loop runs up to MAX_ITERATIONS. It terminates early when no new
    InvestigationHints are surfaced in the last iteration.

    Parameters node_timelines and incident_end are reserved for future use
    and are not consumed by the current implementation. node_timelines is
    accepted for API symmetry with the original reasoning/ pipeline.
    incident_end will be used in V2 for bounded incident window analysis.

    Known limitation: code_change_event accepts a single CodeChangeEvent
    for the entire incident run. Case B stacktrace matching in service.py
    is limited to this one event. Real incidents may involve multiple
    services deploying within the lookback window.
    V2: change to code_change_events: list[CodeChangeEvent] and let the
    domain RCA orchestrator filter by service_id and deploy_event_id.
    The list approach is preferred over dict[str, list[CodeChangeEvent]]
    because filtering belongs in the domain engine, not the caller.
    """
    try:
        return _run_analysis(
            graph=graph,
            spans=spans,
            metrics=metrics,
            baselines=baselines,
            anomalous_nodes=anomalous_nodes,
            incident_start=incident_start,
            incident_id=incident_id,
            code_change_event=code_change_event,
            llm_client=llm_client,
            top_k=top_k,
        )
    except Exception:
        logger.error(
            "analyze: unhandled exception in reasoning loop:\n%s",
            traceback.format_exc(),
        )
        return RCAResult(
            incident_id=incident_id,
            verdict=RCAVerdict.NO_ROOT_CAUSE_FOUND,
            root_causes=[],
            narrative="Analysis failed due to internal error",
            all_candidates=[],
            iterations_run=0,
            analyzed_at=datetime.utcnow(),
            data_quality=DataQuality.LOW,
        )


# ---------------------------------------------------------------------------
# Inner loop (called from analyze; allows clean try/except at top level)
# ---------------------------------------------------------------------------

def _run_analysis(
    graph: nx.DiGraph,
    spans: list[Span],
    metrics: list[Metric],
    baselines: dict[tuple[str, MetricType], BaselineStats],
    anomalous_nodes: list[DeviationResult],
    incident_start: datetime,
    incident_id: str,
    code_change_event: CodeChangeEvent | None,
    llm_client: Any | None,
    top_k: int,
) -> RCAResult:
    hint_pool: list[InvestigationHint] = []
    known_candidate_ids: set[str] = set()
    all_enriched: list[EnrichedCandidate] = []
    arbiter_result: ArbiterResult | None = None
    iterations_run = 0

    for iteration in range(1, MAX_ITERATIONS + 1):
        iterations_run = iteration

        # ------------------------------------------------------------------
        # Stage 1: Localize
        # ------------------------------------------------------------------
        candidates = localizer.localize(
            graph=graph,
            anomalous_nodes=anomalous_nodes,
            spans=spans,
            incident_start=incident_start,
            hints=hint_pool,
            known_candidate_ids=known_candidate_ids,
        )

        if not candidates:
            logger.debug("analyze: iteration %d — no candidates, stopping", iteration)
            break

        # ------------------------------------------------------------------
        # Stage 2: Rank candidates
        # ------------------------------------------------------------------
        ranked = causal_model.rank_candidates(
            candidates=candidates,
            graph=graph,
            spans=spans,
            metrics=metrics,
            baselines=baselines,
            incident_start=incident_start,
            top_k=top_k,
        )

        if not ranked:
            logger.debug(
                "analyze: iteration %d — rank_candidates returned empty, stopping",
                iteration,
            )
            break

        # ------------------------------------------------------------------
        # Stage 3: Domain RCA + Neighbour Scan
        # Orchestrated by domain_rca.orchestrator.investigate_all().
        # Dispatch by node type, neighbour scan per candidate, and error
        # handling are all owned by the orchestrator.
        # ------------------------------------------------------------------
        current_batch_ids: set[str] = {r.candidate.node_id for r in ranked}

        domain_result = domain_rca_investigate_all(
            ranked_candidates=ranked,
            graph=graph,
            spans=spans,
            metrics=metrics,
            baselines=baselines,
            incident_start=incident_start,
            code_change_event=code_change_event,
            llm_client=llm_client,
            already_investigated=known_candidate_ids | current_batch_ids,
            iteration=iteration,
        )
        iteration_enriched: list[EnrichedCandidate] = domain_result.candidates
        iteration_hints: list[InvestigationHint] = domain_result.hints

        # ------------------------------------------------------------------
        # Stage 3c: Evidence Arbitrator (after all candidates this iteration)
        # ------------------------------------------------------------------
        if iteration_enriched:
            arbiter_result = evidence_arbitrator.arbitrate(
                candidates=iteration_enriched,
                graph=graph,
                incident_start=incident_start,
            )
            iteration_enriched = list(arbiter_result.adjusted_candidates)

        all_enriched.extend(iteration_enriched)

        # ------------------------------------------------------------------
        # Termination check
        # ------------------------------------------------------------------
        all_known_after = known_candidate_ids | current_batch_ids
        new_hints = [h for h in iteration_hints if h.node_id not in all_known_after]

        known_candidate_ids = all_known_after

        if not new_hints or iteration == MAX_ITERATIONS:
            logger.debug(
                "analyze: stopping after iteration %d "
                "(new_hints=%d, at_max=%s)",
                iteration,
                len(new_hints),
                iteration == MAX_ITERATIONS,
            )
            break

        hint_pool = new_hints

    # ------------------------------------------------------------------
    # Stage 4: Score
    # ------------------------------------------------------------------
    data_quality = _assess_data_quality(anomalous_nodes, baselines)

    if not all_enriched:
        return RCAResult(
            incident_id=incident_id,
            verdict=RCAVerdict.NO_ROOT_CAUSE_FOUND,
            root_causes=[],
            narrative="",
            all_candidates=[],
            iterations_run=iterations_run,
            analyzed_at=datetime.utcnow(),
            data_quality=data_quality,
            weak_signals=[],
        )

    scored = scorer.score(candidates=all_enriched, graph=graph)

    # ------------------------------------------------------------------
    # Stage 5: Multi-Factor Engine (conditional)
    # ------------------------------------------------------------------
    multi_factor_flagged = arbiter_result.multi_factor_flagged if arbiter_result else False

    if multi_factor_flagged and arbiter_result is not None:
        scored = multi_factor.analyze(
            candidates=scored,
            graph=graph,
            arbiter_result=arbiter_result,
        )

    # ------------------------------------------------------------------
    # Verdict determination
    # ------------------------------------------------------------------
    verdict, root_causes, multi_factor_explanation = _determine_verdict(
        scored=scored,
        multi_factor_flagged=multi_factor_flagged,
    )

    weak_signals: list[str] = []
    if verdict == RCAVerdict.NO_ROOT_CAUSE_FOUND:
        weak_signals = [c.node_id for c in scored]

    narrative = ""
    fix_suggestion = ""
    if llm_client is not None and root_causes:
        try:
            from output.formatter import format_narrative_v2, generate_fix
            narrative = format_narrative_v2(
                verdict=verdict.value,
                root_causes=root_causes,
                incident_id=incident_id,
                llm_client=llm_client,
            )
            fix_suggestion = generate_fix(
                root_causes=root_causes,
                incident_id=incident_id,
                llm_client=llm_client,
            )
        except Exception as exc:
            logger.warning("narrative/fix generation failed: %s", exc)

    return RCAResult(
        incident_id=incident_id,
        verdict=verdict,
        root_causes=root_causes,
        narrative=narrative,
        all_candidates=scored,
        iterations_run=iterations_run,
        analyzed_at=datetime.utcnow(),
        data_quality=data_quality,
        multi_factor_explanation=multi_factor_explanation,
        weak_signals=weak_signals,
        fix_suggestion=fix_suggestion,
    )


# ---------------------------------------------------------------------------
# Verdict determination
# ---------------------------------------------------------------------------

def _determine_verdict(
    scored: list[ScoredCandidate],
    multi_factor_flagged: bool,
) -> tuple[RCAVerdict, list[ScoredCandidate], str | None]:
    if not scored:
        return RCAVerdict.NO_ROOT_CAUSE_FOUND, [], None

    top = scored[0]

    # Multi-factor: two candidates >= MULTI_FACTOR_THRESHOLD in different domains
    if multi_factor_flagged:
        qualifying: list[ScoredCandidate] = []
        domains_seen: set = set()
        for c in scored:
            if (
                c.domain_confidence >= MULTI_FACTOR_THRESHOLD
                and c.domain not in domains_seen
            ):
                qualifying.append(c)
                domains_seen.add(c.domain)
            if len(qualifying) >= 2:
                break
        if len(qualifying) >= 2:
            explanation = (
                f"Multi-factor incident: {qualifying[0].domain.value} "
                f"({qualifying[0].node_id}) and {qualifying[1].domain.value} "
                f"({qualifying[1].node_id}) both have sufficient evidence"
            )
            return RCAVerdict.MULTI_FACTOR, qualifying[:2], explanation

    if top.domain_confidence >= CONFIDENCE_THRESHOLD:
        return RCAVerdict.ROOT_CAUSE_FOUND, [top], None

    if any(c.domain_confidence > 0 for c in scored):
        return RCAVerdict.TOP_HYPOTHESES, scored[:3], None

    return RCAVerdict.NO_ROOT_CAUSE_FOUND, [], None


# ---------------------------------------------------------------------------
# Data quality assessment
# ---------------------------------------------------------------------------

def _assess_data_quality(
    anomalous_nodes: list[DeviationResult],
    baselines: dict,
) -> DataQuality:
    """
    Assess telemetry coverage for anomalous nodes.

    - All anomalous nodes have baselines: HIGH
    - Some anomalous nodes have baselines: MEDIUM
    - No anomalous nodes have baselines (or none found): LOW
    """
    if not anomalous_nodes:
        return DataQuality.LOW

    nodes_with_baselines = sum(
        1 for d in anomalous_nodes
        if (d.node_id, d.metric_type) in baselines
    )

    if nodes_with_baselines == len(anomalous_nodes):
        return DataQuality.HIGH
    if nodes_with_baselines > 0:
        return DataQuality.MEDIUM
    return DataQuality.LOW
