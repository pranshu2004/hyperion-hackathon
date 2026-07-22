"""
RCA output schema utilities.

from_feedback() builds the feedback update dict consumed by the dashboard
feedback endpoint. _sanitize() is a shared helper for numpy scalar conversion.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _sanitize(v):
    """Recursively convert numpy scalars/arrays to native Python types."""
    try:
        import numpy as np
        if isinstance(v, np.bool_):
            return bool(v)
        if isinstance(v, np.integer):
            return int(v)
        if isinstance(v, np.floating):
            return float(v)
        if isinstance(v, np.ndarray):
            return v.tolist()
    except ImportError:
        pass
    if isinstance(v, dict):
        return {k: _sanitize(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_sanitize(i) for i in v]
    return v


def from_feedback(
    incident_id: str,
    correct: bool | None,
    notes: str | None = None,
) -> dict:
    """
    Build a feedback update dict for the dashboard feedback endpoint.
    Never raises.
    """
    try:
        return {
            "incident_id": incident_id,
            "feedback": {
                "correct":      correct,
                "notes":        notes,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            },
        }
    except Exception:
        return {
            "incident_id": incident_id,
            "feedback": {
                "correct":      correct,
                "notes":        notes,
                "submitted_at": "",
            },
        }
