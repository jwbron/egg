"""Main overseer monitoring loop.

Implements the continuous poll-classify-decide-act cycle that runs for
the lifetime of a pipeline.  The monitor queries the orchestrator for
progress events and health alerts, routes anomalies through the
classifier tier, and executes corrective actions via the decision tier.

----

Decomposed into a sub-package (#3312, slice-8) following the canonical
method-modules-on-class pattern (docs/guides/decomposition-pattern.md §c).
This barrel is the **stable public API**: ``OverseerMonitor`` keeps its
identity on the ``overseer.monitor`` module path, and every external symbol /
``unittest.mock.patch`` target (``OverseerMonitor``, ``_accepts_kwarg``,
``_get_state_store``, ``file_diagnostic_issue``) resolves through here.
``OverseerMonitor`` method bodies live in underscore-prefixed submodules as
module-level functions taking ``self`` explicitly; they are bound back onto
the class below. Submodule functions reach barrel-patched module globals
(``file_diagnostic_issue``, ``_get_state_store``, ``_HUMAN_WORDS``,
``_ACTION_WORDS``, ``_TERMINAL_STATUSES``, ``_accepts_kwarg``) via
``import overseer.monitor as _pkg``, so those patch seams keep working.
"""

from __future__ import annotations

import inspect
import logging
from collections import deque
from pathlib import Path
from typing import Any

from models import PipelineStatus
from overseer.issue_filer import (  # noqa: F401 — file_diagnostic_issue re-export / _pkg patch seam
    IssueDedupLedger,
    file_diagnostic_issue,
)
from overseer.self_monitor import OverseerSelfMonitor

try:
    from state_store import get_state_store as _get_state_store
except ImportError:
    _get_state_store = None

logger = logging.getLogger(__name__)

# Keywords for the escalation safety net — match common LLM phrasings
# indicating human intervention is needed.
_HUMAN_WORDS = ("human", "manual", "operator")
_ACTION_WORDS = ("intervention", "attention", "review", "required", "needed", "escalat")

# Pipeline status string values that indicate a terminal state. Derived from
# the canonical PipelineStatus.terminal() set so the overseer shares one
# definition with sse.py / state_store.py / routes (#3174 review).
_TERMINAL_STATUSES = {status.value for status in PipelineStatus.terminal()}


def _accepts_kwarg(func: Any, name: str) -> bool:
    """Return True if *func* accepts a keyword argument named *name*.

    Uses :func:`inspect.signature` to inspect the callable. ``True`` is
    returned when the parameter is declared explicitly or absorbed by a
    ``**kwargs`` catch-all. Callables whose signature can't be
    introspected (e.g. some C-implemented builtins) default to ``True``
    on the assumption that they accept arbitrary kwargs — matching how
    :class:`unittest.mock.AsyncMock` and friends behave at the call site.
    """
    try:
        sig = inspect.signature(func)
    except TypeError, ValueError:
        return True
    params = sig.parameters
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class _DefaultConfig:
    """Fallback config when no PipelineConfig is provided."""

    overseer_poll_interval_seconds: int = 30
    overseer_max_redirects_before_escalation: int = 2
    overseer_rerun_min_work_seconds: int = 60
    overseer_hitl_propagation_timeout_seconds: int = 300
    overseer_infra_error_dedup_window_seconds: int = 300


# -- method-body submodules (bound onto OverseerMonitor below) -------------
# Imported here (not lazily) so the ``import overseer.monitor as _pkg`` barrel
# access inside them resolves once this module finishes initialising.
from . import (
    _alerting,
    _anomaly_checks,
    _consensus_stall,
    _decision_tier,
    _escalation,
    _lifecycle,
    _poll,
    _queries,
)


class OverseerMonitor:
    """Main overseer monitoring loop.

    Polls the orchestrator for progress events and health alerts,
    classifies anomalies, decides on corrective actions, and executes
    them.

    Args:
        pipeline_id: The pipeline to monitor.
        config: Pipeline configuration (uses defaults if ``None``).
        classifier: Optional override for the classifier module (for testing).
        decision_maker: Optional override for the decision_maker module (for testing).
    """

    def __init__(
        self,
        pipeline_id: str,
        config: Any = None,
        classifier: Any = None,
        decision_maker: Any = None,
    ) -> None:
        self.pipeline_id = pipeline_id
        self.config = config or _DefaultConfig()
        self.self_monitor = OverseerSelfMonitor()
        self._running = False
        # agent_role -> bounded deque of escalations (keep last 50 per agent)
        self._escalation_history: dict[str, deque] = {}
        # Generation token (#2270 slice-5): reset on orchestrator pod recycle
        # via ``reset_generation``. Every escalation record is stamped with the
        # generation that produced it, and redirect-history reads filter to the
        # current generation, so stale escalation state from a prior generation
        # can never cascade into a fresh run's corrective decisions.
        self.generation: int = 0

        # Allow dependency injection for testing
        self._classifier = classifier
        self._decision_maker = decision_maker

        # Post-consensus stall deduplication
        self._post_consensus_stall_reported = False
        self._post_consensus_stall_first_seen: float | None = None

        # Incomplete consensus stall tracking (#1471)
        self._incomplete_consensus_first_seen: float | None = None
        self._incomplete_consensus_blocking: frozenset[str] | None = None
        self._incomplete_consensus_nudged = False
        self._incomplete_consensus_hitl_created = False
        # Track the absolute start time for activity deferral cap (#1609)
        self._incomplete_consensus_absolute_start: float | None = None

        # Re-run anomaly deduplication (decision IDs already flagged)
        self._rerun_anomaly_reported: set[str] = set()

        # Status inconsistency deduplication
        self._status_inconsistency_reported = False
        self._status_inconsistency_first_seen: float | None = None

        # HITL resolution propagation tracking
        self._hitl_resolution_pending: dict[str, float] = {}  # decision_id -> first-seen ts
        self._hitl_resolution_verified: set[str] = set()
        self._hitl_resolution_alerted: set[str] = set()  # failures already alerted

        # Orchestrator unreachability tracking
        self._consecutive_orch_failures: int = 0
        self._orch_unreachable_threshold: int = 3  # escalate after N consecutive failures

        # Infrastructure error deduplication between Tier 1 and Tier 2 (#1489)
        # Maps (agent_id, error_hash) -> timestamp of first escalation
        self._infra_error_dedup: dict[tuple[str, str], float] = {}

        # Two-tier diagnostic-issue dedup (#2270 §5/§6, slice-9): a single
        # persistent ledger so a repeated "issue" corrective action for the
        # same anomaly never files a duplicate GitHub issue. Reset on a
        # generation recycle so a fresh run starts with a clean slate.
        self._issue_dedup_ledger = IssueDedupLedger()

        # Agent restart tracking: the spawner (via the REST API) is the single
        # source of truth for restart counts and limit enforcement.  We track
        # which agents have been reported as exhausted (limit exceeded) so we
        # can escalate to a phase restart HITL when 2+ agents are exhausted.
        # See issue #1695 items 2 & 3.
        self._agents_restart_exhausted: set[str] = set()

        # Cross-phase consistency: track phase transitions and deduplication
        self._last_phase_name: str | None = None
        self._cross_phase_checked: set[tuple[str, str]] = set()

        # Oversight logging to .egg-state/oversight/
        self._oversight_dir = self._resolve_oversight_dir()
        self._jsonl_path: Path | None = None
        if self._oversight_dir:
            try:
                self._oversight_dir.mkdir(parents=True, exist_ok=True)
                self._jsonl_path = self._oversight_dir / f"{pipeline_id}-oversight.jsonl"
            except OSError:
                # Non-critical: oversight logging is optional
                logger.debug("Cannot create oversight dir %s", self._oversight_dir)
                self._oversight_dir = None

    # -- Oversight logging + lifecycle (bodies in _lifecycle.py)
    _resolve_oversight_dir = staticmethod(_lifecycle._resolve_oversight_dir)
    _log_oversight_event = _lifecycle._log_oversight_event
    write_health_summary = _lifecycle.write_health_summary
    start = _lifecycle.start
    adjudicate = _lifecycle.adjudicate
    stop = _lifecycle.stop
    reset_escalation_history = _lifecycle.reset_escalation_history
    reset_generation = _lifecycle.reset_generation
    generate_health_summary = _lifecycle.generate_health_summary

    # -- Classifier / decision tier (bodies in _decision_tier.py)
    _classify_stall = _decision_tier._classify_stall
    _check_decision_consistency_cls = _decision_tier._check_decision_consistency_cls
    _resolve_tier_model = _decision_tier._resolve_tier_model
    _decide_corrective_action = _decision_tier._decide_corrective_action
    _decide_escalation_level = _decision_tier._decide_escalation_level

    # -- Core poll cycle (body in _poll.py)
    _poll_cycle = _poll._poll_cycle

    # -- Escalation handling + action execution (bodies in _escalation.py)
    handle_escalation = _escalation.handle_escalation
    _execute_action = _escalation._execute_action
    _execute_restart_agent = _escalation._execute_restart_agent
    _handle_restart_failure = _escalation._handle_restart_failure

    # -- Orchestrator / CLI query tier (bodies in _queries.py)
    _run_cli = _queries._run_cli
    _query_progress = _queries._query_progress
    _query_health_alerts = _queries._query_health_alerts
    _poll_escalation_messages = _queries._poll_escalation_messages
    _query_pipeline_data = _queries._query_pipeline_data
    _query_consensus_status = _queries._query_consensus_status
    _query_current_phase = _queries._query_current_phase
    _query_decisions = _queries._query_decisions
    _query_contract_data = _queries._query_contract_data
    _query_container_list = _queries._query_container_list
    _query_container_logs = _queries._query_container_logs

    # -- Consensus-stall detection (bodies in _consensus_stall.py)
    _load_pipeline_for_transition_check = _consensus_stall._load_pipeline_for_transition_check
    _check_post_consensus_stall = _consensus_stall._check_post_consensus_stall
    _blocking_agents_are_active = _consensus_stall._blocking_agents_are_active
    _get_recent_proposal_age = _consensus_stall._get_recent_proposal_age
    _check_incomplete_consensus_stall = _consensus_stall._check_incomplete_consensus_stall
    _reset_incomplete_consensus_tracking = _consensus_stall._reset_incomplete_consensus_tracking

    # -- Deterministic health checks + alert filtering (bodies in _anomaly_checks.py)
    _filter_current_phase_agents = _anomaly_checks._filter_current_phase_agents
    _check_orchestrator_reachability = _anomaly_checks._check_orchestrator_reachability
    _check_rerun_anomaly = _anomaly_checks._check_rerun_anomaly
    _check_status_consistency = _anomaly_checks._check_status_consistency
    _check_hitl_resolution_propagation = _anomaly_checks._check_hitl_resolution_propagation
    _check_cross_phase_consistency = _anomaly_checks._check_cross_phase_consistency

    # -- Alerting / messaging + infra-error dedup (bodies in _alerting.py)
    _infra_error_hash = _alerting._infra_error_hash
    _is_infra_error_deduped = _alerting._is_infra_error_deduped
    _record_infra_error_escalation = _alerting._record_infra_error_escalation
    _cleanup_infra_error_dedup = _alerting._cleanup_infra_error_dedup
    _broadcast_alert = _alerting._broadcast_alert
    _send_message = _alerting._send_message
    _resolve_alert = _alerting._resolve_alert
    _create_hitl_decision = _alerting._create_hitl_decision
    _create_phase_restart_decision = _alerting._create_phase_restart_decision
    _send_slack_notification = _alerting._send_slack_notification
