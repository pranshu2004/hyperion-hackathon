"""
RCA output formatter for reasoning pipeline.

format_narrative_v2  — plain-English incident narrative via local LLM
generate_fix         — plain-English remediation recommendation via local LLM

Both functions use the same OpenAI-compatible llm_client as domain RCA and
degrade gracefully to "" when llm_client is None or the call fails.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def format_narrative_v2(
    verdict: str,
    root_causes: list,
    incident_id: str,
    llm_client: Any | None = None,
) -> str:
    """
    Generate a plain-English narrative for a reasoning RCAResult.
    Uses the OpenAI-compatible llm_client (same client as domain RCA).
    Returns "" if llm_client is None, root_causes is empty, or the call fails.
    Never raises.
    """
    if llm_client is None or not root_causes:
        return ""

    try:
        top = root_causes[0]
        evidence_lines = "\n".join(
            f"{i + 1}. {e.signal}: {e.finding} ({e.strength.value})"
            for i, e in enumerate(top.evidence)
        )
        prompt = (
            f"You are summarizing a root cause analysis report for a production incident.\n"
            f"Incident ID: {incident_id}\n"
            f"Verdict: {verdict}\n"
            f"Root cause: {top.node_id} ({top.node_type.value})\n"
            f"Domain: {top.domain.value}\n"
            f"Confidence: {top.domain_confidence:.0%}\n"
            f"Evidence:\n{evidence_lines}\n\n"
            f"Write a concise 3-5 sentence explanation in plain English with no markdown "
            f"and no bullet points. Explain what failed, why it failed, and what the "
            f"likely downstream impact was. Mention the confidence level. Do not make up "
            f"any information not present above."
        )
        response = llm_client.chat.completions.create(
            model=os.environ.get("HYPERION_LLM_MODEL", "qwen2.5-coder:7b"),
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=1000,
        )
        return response.choices[0].message.content.strip()

    except Exception as exc:
        logger.warning("format_narrative_v2 failed: %s", exc)
        return ""


def generate_fix(
    root_causes: list,
    incident_id: str,
    llm_client: Any | None = None,
) -> str:
    """
    Generate a plain-English fix recommendation from a list of ScoredCandidates.
    Uses the OpenAI-compatible llm_client (same client as domain RCA).
    Returns "" if llm_client is None, root_causes is empty, or the call fails.
    Never raises.
    """
    if llm_client is None or not root_causes:
        return ""

    try:
        top = root_causes[0]
        strong_evidence = [e for e in top.evidence if e.strength.value == "strong"]
        evidence_to_show = strong_evidence or top.evidence[:4]
        evidence_lines = "\n".join(
            f"{i + 1}. [{e.strength.value.upper()}] {e.signal}: {e.finding}"
            for i, e in enumerate(evidence_to_show)
        )
        prompt = (
            f"You are an on-call engineer responding to a production incident.\n"
            f"Incident ID: {incident_id}\n"
            f"Root cause: {top.node_id} ({top.node_type.value})\n"
            f"Domain: {top.domain.value}\n"
            f"Failure type: {top.failure_type.value}\n"
            f"Confidence: {top.domain_confidence:.0%}\n"
            f"Key evidence:\n{evidence_lines}\n\n"
            f"Based solely on the above, write a concise 2-4 sentence fix recommendation "
            f"in plain English with no markdown and no bullet points. Be specific and "
            f"actionable — tell the engineer exactly what to do to resolve this incident. "
            f"Do not invent information not present in the evidence above."
        )
        response = llm_client.chat.completions.create(
            model=os.environ.get("HYPERION_LLM_MODEL", "qwen2.5-coder:7b"),
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=1000,
        )
        return response.choices[0].message.content.strip()

    except Exception as exc:
        logger.warning("generate_fix failed: %s", exc)
        return ""
