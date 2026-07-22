"""
Unit tests for reasoning/localizer.py.

Tests the fault localizer against known failure scenarios. Each test provides
a synthetic graph + anomalous nodes and asserts that the expected root cause
node appears in the candidate list at the expected inclusion reason.
"""

from __future__ import annotations

import pytest

from reasoning.localizer import localize

# TODO: add test cases for:
#   - single-service latency spike (root cause is origin service)
#   - cascading failure (root cause is upstream, not the most-erroring downstream)
#   - database failure propagating to multiple callers
#   - external dependency timeout
#   - missing data / partial graph (graceful degradation)
