"""
Tests for reasoning/domain_rca/service.py

Runnable with: pytest tests/reasoning/test_domain_rca_service.py
No external dependencies. LLM client is mocked inline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import networkx as nx
import pytest

from core.change_event import DeployEvent, FeatureFlagChangeEvent
from core.code_change import (
    CodeChangeEvent,
    FileChange,
    FunctionChange,
    MatchType,
)
from core.nodes import NodeType, ServiceNode
from core.span import Span, SpanKind
from reasoning.contracts import (
    CandidateNode,
    EvidenceStrength,
    FailureType,
    InclusionReason,
    RCADomain,
    RankedCandidate,
)
from reasoning.domain_rca.service import (
    PRIORITY_OVERRIDE_SMOKING_GUN,
    _parse_stacktrace,
    investigate,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_SERVICE_ID = "fraud-service"


def _make_candidate(priority: float = 0.5, causal_confidence: float = 0.5) -> RankedCandidate:
    node = CandidateNode(
        node_id=_SERVICE_ID,
        node_type=NodeType.SERVICE,
        inclusion_reason=InclusionReason.METRIC_ANOMALY,
        is_potential_origin=True,
        priority_score=priority,
    )
    return RankedCandidate(candidate=node, causal_confidence=causal_confidence)


def _make_service_node(**kwargs) -> ServiceNode:
    defaults = dict(
        id=_SERVICE_ID,
        name=_SERVICE_ID,
        recent_deploys=[],
        recent_config_changes=[],
        recent_feature_flag_changes=[],
        code_access_enabled=False,
    )
    defaults.update(kwargs)
    return ServiceNode(**defaults)


def _make_error_span(
    operation: str = "POST /validate",
    exception_type: str | None = None,
    exception_message: str | None = None,
    stacktrace: str | None = None,
    offset_seconds: int = 30,
) -> Span:
    events = []
    if exception_type or stacktrace:
        attrs: dict = {}
        if exception_type:
            attrs["exception.type"] = exception_type
        if exception_message:
            attrs["exception.message"] = exception_message
        if stacktrace:
            attrs["exception.stacktrace"] = stacktrace
        events.append({"name": "exception", "attributes": attrs})

    start = _T0 + timedelta(seconds=offset_seconds)
    return Span(
        trace_id="trace-1",
        span_id=f"span-{operation[:8]}-{offset_seconds}",
        parent_span_id=None,
        service_id=_SERVICE_ID,
        operation_name=operation,
        start_time=start,
        end_time=start + timedelta(milliseconds=200),
        duration_ms=200.0,
        kind=SpanKind.SERVER,
        otel_status_code=2,
        events=events,
    )


def _make_deploy(minutes_before: float, diff_summary: str = "") -> DeployEvent:
    return DeployEvent(
        event_id="deploy-1",
        service_id=_SERVICE_ID,
        timestamp=_T0 - timedelta(minutes=minutes_before),
        version="v1.8.1",
        deploy_scope=[_SERVICE_ID],
        diff_summary=diff_summary,
    )


def _make_feature_flag(minutes_before: float) -> FeatureFlagChangeEvent:
    return FeatureFlagChangeEvent(
        event_id="ff-1",
        service_id=_SERVICE_ID,
        timestamp=_T0 - timedelta(minutes=minutes_before),
        flag_key="enable_new_fraud_check",
        old_value=False,
        new_value=True,
    )


def _make_llm_client(assessment: str, reasoning: str = "test reasoning") -> MagicMock:
    """Return a mock llm_client that produces a fixed causal_assessment."""
    import json as _json

    payload = _json.dumps({
        "causal_assessment": assessment,
        "reasoning": reasoning,
        "signals": ["signal-a"],
    })
    mock_msg = MagicMock()
    mock_msg.content = payload
    mock_choice = MagicMock()
    mock_choice.message = mock_msg
    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]

    client = MagicMock()
    client.chat.completions.create.return_value = mock_resp
    return client


def _make_code_change_event(
    func_name: str = "validate_transaction",
    file_path: str = "validator.py",
    start_line: int = 45,
    old_code: str = "old",
    new_code: str = "new",
) -> CodeChangeEvent:
    fc = FunctionChange(
        function_name=func_name,
        file_path=file_path,
        change_type="modified",
        language="python",
        start_line=start_line,
        end_line=start_line + 10,
        old_code=old_code,
        new_code=new_code,
    )
    file_change = FileChange(
        file_path=file_path,
        language="python",
        change_type="modified",
        raw_diff="--- a/validator.py\n+++ b/validator.py",
        functions_changed=[fc],
    )
    return CodeChangeEvent(
        event_id="cc-1",
        service_id=_SERVICE_ID,
        commit_sha="abc123",
        timestamp=_T0 - timedelta(minutes=10),
        repo_url="https://github.com/example/repo",
        branch="main",
        files_changed=[file_change],
    )


# ---------------------------------------------------------------------------
# Test 1 — Deploy found, no LLM → code engine fires, APPLICATION_CODE domain
# ---------------------------------------------------------------------------

def test_deploy_found_no_llm():
    """Deploy 10 min before incident. No exception, no LLM."""
    candidate = _make_candidate()
    node = _make_service_node(recent_deploys=[_make_deploy(minutes_before=10)])
    graph = nx.DiGraph()
    graph.add_node(_SERVICE_ID, data=node)

    result = investigate(
        candidate=candidate,
        node=node,
        graph=graph,
        spans=[],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=None,
        iteration=1,
    )

    assert result.domain == RCADomain.APPLICATION_CODE
    assert result.failure_type == FailureType.CODE_REGRESSION
    signals = {e.signal for e in result.evidence}
    assert "deploy_in_window" in signals


# ---------------------------------------------------------------------------
# Test 2 — Exception in spans, no deploy → RUNTIME_ERROR
# ---------------------------------------------------------------------------

def test_exception_no_deploy():
    """Error spans with exception.type, no deploy, no LLM."""
    candidate = _make_candidate()
    node = _make_service_node()
    graph = nx.DiGraph()
    graph.add_node(_SERVICE_ID, data=node)

    span = _make_error_span(
        exception_type="NullPointerException",
        exception_message="user_id is None",
    )

    result = investigate(
        candidate=candidate,
        node=node,
        graph=graph,
        spans=[span],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=None,
        iteration=1,
    )

    assert result.failure_type == FailureType.RUNTIME_ERROR
    signals = {e.signal for e in result.evidence}
    assert "exception_in_spans" in signals


# ---------------------------------------------------------------------------
# Test 3 — Feature flag change → config engine fires
# ---------------------------------------------------------------------------

def test_feature_flag_change():
    """FeatureFlagChangeEvent 3 min before incident. No deploy, no LLM."""
    candidate = _make_candidate()
    node = _make_service_node(
        recent_feature_flag_changes=[_make_feature_flag(minutes_before=3)]
    )
    graph = nx.DiGraph()
    graph.add_node(_SERVICE_ID, data=node)

    result = investigate(
        candidate=candidate,
        node=node,
        graph=graph,
        spans=[],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=None,
        iteration=1,
    )

    assert result.domain == RCADomain.CONFIGURATION
    assert result.failure_type == FailureType.FEATURE_FLAG_REGRESSION
    signals = {e.signal for e in result.evidence}
    assert "feature_flag_change" in signals


# ---------------------------------------------------------------------------
# Test 4 — Both deploy and feature flag → both engines fire, evidence merged
# ---------------------------------------------------------------------------

def test_deploy_and_feature_flag_both_engines():
    """Deploy 10 min before and feature flag 5 min before. No LLM."""
    candidate = _make_candidate()
    node = _make_service_node(
        recent_deploys=[_make_deploy(minutes_before=10)],
        recent_feature_flag_changes=[_make_feature_flag(minutes_before=5)],
    )
    graph = nx.DiGraph()
    graph.add_node(_SERVICE_ID, data=node)

    result = investigate(
        candidate=candidate,
        node=node,
        graph=graph,
        spans=[],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=None,
        iteration=1,
    )

    signals = {e.signal for e in result.evidence}
    assert "deploy_in_window" in signals
    assert "feature_flag_change" in signals
    # Code engine domain wins when both have strong evidence
    assert result.domain == RCADomain.APPLICATION_CODE


# ---------------------------------------------------------------------------
# Test 5 — LLM returns "likely" for Case A → exception evidence upgraded
# ---------------------------------------------------------------------------

def test_llm_case_a_likely_upgrades_exception():
    """Deploy with diff_summary + exception span. LLM returns 'likely'."""
    candidate = _make_candidate()
    node = _make_service_node(
        recent_deploys=[_make_deploy(minutes_before=8, diff_summary="removed null check")]
    )
    graph = nx.DiGraph()
    graph.add_node(_SERVICE_ID, data=node)

    span = _make_error_span(
        exception_type="NullPointerException",
        exception_message="cannot be null",
    )
    llm_client = _make_llm_client("likely", reasoning="Null check removal directly caused NPE")

    result = investigate(
        candidate=candidate,
        node=node,
        graph=graph,
        spans=[span],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=llm_client,
        iteration=1,
    )

    strong_items = [e for e in result.evidence if e.strength == EvidenceStrength.STRONG]
    llm_items = [e for e in result.evidence if e.signal == "llm_causal_assessment"]
    assert len(llm_items) >= 1
    assert llm_items[0].strength == EvidenceStrength.STRONG


# ---------------------------------------------------------------------------
# Test 6 — LLM returns bad JSON → degrades gracefully, no crash
# ---------------------------------------------------------------------------

def test_llm_bad_json_degrades_gracefully():
    """LLM returns unparseable response. investigate() must not raise."""
    candidate = _make_candidate()
    node = _make_service_node(
        recent_deploys=[_make_deploy(minutes_before=8, diff_summary="removed null check")]
    )
    graph = nx.DiGraph()
    graph.add_node(_SERVICE_ID, data=node)

    span = _make_error_span(
        exception_type="ValueError",
        exception_message="bad input",
    )

    mock_msg = MagicMock()
    mock_msg.content = "this is not json at all"
    mock_choice = MagicMock()
    mock_choice.message = mock_msg
    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]
    llm_client = MagicMock()
    llm_client.chat.completions.create.return_value = mock_resp

    result = investigate(
        candidate=candidate,
        node=node,
        graph=graph,
        spans=[span],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=llm_client,
        iteration=1,
    )

    # Must not crash; "uncertain" produces no llm_causal_assessment item
    assert result is not None
    llm_items = [e for e in result.evidence if e.signal == "llm_causal_assessment"]
    assert len(llm_items) == 0


# ---------------------------------------------------------------------------
# Test 7 — No signals at all → APPLICATION_ERROR, priority penalised
# ---------------------------------------------------------------------------

def test_no_signals_penalty():
    """ServiceNode with no deploys, no config changes, no error spans."""
    initial_priority = 0.5
    candidate = _make_candidate(priority=initial_priority, causal_confidence=initial_priority)
    node = _make_service_node()
    graph = nx.DiGraph()
    graph.add_node(_SERVICE_ID, data=node)

    result = investigate(
        candidate=candidate,
        node=node,
        graph=graph,
        spans=[],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=None,
        iteration=1,
    )

    assert result.failure_type == FailureType.APPLICATION_ERROR
    assert result.priority_score < candidate.causal_confidence


# ---------------------------------------------------------------------------
# Test 8 — Case B smoking gun → priority overridden to 0.95
# ---------------------------------------------------------------------------

def test_case_b_smoking_gun_overrides_priority():
    """Stacktrace match + LLM 'likely' → smoking gun, priority = 0.95."""
    func_name = "validate_transaction"
    file_path = "validator.py"
    start_line = 45

    stacktrace = (
        f'Traceback (most recent call last):\n'
        f'  File "{file_path}", line {start_line}, in {func_name}\n'
        f'    return True\n'
        f'NullPointerException: user_id is None'
    )

    candidate = _make_candidate(priority=0.6, causal_confidence=0.6)
    node = _make_service_node(
        code_access_enabled=True,
        recent_deploys=[_make_deploy(minutes_before=10)],
    )
    graph = nx.DiGraph()
    graph.add_node(_SERVICE_ID, data=node)

    span = _make_error_span(
        exception_type="NullPointerException",
        exception_message="user_id is None",
        stacktrace=stacktrace,
    )

    code_change = _make_code_change_event(
        func_name=func_name,
        file_path=file_path,
        start_line=start_line,
        old_code="def validate_transaction(user_id):\n    if user_id is None:\n        raise ValueError('null')\n    return True",
        new_code="def validate_transaction(user_id):\n    return True",
    )
    llm_client = _make_llm_client(
        "likely", reasoning="Null check removal caused NullPointerException at line 45"
    )

    result = investigate(
        candidate=candidate,
        node=node,
        graph=graph,
        spans=[span],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=code_change,
        llm_client=llm_client,
        iteration=1,
    )

    assert result.priority_score == pytest.approx(PRIORITY_OVERRIDE_SMOKING_GUN)
    llm_b_items = [e for e in result.evidence if e.signal == "llm_case_b_analysis"]
    assert len(llm_b_items) >= 1
    assert llm_b_items[0].strength == EvidenceStrength.STRONG


# ---------------------------------------------------------------------------
# Test 9 — FILE_MATCH + LLM "likely" must NOT produce smoking gun
# ---------------------------------------------------------------------------

def test_file_match_llm_likely_not_smoking_gun():
    """FILE_MATCH stacktrace + LLM 'likely' must not trigger smoking gun."""
    func_name = "validate_transaction"
    file_path = "validator.py"
    start_line = 45

    # Frame is in the same file but at a distant line with a different function
    # name — produces FILE_MATCH (same basename), not EXACT_LINE or FUNCTION_MATCH.
    stacktrace = (
        f'Traceback (most recent call last):\n'
        f'  File "{file_path}", line 200, in some_other_function\n'
        f'    result = helper()\n'
        f'NullPointerException: user_id is None'
    )

    candidate = _make_candidate(priority=0.6, causal_confidence=0.6)
    node = _make_service_node(
        code_access_enabled=True,
        recent_deploys=[_make_deploy(minutes_before=10)],
    )
    graph = nx.DiGraph()
    graph.add_node(_SERVICE_ID, data=node)

    span = _make_error_span(
        exception_type="NullPointerException",
        exception_message="user_id is None",
        stacktrace=stacktrace,
    )
    code_change = _make_code_change_event(
        func_name=func_name,
        file_path=file_path,
        start_line=start_line,
        old_code="def validate_transaction(user_id):\n    if user_id is None:\n        raise ValueError('null')\n    return True",
        new_code="def validate_transaction(user_id):\n    return True",
    )
    llm_client = _make_llm_client("likely", reasoning="code change caused the exception")

    result = investigate(
        candidate=candidate,
        node=node,
        graph=graph,
        spans=[span],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=code_change,
        llm_client=llm_client,
        iteration=1,
    )

    # FILE_MATCH is insufficient for smoking gun regardless of LLM assessment
    assert result.priority_score < PRIORITY_OVERRIDE_SMOKING_GUN
    b_likely_items = [
        e for e in result.evidence
        if e.signal == "llm_case_b_analysis" and e.metadata.get("assessment") == "likely"
    ]
    assert len(b_likely_items) == 0


# ---------------------------------------------------------------------------
# Test 10 — Wrong service_id code_change_event skips Case B
# ---------------------------------------------------------------------------

def test_wrong_service_id_skips_case_b():
    """code_change_event.service_id != candidate_id → no stacktrace_match evidence."""
    func_name = "validate_transaction"
    file_path = "validator.py"
    start_line = 45

    # Stacktrace that WOULD produce an EXACT_LINE match if the guard did not fire.
    stacktrace = (
        f'Traceback (most recent call last):\n'
        f'  File "{file_path}", line {start_line}, in {func_name}\n'
        f'    return True\n'
        f'NullPointerException: user_id is None'
    )

    candidate = _make_candidate()
    node = _make_service_node(
        code_access_enabled=True,
        recent_deploys=[_make_deploy(minutes_before=10)],
    )
    graph = nx.DiGraph()
    graph.add_node(_SERVICE_ID, data=node)

    span = _make_error_span(
        exception_type="NullPointerException",
        exception_message="user_id is None",
        stacktrace=stacktrace,
    )

    fc = FunctionChange(
        function_name=func_name,
        file_path=file_path,
        change_type="modified",
        language="python",
        start_line=start_line,
        end_line=start_line + 10,
        old_code="old",
        new_code="new",
    )
    wrong_service_code_change = CodeChangeEvent(
        event_id="cc-wrong",
        service_id="wrong-service",
        commit_sha="abc123",
        timestamp=_T0 - timedelta(minutes=10),
        repo_url="https://github.com/example/repo",
        branch="main",
        files_changed=[FileChange(
            file_path=file_path,
            language="python",
            change_type="modified",
            raw_diff="--- a/validator.py\n+++ b/validator.py",
            functions_changed=[fc],
        )],
    )

    result = investigate(
        candidate=candidate,
        node=node,
        graph=graph,
        spans=[span],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=wrong_service_code_change,
        llm_client=None,
        iteration=1,
    )

    signals = {e.signal for e in result.evidence}
    assert "stacktrace_match" not in signals


# ---------------------------------------------------------------------------
# Test 11 — Out-of-window code_change_event skips Case B
# ---------------------------------------------------------------------------

def test_out_of_window_code_change_skips_case_b():
    """code_change_event 60 min before incident (outside 30-min window) → no stacktrace_match."""
    func_name = "validate_transaction"
    file_path = "validator.py"
    start_line = 45

    # Stacktrace that WOULD produce an EXACT_LINE match if the guard did not fire.
    stacktrace = (
        f'Traceback (most recent call last):\n'
        f'  File "{file_path}", line {start_line}, in {func_name}\n'
        f'    return True\n'
        f'NullPointerException: user_id is None'
    )

    candidate = _make_candidate()
    node = _make_service_node(
        code_access_enabled=True,
        recent_deploys=[_make_deploy(minutes_before=10)],
    )
    graph = nx.DiGraph()
    graph.add_node(_SERVICE_ID, data=node)

    span = _make_error_span(
        exception_type="NullPointerException",
        exception_message="user_id is None",
        stacktrace=stacktrace,
    )

    fc = FunctionChange(
        function_name=func_name,
        file_path=file_path,
        change_type="modified",
        language="python",
        start_line=start_line,
        end_line=start_line + 10,
        old_code="old",
        new_code="new",
    )
    old_code_change = CodeChangeEvent(
        event_id="cc-old",
        service_id=_SERVICE_ID,
        commit_sha="abc123",
        timestamp=_T0 - timedelta(minutes=60),  # 60 min before; deploy window is 30 min
        repo_url="https://github.com/example/repo",
        branch="main",
        files_changed=[FileChange(
            file_path=file_path,
            language="python",
            change_type="modified",
            raw_diff="--- a/validator.py\n+++ b/validator.py",
            functions_changed=[fc],
        )],
    )

    result = investigate(
        candidate=candidate,
        node=node,
        graph=graph,
        spans=[span],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=old_code_change,
        llm_client=None,
        iteration=1,
    )

    signals = {e.signal for e in result.evidence}
    assert "stacktrace_match" not in signals


# ---------------------------------------------------------------------------
# Test 12 — causal_confidence preserved unchanged
# ---------------------------------------------------------------------------

def test_causal_confidence_preserved():
    """causal_confidence from the input RankedCandidate passes through unchanged."""
    candidate = _make_candidate(priority=0.5, causal_confidence=0.73)
    node = _make_service_node()
    graph = nx.DiGraph()
    graph.add_node(_SERVICE_ID, data=node)

    result = investigate(
        candidate=candidate,
        node=node,
        graph=graph,
        spans=[],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=None,
        llm_client=None,
        iteration=1,
    )

    assert result.causal_confidence == pytest.approx(0.73)


# ---------------------------------------------------------------------------
# Test 13 — Node/V8 named frames: true line number, clean file path
# ---------------------------------------------------------------------------

def test_parse_stacktrace_node_named_frames():
    """V8 file:line:column frames must yield the line, not the column."""
    raw = (
        "TypeError: Cannot read properties of null (reading 'score')\n"
        "    at computeRiskScore (/app/src/services/fraud.js:42:15)\n"
        "    at async FraudController.check (/app/src/controllers/fraudController.js:18:20)\n"
    )

    frames = {f.function_name: f for f in _parse_stacktrace(raw)}

    assert frames["computeRiskScore"].file_path == "/app/src/services/fraud.js"
    assert frames["computeRiskScore"].line_number == 42
    assert frames["check"].file_path == "/app/src/controllers/fraudController.js"
    assert frames["check"].line_number == 18


# ---------------------------------------------------------------------------
# Test 14 — Node/V8 alias frames and colon-bearing internal paths
# ---------------------------------------------------------------------------

def test_parse_stacktrace_node_alias_and_internal_frames():
    """[as alias] must not corrupt the function name; node: paths stay intact."""
    raw = (
        "    at Layer.handle [as handle_request] (/app/node_modules/express/lib/router/layer.js:95:5)\n"
        "    at processTicksAndRejections (node:internal/process/task_queues:95:5)\n"
    )

    frames = {f.function_name: f for f in _parse_stacktrace(raw)}

    assert frames["handle"].file_path == "/app/node_modules/express/lib/router/layer.js"
    assert frames["handle"].line_number == 95
    assert frames["processTicksAndRejections"].file_path == "node:internal/process/task_queues"
    assert frames["processTicksAndRejections"].line_number == 95


# ---------------------------------------------------------------------------
# Test 15 — Node/V8 anonymous frames (no parens) — Express handlers land here
# ---------------------------------------------------------------------------

def test_parse_stacktrace_node_anonymous_frames():
    """Paren-less V8 frames parse as <anonymous> with correct file and line."""
    raw = (
        "    at /app/src/routes/orders.js:23:13\n"
        "    at async /app/src/middleware/auth.js:8:9\n"
    )

    frames = _parse_stacktrace(raw)

    assert len(frames) == 2
    assert all(f.function_name == "<anonymous>" for f in frames)
    assert (frames[0].file_path, frames[0].line_number) == ("/app/src/routes/orders.js", 23)
    assert (frames[1].file_path, frames[1].line_number) == ("/app/src/middleware/auth.js", 8)


# ---------------------------------------------------------------------------
# Test 16 — Java frames (no column) unchanged — hero stacktrace regression guard
# ---------------------------------------------------------------------------

def test_parse_stacktrace_java_unchanged():
    """Simulator-style Java frames must parse exactly as before the V8 fix."""
    raw = (
        "com.hyperion.fraud.RiskEvaluator.evaluateTransaction(RiskEvaluator.java:47)\n"
        "at com.hyperion.fraud.FraudService.evaluate(FraudService.java:112)\n"
    )

    frames = {f.function_name: f for f in _parse_stacktrace(raw)}

    assert frames["evaluateTransaction"].file_path == "RiskEvaluator.java"
    assert frames["evaluateTransaction"].line_number == 47
    assert frames["evaluate"].file_path == "FraudService.java"
    assert frames["evaluate"].line_number == 112


# ---------------------------------------------------------------------------
# Test 17 — Case B smoking gun fires on a Node/V8 stacktrace
# ---------------------------------------------------------------------------

def test_case_b_smoking_gun_node_stacktrace():
    """V8 stacktrace + matching JS code change + LLM 'likely' → smoking gun.

    Before the V8 parsing fix, the column number was captured as the line
    and the line was folded into the file path, so no match type could ever
    fire for Node stacktraces and the smoking gun was unreachable.
    """
    stacktrace = (
        "TypeError: Cannot read properties of null (reading 'score')\n"
        "    at computeRiskScore (/app/src/services/fraud.js:42:15)\n"
        "    at async FraudController.check (/app/src/controllers/fraudController.js:18:20)\n"
    )

    candidate = _make_candidate(priority=0.6, causal_confidence=0.6)
    node = _make_service_node(
        code_access_enabled=True,
        recent_deploys=[_make_deploy(minutes_before=10)],
    )
    graph = nx.DiGraph()
    graph.add_node(_SERVICE_ID, data=node)

    span = _make_error_span(
        exception_type="TypeError",
        exception_message="Cannot read properties of null (reading 'score')",
        stacktrace=stacktrace,
    )

    # start_line=40 → frame line 42 is within EXACT_LINE tolerance (±3)
    code_change = _make_code_change_event(
        func_name="computeRiskScore",
        file_path="src/services/fraud.js",
        start_line=40,
        old_code="function computeRiskScore(txn) {\n  if (!txn.risk) return 0;\n  return txn.risk.score;\n}",
        new_code="function computeRiskScore(txn) {\n  return txn.risk.score;\n}",
    )
    llm_client = _make_llm_client(
        "likely", reasoning="Removed null guard causes TypeError on txn.risk.score"
    )

    result = investigate(
        candidate=candidate,
        node=node,
        graph=graph,
        spans=[span],
        metrics=[],
        baselines={},
        incident_start=_T0,
        code_change_event=code_change,
        llm_client=llm_client,
        iteration=1,
    )

    assert result.priority_score == pytest.approx(PRIORITY_OVERRIDE_SMOKING_GUN)
    st_items = [e for e in result.evidence if e.signal == "stacktrace_match"]
    assert len(st_items) == 1
    assert st_items[0].metadata.get("match_type") == MatchType.EXACT_LINE.value
