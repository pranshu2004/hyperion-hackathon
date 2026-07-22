"""
Stage 4 of the reasoning pipeline.
Domain-specific scorers. Single source of truth for final
domain_confidence. causal_confidence never influences scoring.
"""

from __future__ import annotations

import networkx as nx

from reasoning.contracts import (
    EnrichedCandidate,
    EvidenceStrength,
    ScoredCandidate,
)

# ---------------------------------------------------------------------------
# Named constants — evidence weight per strength tier
# ---------------------------------------------------------------------------

STRONG_WEIGHT:       float = 0.35
MODERATE_WEIGHT:     float = 0.15
WEAK_WEIGHT:         float = 0.05
MAX_EVIDENCE_SCORE:  float = 0.95


def score(
    candidates: list[EnrichedCandidate],
    graph: nx.DiGraph,
) -> list[ScoredCandidate]:
    """
    Score enriched candidates and return them ranked by domain_confidence.

    Stub: sums evidence weights per strength tier, clips to MAX_EVIDENCE_SCORE.
    causal_confidence is carried through unchanged for display purposes only.
    propagation_path is empty — graph traversal is V2.
    """
    # TODO (V2): replace with domain-specific scorers:
    # - ApplicationCodeScorer: stacktrace match > deploy > exception
    # - DatabaseScorer: slow query > connection exhaustion > latency
    # - DependencyScorer: all callers affected > sustained errors
    # - ConfigScorer: sub-minute temporal correlation > blast radius match
    # Each domain scorer knows its own evidence ceiling tiers.

    scored: list[ScoredCandidate] = []

    for candidate in candidates:
        raw_score = 0.0
        for item in candidate.evidence:
            if item.strength == EvidenceStrength.STRONG:
                raw_score += STRONG_WEIGHT
            elif item.strength == EvidenceStrength.MODERATE:
                raw_score += MODERATE_WEIGHT
            elif item.strength == EvidenceStrength.WEAK:
                raw_score += WEAK_WEIGHT

        domain_confidence = min(raw_score, MAX_EVIDENCE_SCORE)

        scored.append(ScoredCandidate(
            node_id=candidate.node_id,
            node_type=candidate.node_type,
            domain=candidate.domain,
            failure_type=candidate.failure_type,
            evidence=candidate.evidence,
            domain_confidence=domain_confidence,
            causal_confidence=candidate.causal_confidence,
            rank=0,
            propagation_path=[],
            iteration_found=candidate.iteration_found,
            analysis_complete=True,
        ))

    scored.sort(key=lambda c: c.domain_confidence, reverse=True)

    for i, candidate in enumerate(scored):
        candidate.rank = i + 1

    return scored
