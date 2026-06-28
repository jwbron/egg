"""
Signal endpoints for sandbox callbacks.

Provides REST endpoints for sandboxes to report completion, progress updates,
errors, and the full Broadcast-Review-Converge consensus signal family back to
the orchestrator.

This package is the stable public API surface for the ``signals`` blueprint
(file-decomposition pattern, #3312). Per decision-8 the ``@signals_bp.route``
decorators stay here on thin wrappers that delegate to private submodules:

- ``_responses``           — make_error_response / make_success_response
- ``_validation``          — BRC content + route-version + plan/artifact
  validators
- ``_lifecycle``           — non-consensus signal handlers
  (complete/progress/error/heartbeat/readiness) + commit-verification helpers
- ``_consensus_verdicts``  — propose / ack / nack / withdraw + their helpers
- ``_consensus_confirm``   — confirmed / excuse-producer / resolve-obligation /
  producer-push + their helpers
- ``_dispatch``            — handle_signal + handle_batch_signals bodies

The barrel re-exports every externally-referenced or test-patched symbol so
``from routes.signals import _foo`` and ``patch("routes.signals._foo")`` keep
resolving. Submodules invoke the barrel-patched dependencies through this
package module (``import routes.signals as _pkg``) so the existing
``patch("routes.signals.<name>")`` seams stay effective unchanged — the names
were module globals of the pre-split file, and package-attribute access
reproduces that lookup exactly.
"""

import subprocess  # noqa: F401 — re-exported patch seam (patch("routes.signals.subprocess"))
import sys
from pathlib import Path

from flask import Blueprint, Response

# Add parent directory (orchestrator/) to path for imports. The sub-package
# lives one level deeper than the original module, so the walk-up gains a
# ``.parent`` versus the pre-split file.
_parent_path = Path(__file__).parent.parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

# Add shared directory to path for logging
_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


# Barrel-patched dependencies + the role map. Imported here so
# ``routes.signals.<name>`` is a single patch point; the private submodules call
# the patched seams via ``_pkg`` so ``patch("routes.signals.get_state_store")``
# (etc.) keeps working.
from egg_contracts import load_contract, save_contract  # noqa: E402,F401
from egg_contracts.agent_roles import AgentRole as ContractAgentRole  # noqa: E402
from egg_contracts.orchestrator import create_orchestrator  # noqa: E402,F401
from handoffs import save_agent_output  # noqa: E402,F401
from models import AgentRole  # noqa: E402
from routes import (  # noqa: E402,F401 — shared helpers + patch seams
    get_repo_path,
    resolve_repo_path_for_pipeline,
    resolve_worktree_path,
)
from state_store import get_state_store  # noqa: E402,F401

logger = get_logger("orchestrator.signals")

# Mapping from orchestrator AgentRole to egg_contracts AgentRole.
# Roles not in this mapping (e.g. REFINER, REVIEWER_REFINE) don't
# participate in contract orchestration.
_AGENT_ROLE_TO_CONTRACT_ROLE: dict[AgentRole, ContractAgentRole] = {
    AgentRole.CODER: ContractAgentRole.CODER,
    AgentRole.TESTER: ContractAgentRole.TESTER,
    AgentRole.DOCUMENTER: ContractAgentRole.DOCUMENTER,
    AgentRole.ARCHITECT: ContractAgentRole.ARCHITECT,
    AgentRole.TASK_PLANNER: ContractAgentRole.TASK_PLANNER,
    AgentRole.RISK_ANALYST: ContractAgentRole.RISK_ANALYST,
    AgentRole.REVIEWER_CODE: ContractAgentRole.REVIEWER_CODE,
    AgentRole.REVIEWER_CONTRACT: ContractAgentRole.REVIEWER_CONTRACT,
    AgentRole.REVIEWER_AGENT_DESIGN: ContractAgentRole.REVIEWER_AGENT_DESIGN,
}

signals_bp = Blueprint("signals", __name__, url_prefix="/api/v1/pipelines")

# Private submodules. Imported after the blueprint + shared deps + logger exist
# so the ``import routes.signals as _pkg`` barrel access inside them resolves
# against a populated package module.
from . import (  # noqa: E402,F401
    _consensus_confirm,
    _consensus_verdicts,
    _dispatch,
    _lifecycle,
    _responses,
    _validation,
)

# Re-export the stable public / test-patched surface (pattern §a/§d).
from ._consensus_confirm import (  # noqa: E402,F401
    _existing_confirmed_for_role,
    _write_consensus_confirmed_marker,
    handle_consensus_confirmed_signal,
    handle_consensus_excuse_producer_signal,
    handle_consensus_producer_push_signal,
    handle_consensus_resolve_obligation_signal,
)
from ._consensus_verdicts import (  # noqa: E402,F401
    _contract_completeness_rejection,
    _emit_ready_to_confirm_nudges,
    _get_re_review_priming_text,
    _resolve_pipeline_phase,
    _resolve_reviewer_delta_range,
    _stale_version_rejection,
    handle_consensus_ack_signal,
    handle_consensus_nack_signal,
    handle_consensus_propose_signal,
    handle_consensus_withdraw_signal,
)
from ._lifecycle import (  # noqa: E402,F401
    _SIGTERM_PATTERN,
    _check_branch_progress,
    _commit_object_resolvable,
    _gateway_fetch_tracking_ref,
    _is_sigterm_after_completion,
    _verify_commit_on_branch,
    handle_complete_signal,
    handle_error_signal,
    handle_heartbeat_signal,
    handle_progress_signal,
    handle_readiness_signal,
)
from ._responses import (  # noqa: E402,F401
    make_error_response,
    make_success_response,
)
from ._validation import (  # noqa: E402,F401
    _ARTIFACT_HUMAN_LABEL,
    _BRC_BOILERPLATE,
    _BRC_CONDITION_KINDS,
    _BRC_CONDITION_MIN_LEN,
    _BRC_MIN_CONTENT_LEN,
    _artifact_human_label,
    _require_route_version,
    _validate_brc_content,
    _validate_plan_extensions,
    _validate_plan_proposal,
    _validate_producer_artifacts,
    _validate_tester_check_coverage,
)

# ---- Route registrations -------------------------------------------------
# Decision-8: decorators stay in __init__.py on thin wrappers; the bodies live
# in the private submodules above.


@signals_bp.route("/<pipeline_id>/signal", methods=["POST"])
def handle_signal(pipeline_id: str) -> tuple[Response, int]:
    return _dispatch.handle_signal(pipeline_id)


@signals_bp.route("/<pipeline_id>/signal/batch", methods=["POST"])
def handle_batch_signals(pipeline_id: str) -> tuple[Response, int]:
    return _dispatch.handle_batch_signals(pipeline_id)
