"""
Failure scenario injector.

Defines GroundTruth and FailureScenario dataclasses used to declare named,
self-contained test scenarios for validating the Hyperion reasoning engine.
Each scenario bundles simulation inputs and ground truth labels for end-to-end
test validation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from core.change_event import ConfigChangeEvent, DeployEvent, FeatureFlagChangeEvent
from core.code_change import (
    CodeChangeEvent,
    FileChange,
    FunctionChange,
    MatchType,
)
from core.metric import Metric
from core.nodes import NodeType
from core.span import Span
from ingestion.simulator.metric_generator import (
    generate_normal as generate_normal_metrics,
    generate_failure_from_scenario,
)
from ingestion.simulator.trace_generator import FailureMode


@dataclass
class GroundTruth:
    # Primary attribution
    root_cause_node_id: str
    domain: str
    expected_min_confidence: float

    # Operation and signal attribution
    root_operation: str | None
    root_signal: str | None
    failure_mechanism: str | None

    # Fields with defaults must follow all required fields
    propagation_path: list[str] = field(default_factory=list)
    expected_evidence_signals: list[str] = field(default_factory=list)

    # Case B attribution (code intelligence — only for application_code domain)
    expected_stacktrace_match_file: str | None = None
    expected_stacktrace_match_function: str | None = None
    expected_match_type: str | None = None
    expected_case_b_min_confidence: float | None = None

    # Causal validation
    expected_counterfactual_necessary: bool = True
    expected_min_fit_score: float = 0.60


@dataclass
class FailureScenario:
    # Identity
    name: str
    domain: str
    description: str

    # Simulation parameters
    failure_mode: FailureMode

    # Ground truth for validation (required — must precede fields with defaults)
    ground_truth: GroundTruth

    trace_count: int = 100
    failure_start_offset_seconds: int = 300

    # Change events to inject alongside traces
    deploy_events: list[DeployEvent] = field(default_factory=list)
    config_change_events: list[ConfigChangeEvent] = field(default_factory=list)
    feature_flag_events: list[FeatureFlagChangeEvent] = field(default_factory=list)
    code_change_event: CodeChangeEvent | None = None


# ---------------------------------------------------------------------------
# SC-001 — Hero scenario: bad deploy on fraud-service
# ---------------------------------------------------------------------------


def _make_sc001_deploy_event(deploy_time: datetime) -> DeployEvent:
    """Deploy event for fraud-service v1.8.1."""
    return DeployEvent(
        event_id="deploy-sc001-fraud-v181",
        service_id="fraud-service",
        timestamp=deploy_time,
        version="v1.8.1",
        author="alice@hyperion-demo.com",
        diff_summary=(
            "Refactored RiskEvaluator.evaluateTransaction to use new "
            "TokenizerV2. Removed legacy null-safety fallback on tokenizer "
            "field. Assumed TokenizerV2 always non-null after DI wiring."
        ),
        deploy_scope=["fraud-service"],
        tags={"pipeline": "github-actions", "env": "production"},
    )


def _make_sc001_code_change_event(deploy_time: datetime) -> CodeChangeEvent:
    """Code change for the commit that introduced the null check removal. Used for Case B."""
    changed_function = FunctionChange(
        function_name="evaluateTransaction",
        file_path="src/main/java/com/hyperion/fraud/RiskEvaluator.java",
        change_type="modified",
        language="java",
        start_line=40,
        end_line=58,
        old_code=(
            "public RiskScore evaluateTransaction(Transaction tx) {\n"
            "    if (this.tokenizer == null) {\n"
            "        logger.warn('Tokenizer null, using legacy fallback');\n"
            "        return legacyEvaluate(tx);\n"
            "    }\n"
            "    String token = this.tokenizer.generate(tx.getCardData());\n"
            "    return this.riskModel.score(token, tx);\n"
            "}"
        ),
        new_code=(
            "public RiskScore evaluateTransaction(Transaction tx) {\n"
            "    // TokenizerV2 guaranteed via DI — removed legacy fallback\n"
            "    String token = this.tokenizer.generate(tx.getCardData());\n"
            "    return this.riskModel.score(token, tx);\n"
            "}"
        ),
    )

    file_change = FileChange(
        file_path="src/main/java/com/hyperion/fraud/RiskEvaluator.java",
        language="java",
        change_type="modified",
        raw_diff=(
            "--- a/src/main/java/com/hyperion/fraud/RiskEvaluator.java\n"
            "+++ b/src/main/java/com/hyperion/fraud/RiskEvaluator.java\n"
            "@@ -40,12 +40,8 @@ public class RiskEvaluator {\n"
            "     public RiskScore evaluateTransaction(Transaction tx) {\n"
            "-        if (this.tokenizer == null) {\n"
            "-            logger.warn('Tokenizer null, using legacy fallback');\n"
            "-            return legacyEvaluate(tx);\n"
            "-        }\n"
            "         String token = this.tokenizer.generate(tx.getCardData());\n"
            "         return this.riskModel.score(token, tx);\n"
            "     }"
        ),
        functions_changed=[changed_function],
    )

    return CodeChangeEvent(
        event_id="code-sc001-fraud-v181",
        service_id="fraud-service",
        commit_sha="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        timestamp=deploy_time,
        repo_url="https://github.com/hyperion-demo/fraud-service",
        branch="main",
        files_changed=[file_change],
        author="alice@hyperion-demo.com",
        commit_message="refactor: migrate RiskEvaluator to TokenizerV2, remove legacy null fallback",
        deploy_event_id="deploy-sc001-fraud-v181",
    )


def build_sc001(base_time: datetime | None = None) -> FailureScenario:
    """
    SC-001: Hero scenario — bad deploy on fraud-service cascades to
    payment-service, checkout-service, api-gateway.
    Domain: application_code. Case B enabled.
    """
    if base_time is None:
        base_time = datetime.now(timezone.utc)

    # Deploy at NOW-20min; failure starts immediately.
    # Window starts at base_time-1h, so offset = 40min = 2400s.
    deploy_time = base_time - timedelta(minutes=20)

    from ingestion.simulator.trace_generator import get_hero_failure_mode

    return FailureScenario(
        name="SC-001-hero-deploy-fraud-service",
        domain="application_code",
        description=(
            "Deploy v1.8.1 on fraud-service removed a null safety check "
            "on the tokenizer field in RiskEvaluator.evaluateTransaction. "
            "When TokenizerV2 DI wiring fails under load, tokenizer is null "
            "and NullPointerException is thrown. fraud-service p99 latency "
            "spikes 8.5x, error rate reaches 85%. Cascades to payment-service "
            "then checkout-service then api-gateway."
        ),
        failure_mode=get_hero_failure_mode(),
        trace_count=100,
        failure_start_offset_seconds=2400,
        deploy_events=[_make_sc001_deploy_event(deploy_time)],
        code_change_event=_make_sc001_code_change_event(deploy_time),
        ground_truth=GroundTruth(
            root_cause_node_id="fraud-service",
            domain="application_code",
            propagation_path=[
                "fraud-service",
                "payment-service",
                "checkout-service",
                "api-gateway",
            ],
            expected_min_confidence=0.88,
            root_operation="evaluate_transaction",
            root_signal="deploy_v1.8.1",
            failure_mechanism="null_pointer",
            expected_evidence_signals=[
                "deploy_event",
                "exception_type",
                "stacktrace_match",
                "error_rate_spike",
                "latency_spike",
            ],
            expected_stacktrace_match_file=(
                "src/main/java/com/hyperion/fraud/RiskEvaluator.java"
            ),
            expected_stacktrace_match_function="evaluateTransaction",
            expected_match_type="exact_line",
            expected_case_b_min_confidence=0.92,
            expected_counterfactual_necessary=True,
            expected_min_fit_score=0.75,
        ),
    )


# ---------------------------------------------------------------------------
# SC-002 — Database slow query: postgres-payments
# ---------------------------------------------------------------------------


def build_sc002() -> FailureScenario:
    """
    SC-002: postgres-payments slow query spike causes payment-service
    timeouts, cascading to checkout-service and api-gateway.
    Domain: database. No deploy or config change in window.
    """
    failure_mode = FailureMode(
        affected_node_ids=[
            "postgres-payments",
            "payment-service",
            "checkout-service",
        ],
        latency_multiplier=12.0,
        error_rate=0.70,
        error_type="QueryTimeoutException",
        failure_version=None,
    )

    return FailureScenario(
        name="SC-002-db-slow-query-postgres-payments",
        domain="database",
        description=(
            "postgres-payments query latency spikes 12x baseline. "
            "No deploy or config change in the incident window. "
            "payment-service connection pool exhausted waiting on slow "
            "INSERT payments queries. Cascades to checkout-service then "
            "api-gateway. Root cause: intrinsic database degradation "
            "(likely missing index or lock contention on payments table)."
        ),
        failure_mode=failure_mode,
        trace_count=100,
        failure_start_offset_seconds=300,
        ground_truth=GroundTruth(
            root_cause_node_id="postgres-payments",
            domain="database",
            propagation_path=[
                "postgres-payments",
                "payment-service",
                "checkout-service",
                "api-gateway",
            ],
            expected_min_confidence=0.70,
            root_operation="INSERT payments",
            root_signal="db_latency_spike",
            failure_mechanism="lock_contention",
            expected_evidence_signals=[
                "db_latency_spike",
                "slow_query",
                "error_rate_spike",
                "no_deploy_in_window",
            ],
            expected_counterfactual_necessary=True,
            expected_min_fit_score=0.65,
        ),
    )


# ---------------------------------------------------------------------------
# SC-003 — External dependency failure: stripe-api 503s
# ---------------------------------------------------------------------------


def build_sc003() -> FailureScenario:
    """
    SC-003: stripe-api returning 503s causes fraud-service failures,
    cascading to payment-service, checkout-service, api-gateway.
    Domain: dependency. No deploy or config change in window.
    """
    failure_mode = FailureMode(
        affected_node_ids=[
            "stripe-api",
            "fraud-service",
            "payment-service",
            "checkout-service",
        ],
        latency_multiplier=6.0,
        error_rate=0.90,
        error_type="ConnectionTimeoutException",
        failure_version=None,
    )

    return FailureScenario(
        name="SC-003-external-dep-stripe-api-503",
        domain="dependency",
        description=(
            "stripe-api returning HTTP 503 Service Unavailable. "
            "No deploy or config change in window — this is an upstream "
            "provider outage. fraud-service calls to stripe-api time out. "
            "payment-service fails since fraud evaluation cannot complete. "
            "Cascades to checkout-service then api-gateway. "
            "Pattern: sustained 503s affecting all callers of stripe-api, "
            "not a single caller misconfiguration."
        ),
        failure_mode=failure_mode,
        trace_count=100,
        failure_start_offset_seconds=300,
        ground_truth=GroundTruth(
            root_cause_node_id="stripe-api",
            domain="dependency",
            propagation_path=[
                "stripe-api",
                "fraud-service",
                "payment-service",
                "checkout-service",
                "api-gateway",
            ],
            expected_min_confidence=0.65,
            root_operation="POST /v1/charges",
            root_signal="http_503_pattern",
            failure_mechanism="upstream_503",
            expected_evidence_signals=[
                "http_503_pattern",
                "external_dep_latency",
                "no_deploy_in_window",
                "single_upstream_caller",
            ],
            expected_counterfactual_necessary=True,
            expected_min_fit_score=0.60,
        ),
    )


# ---------------------------------------------------------------------------
# SC-004 — Configuration / feature flag: fraud scoring flag flip
# ---------------------------------------------------------------------------


def build_sc004(base_time: datetime | None = None) -> FailureScenario:
    """
    SC-004: Feature flag enable-new-fraud-scoring flipped on fraud-service
    causes immediate error burst. Domain: configuration.
    """
    if base_time is None:
        base_time = datetime.now(timezone.utc)

    failure_mode = FailureMode(
        affected_node_ids=[
            "fraud-service",
            "payment-service",
            "checkout-service",
        ],
        latency_multiplier=4.0,
        error_rate=0.75,
        error_type="ConfigurationException",
        failure_version=None,
    )

    # incident_start = window_start + 3420s = base_time - 3min
    # flag 3 min before incident_start = base_time - 6min → within CONFIG_LOOKBACK_MINUTES=15
    flag_event = FeatureFlagChangeEvent(
        event_id="flag-sc004-fraud-scoring",
        service_id="fraud-service",
        timestamp=base_time - timedelta(minutes=6),
        flag_key="enable-new-fraud-scoring",
        old_value="false",
        new_value="true",
        author="bob@hyperion-demo.com",
        tags={"env": "production", "rollout": "100pct"},
    )

    return FailureScenario(
        name="SC-004-config-feature-flag-fraud-scoring",
        domain="configuration",
        description=(
            "Feature flag enable-new-fraud-scoring flipped to true on "
            "fraud-service 3 minutes before incident. New fraud scoring "
            "model has a configuration dependency that is missing in "
            "production. fraud-service error rate rises to 75% immediately "
            "after flag flip. Cascades to payment-service then "
            "checkout-service. Flag flip is the root cause — no deploy "
            "in window."
        ),
        failure_mode=failure_mode,
        trace_count=100,
        failure_start_offset_seconds=3420,
        feature_flag_events=[flag_event],
        ground_truth=GroundTruth(
            root_cause_node_id="fraud-service",
            domain="configuration",
            propagation_path=[
                "fraud-service",
                "payment-service",
                "checkout-service",
                "api-gateway",
            ],
            expected_min_confidence=0.70,
            root_operation="evaluate_transaction",
            root_signal="flag_flip_enable-new-fraud-scoring",
            failure_mechanism="config_regression",
            expected_evidence_signals=[
                "feature_flag_change",
                "error_rate_spike",
                "temporal_proximity",
            ],
            expected_counterfactual_necessary=True,
            expected_min_fit_score=0.62,
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_all_scenarios(base_time: datetime | None = None) -> list[FailureScenario]:
    """
    Returns all four MVP failure scenarios in order.
    Used by the test suite to run full validation.
    """
    if base_time is None:
        base_time = datetime.now(timezone.utc)
    return [
        build_sc001(base_time),
        build_sc002(),
        build_sc003(),
        build_sc004(base_time),
    ]


def get_scenario(name: str, base_time: datetime | None = None) -> FailureScenario:
    """
    Returns a single scenario by name.
    Raises ValueError if name not found.
    """
    scenarios = {s.name: s for s in get_all_scenarios(base_time)}
    if name not in scenarios:
        raise ValueError(
            f"Unknown scenario: '{name}'. "
            f"Available: {sorted(scenarios.keys())}"
        )
    return scenarios[name]


def get_scenario_names() -> list[str]:
    """Returns all available scenario names."""
    return [s.name for s in get_all_scenarios()]


def run_scenario(
    scenario: FailureScenario,
    base_time: datetime | None = None,
) -> dict:
    """
    Runs a scenario end-to-end through the simulator.
    Generates traces AND metrics, returns bundle ready for ingestion.

    Returns:
        {
            "scenario_name": str,
            "domain": str,
            "traces": list[dict],
            "metrics": list[dict],
            "deploy_events": list,
            "config_change_events": list,
            "feature_flag_events": list,
            "code_change_event": CodeChangeEvent | None,
            "ground_truth": GroundTruth,
            "incident_start": datetime,
        }
    """
    from ingestion.simulator.trace_generator import generate_failure

    if base_time is None:
        base_time = datetime.now(timezone.utc)

    window_start = base_time - timedelta(hours=1)
    incident_start = window_start + timedelta(seconds=scenario.failure_start_offset_seconds)

    traces = generate_failure(
        failure_mode=scenario.failure_mode,
        count=scenario.trace_count,
        start_time=base_time - timedelta(hours=1),
        failure_start_offset_seconds=scenario.failure_start_offset_seconds,
    )

    metrics = generate_failure_from_scenario(
        scenario_failure_mode=scenario.failure_mode,
        duration_seconds=3600,
        scrape_interval_seconds=60,
        start_time=base_time - timedelta(hours=1),
        failure_start_offset_seconds=scenario.failure_start_offset_seconds,
    )

    return {
        "scenario_name": scenario.name,
        "domain": scenario.domain,
        "traces": traces,
        "metrics": metrics,
        "deploy_events": scenario.deploy_events,
        "config_change_events": scenario.config_change_events,
        "feature_flag_events": scenario.feature_flag_events,
        "code_change_event": scenario.code_change_event,
        "ground_truth": scenario.ground_truth,
        "incident_start": incident_start,
    }
