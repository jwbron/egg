"""Agent anchor library for post-compaction state recovery.

Provides Pydantic models, atomic file I/O, API sync helpers, and schema
validation for agent anchor files that capture working state at natural
milestones.
"""

from .brc_derive import derive_brc_anchors
from .loader import load_anchor, save_anchor, sync_anchor_to_api
from .models import (
    AgentAnchor,
    AnchorMeta,
    BRCDerivedAnchors,
    BRCState,
    ConditionalAckObligation,
    Decision,
    ErrorEncountered,
    KeyContext,
    OpenNack,
    ProgressItem,
    ReviewEdgeVerdict,
    ReviewVerdict,
    TaskInfo,
)
from .protected_root import RootCaps, render_protected_root
from .validator import check_size_budget, validate_anchor

__all__ = [
    "AgentAnchor",
    "AnchorMeta",
    "BRCDerivedAnchors",
    "BRCState",
    "ConditionalAckObligation",
    "Decision",
    "ErrorEncountered",
    "KeyContext",
    "OpenNack",
    "ProgressItem",
    "ReviewEdgeVerdict",
    "ReviewVerdict",
    "RootCaps",
    "TaskInfo",
    "check_size_budget",
    "derive_brc_anchors",
    "load_anchor",
    "render_protected_root",
    "save_anchor",
    "sync_anchor_to_api",
    "validate_anchor",
]
