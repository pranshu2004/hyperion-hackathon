"""
Single source of truth for all data contracts in reasoning/.

Every other file in reasoning/ imports from here. No logic lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from core.nodes import NodeType


class RCADomain(str, Enum):
    APPLICATION_CODE = "application_code"
    DATABASE         = "database"
    DEPENDENCY       = "dependency"
    CONFIGURATION    = "configuration"
    QUEUE            = "queue"
    UNKNOWN          = "unknown"


class EvidenceStrength(str, Enum):
    STRONG   = "strong"
    MODERATE = "moderate"
    WEAK     = "weak"


class RCAVerdict(str, Enum):
    ROOT_CAUSE_FOUND    = "root_cause_found"
    TOP_HYPOTHESES      = "top_hypotheses"
    MULTI_FACTOR        = "multi_factor"
    NO_ROOT_CAUSE_FOUND = "no_root_cause_found"


class HintSignal(str, Enum):
    SPAN_ERRORS          = "span_errors"
    CHANGE_EVENT         = "change_event"
    MISSING_TELEMETRY    = "missing_telemetry"
    SILENT_DEGRADATION   = "silent_degradation"
    ERROR_CODE_PATTERN   = "error_code_pattern"  # 503s, 429s etc.
    SUB_THRESHOLD_METRIC = "sub_threshold_metric"


class HintDirection(str, Enum):
    PREDECESSOR = "predecessor"
    SUCCESSOR   = "successor"


class HintStrength(str, Enum):
    STRONG   = "strong"
    MODERATE = "moderate"


class InclusionReason(str, Enum):
    METRIC_ANOMALY     = "metric_anomaly"
    # CHANGE_EVENT here describes why a node was included in the candidate list —
    # distinct from HintSignal.CHANGE_EVENT which describes what triggered a neighbour scan hint.
    CHANGE_EVENT       = "change_event"
    INVESTIGATION_HINT = "investigation_hint"


class DataQuality(str, Enum):
    HIGH   = "high"
    MEDIUM = "medium"
    LOW    = "low"


class FailureType(str, Enum):
    UNKNOWN                  = "unknown"
    CODE_REGRESSION          = "code_regression"
    RUNTIME_ERROR            = "runtime_error"
    APPLICATION_ERROR        = "application_error"
    CONFIG_REGRESSION        = "config_regression"
    FEATURE_FLAG_REGRESSION  = "feature_flag_regression"
    DB_SLOW_QUERY            = "db_slow_query"
    DB_CONNECTION_EXHAUSTION = "db_connection_exhaustion"
    DB_ERROR                 = "db_error"
    SLOW_RESPONSE            = "slow_response"
    UPSTREAM_OUTAGE          = "upstream_outage"
    RATE_LIMITED             = "rate_limited"
    AUTH_FAILURE             = "auth_failure"
    TIMEOUT                  = "timeout"
    EXTERNAL_DEP_FAILURE     = "external_dep_failure"
    QUEUE_ERROR              = "queue_error"


@dataclass(frozen=True)
class InvestigationHint:
    node_id: str
    signal: HintSignal
    direction: HintDirection
    hint_strength: HintStrength
    reason: str
    suggesting_node_id: str
    iteration_found: int


@dataclass(frozen=True)
class EvidenceItem:
    # signal: Controlled vocabulary — not yet promoted to enum while domain
    # engines are stabilising. Values are defined by each domain engine.
    # Promote to EvidenceSignal enum once all domain engines are complete.
    signal: str
    finding: str           # plain English, specific enough for an SRE to act on
    strength: EvidenceStrength
    metadata: dict[str, str]  # structured machine-readable; all values must be str


@dataclass
class CandidateNode:
    node_id: str
    node_type: NodeType
    inclusion_reason: InclusionReason
    is_potential_origin: bool
    priority_score: float = 0.0


@dataclass
class RankedCandidate:
    candidate: CandidateNode
    causal_confidence: float  # [0, 1]; preserved for frontend display only, never influences final RCA confidence


@dataclass
class EnrichedCandidate:
    node_id: str
    node_type: NodeType
    domain: RCADomain
    failure_type: FailureType
    evidence: list[EvidenceItem]
    is_potential_origin: bool
    priority_score: float      # updated by domain RCA findings
    causal_confidence: float   # preserved from RankedCandidate; never used for scoring
    iteration_found: int


@dataclass
class ArbiterResult:
    adjusted_candidates: list[EnrichedCandidate]
    multi_factor_flagged: bool
    residual_unexplained_impact: float  # [0, 1]
    explanation: str


@dataclass
class DomainRCAResult:
    candidates: list[EnrichedCandidate]
    hints: list[InvestigationHint]


@dataclass
class ScoredCandidate:
    node_id: str
    node_type: NodeType
    domain: RCADomain
    failure_type: FailureType
    evidence: list[EvidenceItem]
    domain_confidence: float   # [0, 1], from domain-specific scorer; primary confidence number
    causal_confidence: float   # [0, 1], preserved for display only
    rank: int
    propagation_path: list[str]
    iteration_found: int
    analysis_complete: bool    # False if candidate entered late via hint and was not fully investigated before termination


@dataclass
class RCAResult:
    incident_id: str
    verdict: RCAVerdict
    root_causes: list[ScoredCandidate]
    narrative: str              # plain language explanation produced by LLM
    all_candidates: list[ScoredCandidate]
    iterations_run: int
    analyzed_at: datetime
    data_quality: DataQuality
    multi_factor_explanation: str | None = None          # only populated for MULTI_FACTOR verdict
    weak_signals: list[str] = field(default_factory=list)  # populated for NO_ROOT_CAUSE_FOUND
    fix_suggestion: str = ""   # LLM-generated remediation steps; "" when LLM unavailable
