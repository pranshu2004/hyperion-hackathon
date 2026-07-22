"""
Unit tests for reasoning/scorer.py.

Tests that the confidence scorer produces correct scores given known signal
combinations, and that every score is backed by at least one EvidenceItem.
"""

from __future__ import annotations

import pytest

from reasoning.scorer import score

# TODO: add test cases for:
#   - high confidence (stacktrace match + deploy + LLM → score >= 0.70)
#   - medium confidence (latency spike only → score in [0.15, 0.35))
#   - low confidence (minimal signals → score < 0.15)
#   - assert all returned candidates carry non-empty evidence lists
