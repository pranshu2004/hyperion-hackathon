"""
reasoning/domain_rca — Domain RCA engines for reasoning.

One engine per node type:
    service.py     — ServiceNode (code/change + config/change engines)
    database.py    — DatabaseNode
    dependency.py  — ExternalDepNode
    queue.py       — QueueNode (stub)

Orchestration (dispatch + neighbour scan) lives in orchestrator.py.
"""

from .orchestrator import investigate_all

__all__ = ["investigate_all"]
