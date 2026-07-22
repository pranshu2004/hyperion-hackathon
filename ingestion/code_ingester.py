from __future__ import annotations

import json
import logging
import re
import warnings
from datetime import datetime, timezone
from tree_sitter import Language, Parser

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import tree_sitter_languages

from core.code_change import CodeChangeEvent, FileChange, FunctionChange, StacktraceMatch

logger = logging.getLogger(__name__)

_EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py":   "python",
    ".java": "java",
    ".go":   "go",
    ".ts":   "typescript",
    ".tsx":  "typescript",
    ".rb":   "ruby",
    ".js":   "javascript",
    ".jsx":  "javascript",
    ".cpp":  "cpp",
    ".c":    "c",
    ".cs":   "c_sharp",
    ".rs":   "rust",
}

_FUNCTION_NODE_TYPES: dict[str, list[str]] = {
    "python":     ["function_definition", "async_function_definition"],
    "java":       ["method_declaration", "constructor_declaration"],
    "go":         ["function_declaration", "method_declaration"],
    "typescript": ["function_declaration", "method_definition", "arrow_function"],
    "ruby":       ["method", "singleton_method"],
    "javascript": ["function_declaration", "method_definition", "arrow_function"],
}
_DEFAULT_FUNCTION_NODE_TYPES = ["function_declaration", "function_definition"]

# Node types that carry the function/method name (fallbacks beyond "identifier")
_NAME_NODE_TYPES = {"identifier", "property_identifier", "field_identifier"}


def _detect_language(file_path: str) -> str | None:
    """
    Detect programming language from file extension.

    Args:
        file_path: Full or relative file path.
                   e.g. "src/main/java/com/hyperion/fraud/RiskEvaluator.java"

    Returns:
        Language name string if recognized (e.g. "java", "python").
        None if extension not in _EXTENSION_TO_LANGUAGE.
        Never raises.
    """
    try:
        dot = file_path.rfind(".")
        if dot == -1:
            return None
        ext = file_path[dot:]
        return _EXTENSION_TO_LANGUAGE.get(ext)
    except Exception:
        return None


def _get_parser(language: str) -> Parser | None:
    """
    Get a configured tree-sitter Parser for the given language.

    Args:
        language: Language name e.g. "java", "python".

    Returns:
        Configured Parser instance if language supported.
        None with a warning log if language not available.
        Never raises.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return tree_sitter_languages.get_parser(language)
    except Exception:
        logger.warning("tree-sitter parser not available for language: %s", language)
        return None


def _extract_functions(
    source_code: str,
    language: str,
    file_path: str,
) -> list[tuple[str, int, int]]:
    """
    Extract all function/method definitions from source code.
    Returns list of (function_name, start_line, end_line) tuples.
    Lines are 1-indexed.
    """
    if not source_code:
        return []

    parser = _get_parser(language)
    if parser is None:
        return []

    try:
        tree = parser.parse(source_code.encode("utf-8", errors="replace"))
    except Exception:
        logger.warning("tree-sitter parse failed for %s", file_path)
        return []

    target_types = set(_FUNCTION_NODE_TYPES.get(language, _DEFAULT_FUNCTION_NODE_TYPES))
    results: list[tuple[str, int, int]] = []

    def _first_name_child(node) -> str | None:
        for child in node.children:
            if child.type in _NAME_NODE_TYPES:
                return child.text.decode("utf-8", errors="replace")
        return None

    def _walk(node) -> None:
        if node.type in target_types:
            name = _first_name_child(node)
            if name:
                start_line = node.start_point[0] + 1
                end_line = node.end_point[0] + 1
                results.append((name, start_line, end_line))
        for child in node.children:
            _walk(child)

    try:
        _walk(tree.root_node)
    except Exception:
        logger.warning("AST traversal failed for %s", file_path)
        return []

    return results


def _find_functions_in_range(
    functions: list[tuple[str, int, int]],
    changed_lines: set[int],
) -> list[tuple[str, int, int]]:
    """
    Filter functions whose line range overlaps with changed_lines.

    A function overlaps if ANY of its lines appear in changed_lines.
    i.e. any line L where start_line <= L <= end_line is in changed_lines.

    Args:
        functions:     Output of _extract_functions()
        changed_lines: Set of line numbers that changed in the diff.

    Returns:
        Subset of functions that overlap with changed_lines.
        Preserves original order.
        Never raises.
    """
    if not functions or not changed_lines:
        return []

    try:
        return [
            fn for fn in functions
            if any(fn[1] <= line <= fn[2] for line in changed_lines)
        ]
    except Exception:
        return []


def _parse_hunk_header(header: str) -> tuple[int, int, int, int]:
    """
    Parse a unified diff hunk header line.
    Format: @@ -old_start,old_count +new_start,new_count @@

    Examples:
      "@@ -40,12 +40,8 @@"  → (40, 12, 40, 8)
      "@@ -1 +1,5 @@"       → (1, 1, 1, 5)  (count defaults to 1)

    Returns (old_start, old_count, new_start, new_count).
    Raises ValueError on unparseable header.
    """
    m = re.search(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', header)
    if not m:
        raise ValueError(f"Cannot parse hunk header: {header!r}")
    old_start = int(m.group(1))
    old_count = int(m.group(2)) if m.group(2) is not None else 1
    new_start = int(m.group(3))
    new_count = int(m.group(4)) if m.group(4) is not None else 1
    return old_start, old_count, new_start, new_count


def _extract_changed_lines(
    hunk_lines: list[str],
    new_start: int,
) -> tuple[set[int], str, str]:
    """
    Extract changed line numbers and reconstruct old/new file content
    from a list of hunk lines (the lines after the @@ header).

    Args:
        hunk_lines: Lines of the hunk (context, additions, removals).
                    Lines starting with '+' are additions.
                    Lines starting with '-' are removals.
                    Lines starting with ' ' are context (unchanged).
        new_start:  The starting line number in the new file.

    Returns:
        (changed_lines, old_content, new_content) where:
          changed_lines: set of line numbers (1-indexed, in new file)
                         that were added or removed
          old_content:   reconstructed old version (context + removals)
          new_content:   reconstructed new version (context + additions)

    Never raises.
    """
    changed: set[int] = set()
    old_parts: list[str] = []
    new_parts: list[str] = []
    new_line = new_start

    try:
        for line in hunk_lines:
            if not line:
                old_parts.append("")
                new_parts.append("")
                new_line += 1
            elif line[0] == "+":
                changed.add(new_line)
                new_parts.append(line[1:])
                new_line += 1
            elif line[0] == "-":
                changed.add(new_line)
                old_parts.append(line[1:])
            else:
                text = line[1:] if line[0] == " " else line
                old_parts.append(text)
                new_parts.append(text)
                new_line += 1
    except Exception:
        pass

    return changed, "\n".join(old_parts), "\n".join(new_parts)


def parse_diff(
    raw_diff: str,
    file_path: str,
    language: str,
) -> list[FunctionChange]:
    """
    Parse a unified diff string for a single file and return
    FunctionChange objects for each modified function.

    Args:
        raw_diff: Unified diff string for one file.
                  May contain multiple hunks (multiple @@ sections).
        file_path: Path of the file being diffed.
                   Used for FunctionChange.file_path.
        language:  Programming language (e.g. "java", "python").

    Returns:
        List of FunctionChange objects, one per modified function.
        Empty list if raw_diff is empty/None, language not supported,
        no functions overlap with changed lines, or parse fails.
        Never raises.
    """
    if not raw_diff:
        return []

    if _get_parser(language) is None:
        return []

    try:
        diff_lines = raw_diff.splitlines()
        hunk_indices = [i for i, l in enumerate(diff_lines) if l.startswith("@@")]
        if not hunk_indices:
            return []

        all_changed: set[int] = set()
        old_changed: set[int] = set()
        # Absolute line number → text for new and old file reconstruction
        new_line_map: dict[int, str] = {}
        old_line_map: dict[int, str] = {}

        for hi, hunk_start in enumerate(hunk_indices):
            header = diff_lines[hunk_start]
            hunk_end = hunk_indices[hi + 1] if hi + 1 < len(hunk_indices) else len(diff_lines)
            hunk_body = diff_lines[hunk_start + 1:hunk_end]

            try:
                old_start, _, new_start, _ = _parse_hunk_header(header)
            except ValueError:
                continue

            changed, _, _ = _extract_changed_lines(hunk_body, new_start)
            all_changed |= changed

            new_line = new_start
            old_line = old_start
            for line in hunk_body:
                if not line:
                    new_line_map[new_line] = ""
                    old_line_map[old_line] = ""
                    new_line += 1
                    old_line += 1
                elif line[0] == "+":
                    new_line_map[new_line] = line[1:]
                    new_line += 1
                elif line[0] == "-":
                    old_line_map[old_line] = line[1:]
                    old_changed.add(old_line)
                    old_line += 1
                else:
                    text = line[1:] if line[0] == " " else line
                    new_line_map[new_line] = text
                    old_line_map[old_line] = text
                    new_line += 1
                    old_line += 1

        if not all_changed:
            return []

        def _build_content(line_map: dict[int, str]) -> str:
            if not line_map:
                return ""
            max_line = max(line_map.keys())
            return "\n".join(line_map.get(i, "") for i in range(1, max_line + 1))

        new_content = _build_content(new_line_map)
        old_content = _build_content(old_line_map)

        new_funcs = _extract_functions(new_content, language, file_path)
        old_funcs = _extract_functions(old_content, language, file_path)
        old_func_by_name: dict[str, tuple[int, int]] = {
            name: (s, e) for name, s, e in old_funcs
        }

        changed_funcs = _find_functions_in_range(new_funcs, all_changed)

        result: list[FunctionChange] = []
        new_func_names: set[str] = set()
        for name, start, end in changed_funcs:
            new_func_names.add(name)
            new_code = "\n".join(new_line_map.get(i, "") for i in range(start, end + 1))

            if name in old_func_by_name:
                os_, oe_ = old_func_by_name[name]
                old_code: str | None = "\n".join(
                    old_line_map.get(i, "") for i in range(os_, oe_ + 1)
                )
                change_type = "modified"
            else:
                old_code = None
                change_type = "added"

            result.append(FunctionChange(
                function_name=name,
                file_path=file_path,
                change_type=change_type,
                language=language,
                start_line=start,
                end_line=end,
                old_code=old_code,
                new_code=new_code,
            ))

        deleted_funcs = _find_functions_in_range(old_funcs, old_changed)
        for name, start, end in deleted_funcs:
            if name not in new_func_names:
                old_code = "\n".join(
                    old_line_map.get(i, "") for i in range(start, end + 1)
                )
                result.append(FunctionChange(
                    function_name=name,
                    file_path=file_path,
                    change_type="deleted",
                    language=language,
                    start_line=start,
                    end_line=end,
                    old_code=old_code,
                    new_code=None,
                ))

        return result

    except Exception:
        logger.warning("parse_diff failed for %s", file_path)
        return []


def _build_file_changes(
    file_diffs: list[dict],
) -> list[FileChange]:
    """
    Build FileChange objects from a list of file diff dicts.

    Each dict: {"file_path", "raw_diff", "change_type", "old_path"}.
    Skips files where raw_diff is empty.
    Never raises.
    """
    if not file_diffs:
        return []

    result: list[FileChange] = []
    try:
        for fd in file_diffs:
            try:
                file_path = fd.get("file_path", "")
                raw_diff = fd.get("raw_diff", "")
                change_type = fd.get("change_type", "modified")
                old_path = fd.get("old_path")

                if not raw_diff:
                    continue

                language = _detect_language(file_path)
                functions_changed: list[FunctionChange] = []
                if language and change_type in ("modified", "added"):
                    functions_changed = parse_diff(raw_diff, file_path, language)

                result.append(FileChange(
                    file_path=file_path,
                    language=language or "unknown",
                    change_type=change_type,
                    raw_diff=raw_diff,
                    functions_changed=functions_changed,
                    old_path=old_path,
                ))
            except Exception:
                continue
    except Exception:
        pass

    return result


def _parse_timestamp(ts: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            dt = datetime.strptime(ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def ingest_raw_diff(
    file_diffs: list[dict],
    metadata: dict,
    service_id: str,
) -> CodeChangeEvent | None:
    """
    Build a CodeChangeEvent from raw diff data + metadata.

    Required metadata fields: event_id, commit_sha, timestamp, repo_url, branch.
    Returns None with warning log if required fields are missing or timestamp
    is unparseable. Never raises.
    """
    try:
        required = ("event_id", "commit_sha", "timestamp", "repo_url", "branch")
        for field in required:
            if not metadata.get(field):
                logger.warning("ingest_raw_diff: missing required field %r", field)
                return None

        ts = _parse_timestamp(metadata["timestamp"])
        if ts is None:
            logger.warning("ingest_raw_diff: unparseable timestamp %r", metadata["timestamp"])
            return None

        files_changed = _build_file_changes(file_diffs or [])

        return CodeChangeEvent(
            event_id=metadata["event_id"],
            service_id=service_id,
            commit_sha=metadata["commit_sha"],
            timestamp=ts,
            repo_url=metadata["repo_url"],
            branch=metadata["branch"],
            files_changed=files_changed,
            author=metadata.get("author"),
            commit_message=metadata.get("commit_message"),
            deploy_event_id=metadata.get("deploy_event_id"),
        )
    except Exception:
        logger.warning("ingest_raw_diff failed for service %s", service_id)
        return None


def ingest_github(
    payload: dict,
    service_id: str,
    file_diffs: list[dict],
) -> CodeChangeEvent | None:
    """
    Parse a GitHub push webhook payload into a CodeChangeEvent.

    Extracts commit SHA, branch, repo URL, author, message, and timestamp
    from the standard GitHub push event structure.
    Returns None with warning if payload is malformed. Never raises.
    """
    try:
        if not payload:
            return None
        commit_sha = payload.get("after") or payload["head_commit"]["id"]
        branch = payload["ref"].replace("refs/heads/", "")
        repo_url = payload["repository"]["html_url"]
        author = payload.get("pusher", {}).get("email")
        head = payload["head_commit"]
        commit_message = head["message"]
        timestamp = head["timestamp"]
        event_id = f"github-{commit_sha[:12]}"

        metadata = {
            "event_id": event_id,
            "commit_sha": commit_sha,
            "timestamp": timestamp,
            "repo_url": repo_url,
            "branch": branch,
            "author": author,
            "commit_message": commit_message,
        }
        return ingest_raw_diff(file_diffs, metadata, service_id)
    except Exception:
        logger.warning("ingest_github: malformed payload for service %s", service_id)
        return None


def ingest_gitlab(
    payload: dict,
    service_id: str,
    file_diffs: list[dict],
) -> CodeChangeEvent | None:
    """
    Parse a GitLab push webhook payload into a CodeChangeEvent.

    Extracts commit SHA, branch, repo URL, author, message, and timestamp
    from the standard GitLab push event structure.
    Returns None with warning if payload is malformed. Never raises.
    """
    try:
        if not payload:
            return None
        commit_sha = payload["checkout_sha"]
        branch = payload["ref"].replace("refs/heads/", "")
        repo_url = payload["project"]["web_url"]
        author = payload.get("user_email")
        first_commit = payload["commits"][0]
        commit_message = first_commit["message"]
        timestamp = first_commit["timestamp"]
        event_id = f"gitlab-{commit_sha[:12]}"

        metadata = {
            "event_id": event_id,
            "commit_sha": commit_sha,
            "timestamp": timestamp,
            "repo_url": repo_url,
            "branch": branch,
            "author": author,
            "commit_message": commit_message,
        }
        return ingest_raw_diff(file_diffs, metadata, service_id)
    except Exception:
        logger.warning("ingest_gitlab: malformed payload for service %s", service_id)
        return None
