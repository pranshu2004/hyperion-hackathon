"""
Stage 3c of the reasoning pipeline.
Cross-candidate coherence check. Runs after all Domain RCA
investigations in an iteration. Adjusts priority scores and
flags multi-factor condition.

Stub implementation — detects multi-factor condition by checking
if two candidates have strong evidence in different domains.
Full cross-candidate impact accounting is V2.
"""

from __future__ import annotations

from datetime import datetime

import networkx as nx

from reasoning.contracts import (
    ArbiterResult,
    EnrichedCandidate,
    EvidenceStrength,
)


def arbitrate(
    candidates: list[EnrichedCandidate],
    graph: nx.DiGraph,
    incident_start: datetime,
) -> ArbiterResult:
    """
    Check cross-candidate coherence and flag multi-factor conditions.

    Stub: finds candidates with STRONG evidence and checks whether two
    or more are in different domains. Priority scores are not adjusted.
    Full explaining-away and residual impact accounting are V2.
    """
    # TODO (V2): implement full cross-candidate coherence:
    # - Compute how much of observed impact each candidate explains
    # - Explaining away: if candidate A fully explains impact,
    #   downgrade candidate B priority
    # - Residual unexplained impact as genuine multi-factor signal
    # - Weight nodes by traffic volume and impact magnitude

    strong_candidates = [
        c for c in candidates
        if any(e.strength == EvidenceStrength.STRONG for e in c.evidence)
    ]

    multi_factor_flagged = False
    residual = 0.0
    explanation = "Single domain dominates"

    if len(strong_candidates) >= 2:
        domains = {c.domain for c in strong_candidates}
        if len(domains) >= 2:
            sorted_domains = sorted(d.value for d in domains)
            multi_factor_flagged = True
            residual = 0.5
            explanation = (
                f"Multi-factor condition detected: {sorted_domains[0]} and"
                f" {sorted_domains[1]} both have strong evidence"
            )

    return ArbiterResult(
        adjusted_candidates=list(candidates),
        multi_factor_flagged=multi_factor_flagged,
        residual_unexplained_impact=residual,
        explanation=explanation,
    )
