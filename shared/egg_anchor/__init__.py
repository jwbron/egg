"""Agent anchor library for post-compaction state recovery.

Provides Pydantic models, atomic file I/O, API sync helpers, and schema
validation for agent anchor files that capture working state at natural
milestones.
"""

from .loader import load_anchor, save_anchor, sync_anchor_to_api
from .models import (
    AgentAnchor,
    AnchorMeta,
    BRCState,
    Decision,
    ErrorEncountered,
    KeyContext,
    ProgressItem,
    TaskInfo,
)
from .validator import check_size_budget, validate_anchor

__all__ = [
    "AgentAnchor",
    "AnchorMeta",
    "BRCState",
    "Decision",
    "ErrorEncountered",
    "KeyContext",
    "ProgressItem",
    "TaskInfo",
    "check_size_budget",
    "load_anchor",
    "save_anchor",
    "sync_anchor_to_api",
    "validate_anchor",
]
