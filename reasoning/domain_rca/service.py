"""
reasoning/domain_rca/service.py

Domain RCA investigation for ServiceNode candidates. Called by the Domain RCA
engine for every ServiceNode in the top-K ranked candidates.

Two engines always run independently on every ServiceNode:
  - Code engine: deploys, exceptions, code changes (Case A and Case B)
  - Config engine: config changes and feature flag changes

Both engines run regardless of what signals exist. _arbitrate() merges their
output into the final EnrichedCandidate.

Scope: this file enriches exactly one ServiceNode candidate.
It does not emit InvestigationHints and does not call neighbour_scan.
Hint emission is the responsibility of the domain_rca orchestrator.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import networkx as nx

from context.baseline_calculator import BaselineStats
from core.change_event import ConfigChangeEvent, FeatureFlagChangeEvent
from core.code_change import (
    CodeChangeEvent,
    FunctionChange,
    MatchType,
    StacktraceFrame,
    StacktraceMatch,
)
from core.metric import Metric, MetricType
from core.nodes import ServiceNode
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
    make_config_change_evidence,
    make_deploy_evidence,
    make_exception_evidence,
    make_feature_flag_evidence,
    make_no_deploy_evidence,
    make_stacktrace_evidence,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

# Domain RCA uses stricter lookbacks than localizer/neighbour_scan because this
# stage is attributing evidence, not casting the initial candidate net.
# TODO: consider if the lookback period should be shared
DEPLOY_LOOKBACK_MINUTES: int = 30
CONFIG_LOOKBACK_MINUTES: int = 15

PRIORITY_BOOST_STRONG: float            = 0.20
PRIORITY_BOOST_MODERATE: float          = 0.10
PRIORITY_PENALTY_WEAK: float            = 0.15
PRIORITY_OVERRIDE_SMOKING_GUN: float    = 0.95

LLM_MAX_TOKENS: int    = 2000
LLM_MODEL_NAME = os.environ.get("HYPERION_LLM_MODEL", "qwen2.5-coder:7b")

EXACT_LINE_TOLERANCE: int     = 3
FUNCTION_MATCH_MIN_CONFIDENCE = 0.6

INCIDENT_WINDOW_SECONDS: int = 300

# ---------------------------------------------------------------------------
# Module-level LLM prompt constants
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_CASE_A = """\
You are an expert site reliability engineer performing root cause analysis.
You will be given a code change summary and an exception that occurred after
the change was deployed. Determine whether the code change likely caused the
exception.

Respond ONLY with valid JSON matching this exact schema:
{
  "causal_assessment": "<likely|unlikely|uncertain>",
  "reasoning": "<1-2 sentences explaining your assessment>",
  "signals": ["<specific signal 1>", "<specific signal 2>"]
}

Rules:
- causal_assessment must be exactly one of: likely, unlikely, uncertain
- Use "likely" only when you can identify a specific mechanism connecting
  the change to the exception
- Use "unlikely" when the change clearly could not produce this exception
- Use "uncertain" when there is insufficient information to assess
- reasoning must be 1-2 sentences maximum
- signals must list the specific technical signals that drove your assessment
- Do not include any text outside the JSON object"""

_FEW_SHOT_CASE_A = """\
Example 1:
Deploy diff summary: "removed null check on user_id before calling validate_transaction()"
Exception: NullPointerException: Cannot invoke method validate() on null object in validate_transaction()
Response: {"causal_assessment": "likely", "reasoning": "The removal of the null check on user_id directly exposes validate_transaction() to null input, which produces the observed NullPointerException.", "signals": ["null check removed", "exception in same function", "NullPointerException consistent with null input"]}

Example 2:
Deploy diff summary: "updated CSS styles for checkout button colour"
Exception: PSQLException: connection timeout after 30000ms
Response: {"causal_assessment": "unlikely", "reasoning": "A CSS styling change cannot affect database connection behaviour or timeout configuration.", "signals": ["CSS change unrelated to database", "no connection pool or timeout configuration changed"]}

Now assess the following:"""

_SYSTEM_PROMPT_CASE_B = """\
You are an expert site reliability engineer performing root cause analysis.
You will be given the old version and new version of a function that was
changed in a recent deploy, along with an exception that occurred in that
function after the deploy. Determine whether the code change caused the
exception.

Respond ONLY with valid JSON matching this exact schema:
{
  "causal_assessment": "<likely|unlikely|uncertain>",
  "reasoning": "<1-2 sentences explaining your assessment>",
  "signals": ["<specific signal 1>", "<specific signal 2>"]
}

Rules:
- causal_assessment must be exactly one of: likely, unlikely, uncertain
- Use "likely" only when you can identify the exact mechanism — the specific
  line or logic change that produces this exception
- Use "unlikely" when the change clearly could not produce this exception
- Use "uncertain" when the connection is plausible but not provable from
  the code alone
- reasoning must be 1-2 sentences maximum, citing specific line-level changes
- signals must list the specific code-level changes that drove your assessment
- Do not include any text outside the JSON object"""

_SYSTEM_PROMPT_CONFIG = """\
You are an expert site reliability engineer performing root cause analysis.
You will be given a configuration or feature flag change and an error pattern
observed in the service after the change. Determine whether the configuration
change likely caused the observed errors.

Respond ONLY with valid JSON matching this exact schema:
{
  "causal_assessment": "<likely|unlikely|uncertain>",
  "reasoning": "<1-2 sentences explaining your assessment>",
  "signals": ["<specific signal 1>", "<specific signal 2>"]
}

Rules:
- causal_assessment must be exactly one of: likely, unlikely, uncertain
- Use "likely" when you can identify a specific operational mechanism:
  a timeout reduction explaining timeouts, a flag enabling untested code paths,
  a connection pool reduction explaining exhaustion, etc.
- Use "unlikely" when the change clearly could not produce these errors
- Use "uncertain" when the flag name or config key is too ambiguous to assess
- reasoning must be 1-2 sentences maximum
- signals must list the specific configuration signals that drove your assessment
- Do not include any text outside the JSON object"""

_FEW_SHOT_CONFIG = """\
Example 1:
Change type: feature_flag
Flag key: enable_new_payment_processor
Old value: false
New value: true
Error pattern: NullPointerException in process_payment()
Response: {"causal_assessment": "likely", "reasoning": "Enabling a new payment processor code path that was previously inactive could expose untested null handling in process_payment().", "signals": ["new code path activated", "exception in payment processing", "flag enables previously inactive logic"]}

Example 2:
Change type: config
Config key: ui_theme_colour
Old value: "#336699"
New value: "#003366"
Error pattern: PSQLException connection timeout
Response: {"causal_assessment": "unlikely", "reasoning": "A UI colour configuration change cannot affect database connection behaviour.", "signals": ["UI-only change", "unrelated to database or connection configuration"]}

Now assess the following:"""

# ---------------------------------------------------------------------------
# Internal dataclasses — not exported
# ---------------------------------------------------------------------------

@dataclass
class EngineResult:
    evidence: list[EvidenceItem]
    domain: RCADomain
    failure_type: FailureType
    strong_count: int
    moderate_count: int
    smoking_gun: bool = False


@dataclass
class LLMFinding:
    causal_assessment: str  # "likely" | "unlikely" | "uncertain"
    reasoning: str
    signals: list[str]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def investigate(
    candidate: RankedCandidate,
    node: ServiceNode,
    graph: nx.DiGraph,
    spans: list[Span],
    metrics: list[Metric],
    baselines: dict[tuple[str, MetricType], BaselineStats],
    incident_start: datetime,
    code_change_event: CodeChangeEvent | None,
    llm_client: Any | None,
    iteration: int,
) -> EnrichedCandidate:
    """
    Investigate a single ServiceNode candidate using the code engine and config engine.

    Parameters graph, metrics, and baselines are reserved for future
    use (e.g. graph-aware blast radius computation, metric-based severity
    refinement). They are accepted for API symmetry and passed through
    but not consumed by the current implementation.
    """
    try:
        candidate_id = candidate.candidate.node_id

        code_result = _run_code_engine(
            candidate_id=candidate_id,
            node=node,
            spans=spans,
            baselines=baselines,
            incident_start=incident_start,
            code_change_event=code_change_event,
            llm_client=llm_client,
        )
        config_result = _run_config_engine(
            candidate_id=candidate_id,
            node=node,
            spans=spans,
            incident_start=incident_start,
            llm_client=llm_client,
        )
        domain, failure_type, evidence, priority = _arbitrate(
            code_result=code_result,
            config_result=config_result,
            current_priority=candidate.candidate.priority_score,
        )
        return EnrichedCandidate(
            node_id=candidate.candidate.node_id,
            node_type=candidate.candidate.node_type,
            domain=domain,
            failure_type=failure_type,
            evidence=evidence,
            is_potential_origin=candidate.candidate.is_potential_origin,
            priority_score=priority,
            causal_confidence=candidate.causal_confidence,
            iteration_found=iteration,
        )
    # Intentional broad catch: domain RCA failure must not crash the
    # reasoning loop. The UNKNOWN fallback allows scoring to proceed
    # with whatever other candidates have been enriched.
    # Tests should explicitly cover graceful degradation paths.
    except Exception as exc:
        logger.warning(
            "investigate: failed for %r: %s",
            candidate.candidate.node_id,
            exc,
            exc_info=True,
        )
        return EnrichedCandidate(
            node_id=candidate.candidate.node_id,
            node_type=candidate.candidate.node_type,
            domain=RCADomain.UNKNOWN,
            failure_type=FailureType.UNKNOWN,
            evidence=[],
            is_potential_origin=candidate.candidate.is_potential_origin,
            priority_score=candidate.candidate.priority_score,
            causal_confidence=candidate.causal_confidence,
            iteration_found=iteration,
        )


# ---------------------------------------------------------------------------
# Code engine
# ---------------------------------------------------------------------------

def _run_code_engine(
    candidate_id: str,
    node: ServiceNode,
    spans: list[Span],
    baselines: dict[tuple[str, MetricType], BaselineStats],
    incident_start: datetime,
    code_change_event: CodeChangeEvent | None,
    llm_client: Any | None,
) -> EngineResult:
    """
    MVP limitations:
    - Only the most recent deploy in the lookback window is analyzed. A prior
      deploy in the same window could be the true cause. V2: analyze all deploys
      and weight evidence by temporal proximity.
    - Exception extraction uses the first exception found across error span events
      only. Multiple exception types are not aggregated. V2: collect all unique
      exception types and weight by frequency.
    - LLM "uncertain" findings are dropped (logged at DEBUG). They are not surfaced
      as weak signals because uncertain adds no information above the base rate.
    - Degraded operation is selected by highest error rate among operations with
      at least one error span.
    - APPLICATION_ERROR is a generic fallback when no deploy, exception, or code
      change signal is found. It is not a diagnosis — it means the code engine
      found no specific cause. The scorer will assign low confidence accordingly.
    """
    evidence: list[EvidenceItem] = []
    inc_start_utc = _to_utc(incident_start)
    deploy_window_start = inc_start_utc - timedelta(minutes=DEPLOY_LOOKBACK_MINUTES)
    incident_window_end = inc_start_utc + timedelta(seconds=INCIDENT_WINDOW_SECONDS)

    # ---- Step 1: Deploy detection ----------------------------------------
    recent_deploy = None
    deploy_found = False

    candidate_deploys = [
        d for d in node.recent_deploys
        if _in_window(d.timestamp, deploy_window_start, inc_start_utc)
    ]
    if candidate_deploys:
        candidate_deploys.sort(key=lambda d: d.timestamp, reverse=True)
        recent_deploy = candidate_deploys[0]
        deploy_found = True
        minutes_before = (
            (inc_start_utc - _to_utc(recent_deploy.timestamp)).total_seconds() / 60.0
        )
        # TODO (V2): derive minutes_before from first observed error span
        # timestamp rather than incident_start. Use incident_start only as
        # fallback when no error spans exist for this candidate.
        evidence.append(make_deploy_evidence(
            version=recent_deploy.version,
            minutes_before=minutes_before,
            service_id=candidate_id,
        ))

    # ---- Step 2: Exception extraction ------------------------------------
    error_spans = [
        s for s in spans
        if s.service_id == candidate_id
        and s.otel_status_code == 2
        and _in_window(s.start_time, inc_start_utc, incident_window_end)
    ]

    exception_type: str | None = None
    exception_message: str | None = None
    exception_found = False

    for span in error_spans:
        for event in span.events:
            attrs = event.get("attributes", {})
            exc_type = attrs.get("exception.type")
            exc_msg = attrs.get("exception.message")
            if exc_type:
                exception_type = str(exc_type)
                exception_message = str(exc_msg) if exc_msg is not None else ""
                exception_found = True
                break
        if exception_found:
            break

    exception_evidence_item: EvidenceItem | None = None
    if exception_found and exception_type:
        exception_evidence_item = make_exception_evidence(
            exception_type=exception_type,
            exception_message=exception_message or "",
            service_id=candidate_id,
        )
        evidence.append(exception_evidence_item)

    # ---- Step 3: LLM Call 1 (Case A, conditional) -----------------------
    if (
        deploy_found
        and recent_deploy is not None
        and recent_deploy.diff_summary
        and exception_found
        and exception_type
        and llm_client is not None
    ):
        finding = _call_llm_case_a(
            llm_client=llm_client,
            deploy=recent_deploy,
            exception_type=exception_type,
            exception_message=exception_message or "",
            service_id=candidate_id,
        )
        if finding is not None:
            if finding.causal_assessment == "likely":
                # Structured LLM finding — STRONG reflects that the LLM identified a
                # specific causal mechanism with reasoning and signals, not that LLM
                # output is treated as scorer ground truth. This influences priority
                # via strong_count. TODO (V2): gate STRONG on corroborating
                # deterministic signal (e.g. stacktrace match present).
                evidence.append(EvidenceItem(
                    signal="llm_causal_assessment",
                    finding=finding.reasoning,
                    strength=EvidenceStrength.STRONG,
                    metadata={
                        "assessment": "likely",
                        "signals": ", ".join(finding.signals),
                    },
                ))
            elif finding.causal_assessment == "unlikely":
                evidence.append(EvidenceItem(
                    signal="llm_causal_assessment",
                    finding=(
                        f"LLM assessment: change unlikely to cause this exception"
                        f" — {finding.reasoning}"
                    ),
                    strength=EvidenceStrength.WEAK,
                    metadata={"assessment": "unlikely"},
                ))
            else:
                logger.debug(
                    "_run_code_engine: LLM Case A returned 'uncertain' for candidate %r"
                    " — no evidence item added",
                    candidate_id,
                )

    # ---- Step 4: Case B (conditional) -----------------------------------
    smoking_gun = False

    if code_change_event is not None:
        if code_change_event.service_id != candidate_id:
            logger.warning(
                "_run_code_engine: skipping Case B — code_change_event "
                "service_id %r does not match candidate %r",
                code_change_event.service_id, candidate_id,
            )
            code_change_event = None
    if code_change_event is not None:
        cc_ts = _to_utc(code_change_event.timestamp)
        if not _in_window(cc_ts, deploy_window_start, inc_start_utc):
            logger.warning(
                "_run_code_engine: skipping Case B — code_change_event "
                "timestamp %s is outside deploy window for candidate %r",
                cc_ts, candidate_id,
            )
            code_change_event = None

    if code_change_event is not None and recent_deploy is not None:
        if (
            code_change_event.deploy_event_id is not None
            and code_change_event.deploy_event_id != recent_deploy.event_id
        ):
            logger.warning(
                "_run_code_engine: skipping Case B — code_change_event "
                "deploy_event_id %r does not match recent deploy event_id %r",
                code_change_event.deploy_event_id, recent_deploy.event_id,
            )
            code_change_event = None

    # Known limitation: deploy_event_id linkage is checked when present,
    # but code_change_event.deploy_event_id is optional and may be None.
    # When None, linkage falls back to service_id + timestamp window only.
    # V2: enforce deploy_event_id or commit SHA linkage as a hard requirement.
    # Additionally: Case B analysis is limited to the single code_change_event
    # supplied globally to analyze(). node.recent_deploys may contain multiple
    # deploys in the window, but only the globally supplied code_change_event
    # is available for stacktrace matching. V2: accept list[CodeChangeEvent]
    # and match each deploy's code change independently.
    if code_change_event is not None and not node.code_access_enabled:
        logger.warning(
            "_run_code_engine: skipping Case B for %r — code_access_enabled=False",
            candidate_id,
        )
    if code_change_event is not None and node.code_access_enabled:
        # Step 4a: Stacktrace matching
        stacktrace_match = _find_stacktrace_match(
            error_spans=error_spans,
            code_change_event=code_change_event,
        )

        if stacktrace_match is not None:
            evidence.append(make_stacktrace_evidence(
                function_name=stacktrace_match.function_change.function_name,
                file_path=stacktrace_match.function_change.file_path,
                line_number=stacktrace_match.frame.line_number,
                match_type=stacktrace_match.match_type.value,
            ))

            # Step 4b: LLM Call 2 (Case B deep analysis)
            fc = stacktrace_match.function_change
            if fc.old_code and fc.new_code and llm_client is not None:
                b_finding = _call_llm_case_b(
                    llm_client=llm_client,
                    match=stacktrace_match,
                    exception_type=exception_type or "",
                    exception_message=exception_message or "",
                    service_id=candidate_id,
                )
                if b_finding is not None:
                    if (
                        b_finding.causal_assessment == "likely"
                        and stacktrace_match.match_type in (
                            MatchType.EXACT_LINE, MatchType.FUNCTION_MATCH
                        )
                    ):
                        # Smoking gun requires all three:
                        # 1. Deterministic stacktrace match at EXACT_LINE or FUNCTION_MATCH
                        #    level (FILE_MATCH is insufficient — too weak)
                        # 2. Validated code_change_event (service_id + timestamp checked above)
                        # 3. LLM "likely" assessment
                        # No single factor alone is sufficient.
                        smoking_gun = True
                        evidence.append(EvidenceItem(
                            signal="llm_case_b_analysis",
                            finding=b_finding.reasoning,
                            strength=EvidenceStrength.STRONG,
                            metadata={
                                "assessment": "likely",
                                "function": fc.function_name,
                                "match_type": stacktrace_match.match_type.value,
                                "signals": ", ".join(b_finding.signals),
                            },
                        ))
                    elif b_finding.causal_assessment == "unlikely":
                        evidence.append(EvidenceItem(
                            signal="llm_case_b_analysis",
                            finding=(
                                f"LLM assessment: code change unlikely to cause this"
                                f" exception — {b_finding.reasoning}"
                            ),
                            strength=EvidenceStrength.WEAK,
                            metadata={
                                "assessment": "unlikely",
                                "function": fc.function_name,
                            },
                        ))
                    else:
                        logger.debug(
                            "_run_code_engine: LLM Case B returned 'uncertain' for candidate %r"
                            " — no evidence item added",
                            candidate_id,
                        )

    # ---- Step 5: Degraded operation -------------------------------------
    if error_spans:
        service_spans_in_window = [
            s for s in spans
            if s.service_id == candidate_id
            and _in_window(s.start_time, inc_start_utc, incident_window_end)
        ]
        op_total: dict[str, int] = defaultdict(int)
        for s in service_spans_in_window:
            op_total[s.operation_name] += 1

        op_errors: dict[str, int] = defaultdict(int)
        for s in error_spans:
            op_errors[s.operation_name] += 1

        if op_errors:
            top_op = max(
                op_errors,
                key=lambda op: op_errors[op] / op_total[op] if op_total[op] > 0 else 0.0,
            )
            total = op_total.get(top_op, 0)
            rate = op_errors[top_op] / total if total > 0 else 1.0
            # The following evidence items (degraded_operation, llm_causal_assessment,
            # llm_case_b_analysis, post_change_errors, simultaneous_change,
            # llm_config_assessment) are service-domain-specific signals. Their
            # strength is intentionally owned here rather than in evidence_builders.py
            # because they are not reusable across domains.
            evidence.append(EvidenceItem(
                signal="degraded_operation",
                finding=f"{top_op} error rate {rate:.0%} in incident window",
                strength=EvidenceStrength.WEAK,
                metadata={
                    "operation": top_op,
                    "error_rate": f"{rate:.3f}",
                },
            ))

    # ---- Step 6: No deploy negative signal ------------------------------
    if not deploy_found:
        evidence.append(make_no_deploy_evidence(
            service_id=candidate_id,
            lookback_minutes=DEPLOY_LOOKBACK_MINUTES,
        ))

    # ---- Domain and failure_type assignment -----------------------------
    if smoking_gun:
        failure_type = FailureType.CODE_REGRESSION
    elif deploy_found and exception_found:
        failure_type = FailureType.CODE_REGRESSION
    elif deploy_found:
        failure_type = FailureType.CODE_REGRESSION
    elif exception_found:
        failure_type = FailureType.RUNTIME_ERROR
    else:
        failure_type = FailureType.APPLICATION_ERROR

    strong_count = sum(1 for e in evidence if e.strength == EvidenceStrength.STRONG)
    moderate_count = sum(1 for e in evidence if e.strength == EvidenceStrength.MODERATE)

    return EngineResult(
        evidence=evidence,
        domain=RCADomain.APPLICATION_CODE,
        failure_type=failure_type,
        strong_count=strong_count,
        moderate_count=moderate_count,
        smoking_gun=smoking_gun,
    )


# ---------------------------------------------------------------------------
# Config engine
# ---------------------------------------------------------------------------

def _run_config_engine(
    candidate_id: str,
    node: ServiceNode,
    spans: list[Span],
    incident_start: datetime,
    llm_client: Any | None,
) -> EngineResult:
    evidence: list[EvidenceItem] = []
    inc_start_utc = _to_utc(incident_start)
    config_window_start = inc_start_utc - timedelta(minutes=CONFIG_LOOKBACK_MINUTES)

    flag_events: list[FeatureFlagChangeEvent] = []
    config_events: list[ConfigChangeEvent] = []

    # ---- Step 1: Feature flag changes ------------------------------------
    for ff in node.recent_feature_flag_changes:
        if _in_window(ff.timestamp, config_window_start, inc_start_utc):
            flag_events.append(ff)
            minutes_before = (
                (inc_start_utc - _to_utc(ff.timestamp)).total_seconds() / 60.0
            )
            # TODO (V2): derive minutes_before from first observed error span
            # timestamp rather than incident_start. Use incident_start only as
            # fallback when no error spans exist for this candidate.
            evidence.append(make_feature_flag_evidence(
                flag_key=ff.flag_key,
                old_value=str(ff.old_value),
                new_value=str(ff.new_value),
                minutes_before=minutes_before,
                service_id=candidate_id,
            ))

    # ---- Step 2: Config changes ------------------------------------------
    for cfg in node.recent_config_changes:
        if _in_window(cfg.timestamp, config_window_start, inc_start_utc):
            config_events.append(cfg)
            minutes_before = (
                (inc_start_utc - _to_utc(cfg.timestamp)).total_seconds() / 60.0
            )
            # TODO (V2): derive minutes_before from first observed error span
            # timestamp rather than incident_start. Use incident_start only as
            # fallback when no error spans exist for this candidate.
            evidence.append(make_config_change_evidence(
                config_key=cfg.key,
                old_value=str(cfg.old_value),
                new_value=str(cfg.new_value),
                minutes_before=minutes_before,
                service_id=candidate_id,
            ))

    all_change_events: list[FeatureFlagChangeEvent | ConfigChangeEvent] = (
        list(flag_events) + list(config_events)
    )

    # ---- Step 3: LLM Call 3 (config interpretation, one per change) -----
    if all_change_events and llm_client is not None:
        error_pattern = _extract_error_pattern(candidate_id, spans, incident_start)
        # LLM analysis order: flag events before config events (flag_events +
        # config_events). Feature flags are higher signal and get priority when
        # the 3-call cap is hit. This is intentional.
        # MVP limitation: capped at 3 LLM calls total across all change events.
        # V2: prioritise by temporal proximity to first error span.
        llm_cap = min(len(all_change_events), 3)

        for i, change_event in enumerate(all_change_events[:llm_cap]):
            finding = _call_llm_config(
                llm_client=llm_client,
                change_event=change_event,
                error_pattern=error_pattern,
                service_id=candidate_id,
            )
            if finding is None:
                continue

            if finding.causal_assessment == "likely":
                # Structured LLM finding — STRONG reflects that the LLM identified a
                # specific causal mechanism with reasoning and signals, not that LLM
                # output is treated as scorer ground truth. This influences priority
                # via strong_count. TODO (V2): gate STRONG on corroborating
                # deterministic signal (e.g. stacktrace match present).
                evidence.append(EvidenceItem(
                    signal="llm_config_assessment",
                    finding=finding.reasoning,
                    strength=EvidenceStrength.STRONG,
                    metadata={
                        "assessment": "likely",
                        "signals": ", ".join(finding.signals),
                    },
                ))
            elif finding.causal_assessment == "unlikely":
                evidence.append(EvidenceItem(
                    signal="llm_config_assessment",
                    finding=(
                        f"LLM assessment: change unlikely to cause these errors"
                        f" — {finding.reasoning}"
                    ),
                    strength=EvidenceStrength.WEAK,
                    metadata={"assessment": "unlikely"},
                ))
            else:
                logger.debug(
                    "_run_config_engine: LLM config assessment returned 'uncertain'"
                    " for candidate %r change %d — no evidence item added",
                    candidate_id, i,
                )

    # ---- Step 4: Post-change error operations ----------------------------
    if all_change_events:
        inc_window_end = inc_start_utc + timedelta(seconds=INCIDENT_WINDOW_SECONDS)
        error_spans = [
            s for s in spans
            if s.service_id == candidate_id
            and s.otel_status_code == 2
            and _in_window(s.start_time, inc_start_utc, inc_window_end)
        ]
        if error_spans:
            all_svc_spans = [
                s for s in spans
                if s.service_id == candidate_id
                and _in_window(s.start_time, inc_start_utc, inc_window_end)
            ]
            op_total: dict[str, int] = defaultdict(int)
            for s in all_svc_spans:
                op_total[s.operation_name] += 1

            op_errors: dict[str, int] = defaultdict(int)
            for s in error_spans:
                op_errors[s.operation_name] += 1

            top_op = max(
                op_errors,
                key=lambda op: op_errors[op] / op_total[op] if op_total[op] > 0 else 0.0,
            )
            total = op_total.get(top_op, 0)
            rate = op_errors[top_op] / total if total > 0 else 1.0
            evidence.append(EvidenceItem(
                signal="post_change_errors",
                finding=f"{top_op} error rate {rate:.0%} after config/flag change",
                strength=EvidenceStrength.MODERATE,
                metadata={
                    "operation": top_op,
                    "error_rate": f"{rate:.3f}",
                },
            ))

    # ---- Step 5: Simultaneous change detection ---------------------------
    deploy_window_start = inc_start_utc - timedelta(minutes=DEPLOY_LOOKBACK_MINUTES)
    has_deploy = any(
        _in_window(d.timestamp, deploy_window_start, inc_start_utc)
        for d in node.recent_deploys
    )
    has_config_or_flag = bool(flag_events or config_events)

    if has_deploy and has_config_or_flag:
        # both_strong is set to "pending_arbitration" here and resolved in
        # _arbitrate() when both engines have strong evidence. This post-construction
        # metadata mutation is an MVP wart — V2 should pass both_strong as a
        # parameter computed after both engines complete.
        evidence.append(EvidenceItem(
            signal="simultaneous_change",
            finding=(
                f"{candidate_id} had both a deploy and a config/flag change in"
                f" the incident window — both are candidate causes"
            ),
            strength=EvidenceStrength.MODERATE,
            metadata={
                "has_deploy": "true",
                "has_config_change": "true",
                "both_strong": "pending_arbitration",
            },
        ))

    # ---- Domain and failure_type assignment -----------------------------
    flag_found = bool(flag_events)
    config_found = bool(config_events)

    if flag_found:
        failure_type = FailureType.FEATURE_FLAG_REGRESSION
        domain = RCADomain.CONFIGURATION
    elif config_found:
        failure_type = FailureType.CONFIG_REGRESSION
        domain = RCADomain.CONFIGURATION
    else:
        failure_type = FailureType.APPLICATION_ERROR
        domain = RCADomain.APPLICATION_CODE

    strong_count = sum(1 for e in evidence if e.strength == EvidenceStrength.STRONG)
    moderate_count = sum(1 for e in evidence if e.strength == EvidenceStrength.MODERATE)

    return EngineResult(
        evidence=evidence,
        domain=domain,
        failure_type=failure_type,
        strong_count=strong_count,
        moderate_count=moderate_count,
    )


# ---------------------------------------------------------------------------
# Arbitration
# ---------------------------------------------------------------------------

def _arbitrate(
    code_result: EngineResult,
    config_result: EngineResult,
    current_priority: float,
) -> tuple[RCADomain, FailureType, list[EvidenceItem], float]:
    # Winner = engine with more STRONG items. Tie → MODERATE count. Tie → code wins.
    if code_result.strong_count > config_result.strong_count:
        winner, loser = code_result, config_result
    elif config_result.strong_count > code_result.strong_count:
        winner, loser = config_result, code_result
    elif code_result.moderate_count >= config_result.moderate_count:
        winner, loser = code_result, config_result
    else:
        winner, loser = config_result, code_result

    # Priority score update
    if code_result.smoking_gun:
        new_priority = PRIORITY_OVERRIDE_SMOKING_GUN
    elif winner.strong_count >= 1:
        new_priority = min(1.0, current_priority + PRIORITY_BOOST_STRONG)
    elif winner.strong_count == 0 and winner.moderate_count >= 2:
        new_priority = min(1.0, current_priority + PRIORITY_BOOST_MODERATE)
    elif code_result.strong_count == 0 and config_result.strong_count == 0:
        new_priority = max(0.0, current_priority - PRIORITY_PENALTY_WEAK)
    else:
        new_priority = current_priority

    # Evidence: winner first, then loser — always both, no deduplication
    merged_evidence = list(winner.evidence) + list(loser.evidence)

    # Both engines have strong evidence. Code domain wins by convention.
    # TODO: Evidence Arbitrator and Multi-Factor Engine should check
    # simultaneous_change evidence item metadata "both_strong": "true"
    # to flag this as a potential multi-factor or ambiguous incident.
    # When both engines have strong evidence, code domain wins by convention
    # regardless of which engine had more STRONG items. The winner/loser
    # assignment above is superseded in this case. This is intentional —
    # deploys are more actionable than config changes when both fire.
    # MVP wart: the both_strong metadata update mutates the EvidenceItem's
    # metadata dict in place. EvidenceItem is frozen but its metadata dict
    # field is itself mutable. V2: construct simultaneous_change item with
    # the correct both_strong value after both engines complete.
    if code_result.strong_count >= 1 and config_result.strong_count >= 1:
        for item in merged_evidence:
            if item.signal == "simultaneous_change" and item.metadata.get("both_strong") == "pending_arbitration":
                item.metadata["both_strong"] = "true"
                break
        domain = code_result.domain
        failure_type = code_result.failure_type
    else:
        domain = winner.domain
        failure_type = winner.failure_type

    return domain, failure_type, merged_evidence, new_priority


# ---------------------------------------------------------------------------
# LLM call implementations
# ---------------------------------------------------------------------------

def _call_llm_case_a(
    llm_client: Any,
    deploy: Any,
    exception_type: str,
    exception_message: str,
    service_id: str,
) -> LLMFinding | None:
    try:
        user_message = (
            f"{_FEW_SHOT_CASE_A}\n\n"
            f"Deploy diff summary: {deploy.diff_summary}\n"
            f"Service: {service_id}\n"
            f"Deploy version: {deploy.version}\n"
            f"Exception type: {exception_type}\n"
            f"Exception message: {exception_message}"
        )
        response = llm_client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT_CASE_A},
                {"role": "user", "content": user_message},
            ],
            max_completion_tokens=LLM_MAX_TOKENS,
        )
        raw = response.choices[0].message.content.strip()
        return _parse_llm_response(raw)
    except Exception as exc:
        logger.warning("_call_llm_case_a: failed for %r: %s", service_id, exc)
        return LLMFinding(causal_assessment="uncertain", reasoning="parse error", signals=[])


def _call_llm_case_b(
    llm_client: Any,
    match: StacktraceMatch,
    exception_type: str,
    exception_message: str,
    service_id: str,
) -> LLMFinding | None:
    try:
        fc = match.function_change
        user_message = (
            f"Service: {service_id}\n"
            f"Function: {fc.function_name}\n"
            f"File: {fc.file_path}\n"
            f"Match type: {match.match_type.value}\n\n"
            f"OLD CODE:\n{fc.old_code}\n\n"
            f"NEW CODE:\n{fc.new_code}\n\n"
            f"Exception type: {exception_type}\n"
            f"Exception at line: {match.frame.line_number}\n"
            f"Exception message: {exception_message}"
        )
        response = llm_client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT_CASE_B},
                {"role": "user", "content": user_message},
            ],
            max_completion_tokens=LLM_MAX_TOKENS,
        )
        raw = response.choices[0].message.content.strip()
        return _parse_llm_response(raw)
    except Exception as exc:
        logger.warning("_call_llm_case_b: failed for %r: %s", service_id, exc)
        return LLMFinding(causal_assessment="uncertain", reasoning="parse error", signals=[])


def _call_llm_config(
    llm_client: Any,
    change_event: ConfigChangeEvent | FeatureFlagChangeEvent,
    error_pattern: str,
    service_id: str,
) -> LLMFinding | None:
    try:
        if isinstance(change_event, FeatureFlagChangeEvent):
            change_detail = (
                f"Change type: feature_flag\n"
                f"Flag key: {change_event.flag_key}\n"
                f"Old value: {change_event.old_value}\n"
                f"New value: {change_event.new_value}"
            )
        else:
            change_detail = (
                f"Change type: config\n"
                f"Config key: {change_event.key}\n"
                f"Old value: {change_event.old_value}\n"
                f"New value: {change_event.new_value}"
            )
        user_message = (
            f"{_FEW_SHOT_CONFIG}\n\n"
            f"Service: {service_id}\n"
            f"{change_detail}\n"
            f"Error pattern: {error_pattern}"
        )
        response = llm_client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT_CONFIG},
                {"role": "user", "content": user_message},
            ],
            max_completion_tokens=LLM_MAX_TOKENS,
        )
        raw = response.choices[0].message.content.strip()
        return _parse_llm_response(raw)
    except Exception as exc:
        logger.warning("_call_llm_config: failed for %r: %s", service_id, exc)
        return LLMFinding(causal_assessment="uncertain", reasoning="parse error", signals=[])


def _parse_llm_response(raw: str) -> LLMFinding:
    """Parse and validate an LLM JSON response. Never raises."""
    try:
        cleaned = raw.strip()
        cleaned = re.sub(r"^```json\s*", "", cleaned)
        cleaned = re.sub(r"^```\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

        data = json.loads(cleaned)
        assessment = data.get("causal_assessment", "")
        if assessment not in {"likely", "unlikely", "uncertain"}:
            logger.warning(
                "_parse_llm_response: invalid causal_assessment %r, raw=%r",
                assessment, raw,
            )
            return LLMFinding(causal_assessment="uncertain", reasoning="parse error", signals=[])

        reasoning = data.get("reasoning", "")
        if not reasoning or not isinstance(reasoning, str):
            logger.warning("_parse_llm_response: empty/invalid reasoning, raw=%r", raw)
            return LLMFinding(causal_assessment="uncertain", reasoning="parse error", signals=[])

        signals = data.get("signals", [])
        if not isinstance(signals, list):
            signals = []

        return LLMFinding(
            causal_assessment=assessment,
            reasoning=reasoning,
            signals=[str(s) for s in signals],
        )
    except Exception as exc:
        logger.warning("_parse_llm_response: failed to parse raw=%r: %s", raw, exc)
        return LLMFinding(causal_assessment="uncertain", reasoning="parse error", signals=[])


# ---------------------------------------------------------------------------
# Stacktrace matching helpers
# ---------------------------------------------------------------------------

def _find_stacktrace_match(
    error_spans: list[Span],
    code_change_event: CodeChangeEvent,
) -> StacktraceMatch | None:
    """
    Find the best stacktrace match across error spans and changed functions.
    Preference order: EXACT_LINE > FUNCTION_MATCH > FILE_MATCH.
    """
    _priority = {
        MatchType.EXACT_LINE: 2,
        MatchType.FUNCTION_MATCH: 1,
        MatchType.FILE_MATCH: 0,
    }
    _confidence = {
        MatchType.EXACT_LINE: 1.0,
        MatchType.FUNCTION_MATCH: FUNCTION_MATCH_MIN_CONFIDENCE,
        MatchType.FILE_MATCH: 0.3,
    }

    best_match: StacktraceMatch | None = None
    best_priority = -1

    for span in error_spans:
        frames = _extract_frames_from_span(span)
        for frame in frames:
            for file_change in code_change_event.files_changed:
                for func_change in file_change.functions_changed:
                    mt = _match_frame_to_function(frame, func_change)
                    if mt is None:
                        continue
                    prio = _priority[mt]
                    if prio > best_priority:
                        best_priority = prio
                        best_match = StacktraceMatch(
                            span_id=span.span_id,
                            frame=frame,
                            function_change=func_change,
                            match_type=mt,
                            confidence=_confidence[mt],
                        )
                    if best_priority == 2:
                        return best_match

    return best_match


def _extract_frames_from_span(span: Span) -> list[StacktraceFrame]:
    """Extract stacktrace frames from span events and span attributes."""
    raw_stacktraces: list[str] = []

    for event in span.events:
        attrs = event.get("attributes", {})
        st = attrs.get("exception.stacktrace")
        if st and isinstance(st, str):
            raw_stacktraces.append(st)

    st = span.attributes.get("exception.stacktrace")
    if st and isinstance(st, str):
        raw_stacktraces.append(st)

    frames: list[StacktraceFrame] = []
    seen: set[tuple[str, str, int]] = set()

    for raw in raw_stacktraces:
        for frame in _parse_stacktrace(raw):
            key = (frame.file_path, frame.function_name, frame.line_number)
            if key not in seen:
                seen.add(key)
                frames.append(frame)

    return frames


# Python-style: File "path/to/file.py", line 42, in function_name
_PYTHON_FRAME_RE = re.compile(
    r'File\s+"?([^",\n]+)"?,\s+line\s+(\d+),\s+in\s+(\S+)'
)

# Java/Node-style: name (file:line) or name (file:line:column).
# The file group is non-greedy so a V8 column number is not folded
# into the file path with the column mistaken for the line number.
_JAVA_NODE_FRAME_RE = re.compile(
    r'(?:at\s+)?([\w.<>/\[\]$]+)'
    r'(?:\s+\[as\s+[^\]]+\])?'
    r'\s*\(([^)]+?):(\d+)(?::\d+)?\)'
)

# V8 anonymous frames have no parens: "at /path/file.js:23:13".
# Express arrow-function handlers and middleware land here.
_V8_ANON_FRAME_RE = re.compile(
    r'^\s*at\s+(?:async\s+)?([^\s()]+?):(\d+):(\d+)\s*$',
    re.MULTILINE,
)


def _parse_stacktrace(raw: str) -> list[StacktraceFrame]:
    """
    Best-effort parsing of stacktrace strings into StacktraceFrame objects.

    Handles three common formats:
      Python:   File "path/to/file.py", line 42, in function_name
      Java:     at ClassName.method(File.java:42)
      Node/V8:  at functionName (/path/to/file.js:42:15)
                at Class.method [as alias] (/path/to/file.js:42:15)
                at /path/to/file.js:42:15   (anonymous frame)
    """
    frames: list[StacktraceFrame] = []
    try:
        for m in _PYTHON_FRAME_RE.finditer(raw):
            frames.append(StacktraceFrame(
                file_path=m.group(1),
                function_name=m.group(3),
                line_number=int(m.group(2)),
            ))

        for m in _JAVA_NODE_FRAME_RE.finditer(raw):
            full_method = m.group(1)
            func_name = full_method.rsplit(".", 1)[-1] if "." in full_method else full_method
            frames.append(StacktraceFrame(
                file_path=m.group(2),
                function_name=func_name,
                line_number=int(m.group(3)),
            ))

        # code_ingester never names a FunctionChange "<anonymous>" (unnamed
        # functions are skipped), so anonymous frames can only produce
        # EXACT_LINE or FILE_MATCH — never a spurious FUNCTION_MATCH.
        for m in _V8_ANON_FRAME_RE.finditer(raw):
            frames.append(StacktraceFrame(
                file_path=m.group(1),
                function_name="<anonymous>",
                line_number=int(m.group(2)),
            ))
    except Exception as exc:
        logger.debug(
            "_parse_stacktrace: aborted mid-parse (%s), returning %d frame(s); raw=%.200r",
            exc, len(frames), raw,
        )
    return frames


def _match_frame_to_function(
    frame: StacktraceFrame,
    func_change: FunctionChange,
) -> MatchType | None:
    """Match a stacktrace frame to a changed function. Returns None if no match."""
    frame_base = os.path.basename(frame.file_path)
    func_base = os.path.basename(func_change.file_path)

    if (
        frame_base == func_base
        and abs(frame.line_number - func_change.start_line) <= EXACT_LINE_TOLERANCE
    ):
        return MatchType.EXACT_LINE

    if frame.function_name == func_change.function_name and frame_base == func_base:
        return MatchType.FUNCTION_MATCH

    if frame_base == func_base:
        return MatchType.FILE_MATCH

    return None


# ---------------------------------------------------------------------------
# Error pattern extraction helper
# ---------------------------------------------------------------------------

def _extract_error_pattern(
    candidate_id: str,
    spans: list[Span],
    incident_start: datetime,
) -> str:
    """Derive a short error pattern description for LLM config context."""
    inc_start_utc = _to_utc(incident_start)
    window_end = inc_start_utc + timedelta(seconds=INCIDENT_WINDOW_SECONDS)

    error_spans = [
        s for s in spans
        if s.service_id == candidate_id
        and s.otel_status_code == 2
        and _in_window(s.start_time, inc_start_utc, window_end)
    ]

    exc_counts: dict[str, int] = defaultdict(int)
    for span in error_spans:
        for event in span.events:
            exc_type = event.get("attributes", {}).get("exception.type")
            if exc_type:
                exc_counts[str(exc_type)] += 1

    if exc_counts:
        return max(exc_counts, key=lambda k: exc_counts[k])

    status_counts: dict[int, int] = defaultdict(int)
    for span in error_spans:
        raw = (
            span.attributes.get("http.status_code")
            or span.attributes.get("http.response.status_code")
        )
        if raw is not None:
            try:
                status_counts[int(raw)] += 1
            except (ValueError, TypeError):
                pass

    if status_counts:
        top_code = max(status_counts, key=lambda k: status_counts[k])
        return f"HTTP {top_code} errors"

    return "elevated error rate"


# ---------------------------------------------------------------------------
# Private timezone / window helpers
# ---------------------------------------------------------------------------

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
