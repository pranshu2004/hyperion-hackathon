"""
Stage 5 of the reasoning pipeline.
Conditional — only runs when Evidence Arbitrator flags multi-factor.
Determines whether two candidates are genuinely independent failures
or whether one explains the other.

Stub implementation — confirms multi-factor when two candidates in
different domains both score above threshold. No impact scope
analysis, shared dependency check, or demotion logic.
"""

from __future__ import annotations

import networkx as nx

from reasoning.contracts import ArbiterResult, ScoredCandidate

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

MULTI_FACTOR_CONFIDENCE_THRESHOLD: float = 0.65


def analyze(
    candidates: list[ScoredCandidate],
    graph: nx.DiGraph,
    arbiter_result: ArbiterResult,
) -> list[ScoredCandidate]:
    """
    Determine whether flagged multi-factor candidates are genuinely independent.

    Stub: if arbiter flagged multi-factor and two candidates in different
    domains both score >= MULTI_FACTOR_CONFIDENCE_THRESHOLD, returns all
    candidates unchanged (multi-factor confirmed). Otherwise returns as-is.
    No demotion, shared dependency check, or change event overlap analysis.
    """
    # TODO (V2): implement full multi-factor analysis:
    # - Impact scope independence: do candidates explain disjoint requests?
    # - Shared dependency check: common upstream cause?
    # - Change event overlap: single change affecting two domains?
    # - Demote secondary if one explains the other

    if not arbiter_result.multi_factor_flagged:
        return candidates

    domains_seen: set = set()
    qualifying: list[ScoredCandidate] = []

    for candidate in candidates:
        if (
            candidate.domain not in domains_seen
            and candidate.domain_confidence >= MULTI_FACTOR_CONFIDENCE_THRESHOLD
        ):
            qualifying.append(candidate)
            domains_seen.add(candidate.domain)
        if len(qualifying) >= 2:
            break

    return candidates
