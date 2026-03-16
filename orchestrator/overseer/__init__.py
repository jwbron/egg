"""Overseer agent package for pipeline health monitoring.

This package provides the LLM-assisted second tier of pipeline health
monitoring.  The orchestrator handles deterministic tripwire rules; the
overseer handles ambiguous escalations using Haiku classifiers and
Sonnet/Opus decision-makers.

Key modules:
    classifier      -- Haiku-tier classification functions
    decision_maker  -- Sonnet/Opus-tier decision functions
    issue_filer     -- GitHub diagnostic issue filing
    self_monitor    -- Overseer self-health tracking
    monitor         -- Main monitoring loop
"""

from overseer.classifier import (
    check_alignment,
    classify_error,
    classify_stall,
    detect_loop,
)
from overseer.decision_maker import (
    compose_redirect_message,
    decide_corrective_action,
    decide_escalation_level,
)
from overseer.issue_filer import file_diagnostic_issue
from overseer.monitor import OverseerMonitor
from overseer.self_monitor import OverseerSelfMonitor

__all__ = [
    "check_alignment",
    "classify_error",
    "classify_stall",
    "compose_redirect_message",
    "decide_corrective_action",
    "decide_escalation_level",
    "detect_loop",
    "file_diagnostic_issue",
    "OverseerMonitor",
    "OverseerSelfMonitor",
]
