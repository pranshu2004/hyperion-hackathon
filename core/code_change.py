from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MatchType(Enum):
    EXACT_LINE     = "exact_line"
    FUNCTION_MATCH = "function_match"
    FILE_MATCH     = "file_match"


@dataclass
class StacktraceFrame:
    file_path: str
    function_name: str
    line_number: int
    module: str | None   = None
    raw_text: str | None = None


@dataclass
class FunctionChange:
    function_name: str
    file_path: str
    change_type: str
    language: str
    start_line: int
    end_line: int
    old_code: str | None = None
    new_code: str | None = None


@dataclass
class FileChange:
    file_path: str
    language: str
    change_type: str
    raw_diff: str
    functions_changed: list[FunctionChange] = field(default_factory=list)
    old_path: str | None = None


@dataclass
class StacktraceMatch:
    span_id: str
    frame: StacktraceFrame
    function_change: FunctionChange
    match_type: MatchType
    confidence: float


@dataclass
class CodeChangeEvent:
    event_id: str
    service_id: str
    commit_sha: str
    timestamp: datetime
    repo_url: str
    branch: str
    files_changed: list[FileChange]
    author: str | None          = None
    commit_message: str | None  = None
    deploy_event_id: str | None = None
    stacktrace_matches: list[StacktraceMatch] = field(default_factory=list)
