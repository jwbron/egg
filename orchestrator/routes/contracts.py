"""Orchestrator contract endpoints.

These endpoints are the authoritative entry point for contract reads
and writes during pipeline execution.  The gateway proxies agent
requests here so that every agent observes the same contract
regardless of which per-agent worktree it is running in (see #1781).

The live contract lives in the *shared* pipeline worktree — not in
per-agent worktrees, which previously caused producers and reviewers
to see divergent copies.  Serialization to the feature branch
continues via ``_commit_statefiles_to_worktree`` at checkpoint
events; the file is already in the right place by the time commits
run, so no dedicated "serialize" step is needed.

URL scheme:
  GET    /api/v1/contracts/<identifier>                 — read
  GET    /api/v1/contracts/<identifier>/exists          — existence
  POST   /api/v1/contracts/<identifier>/mutate          — apply mutation
  POST   /api/v1/contract-mutations/validate            — dry-run
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flask import Blueprint, Response, jsonify, request

# Shared packages live under ../../shared relative to this file.
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

if TYPE_CHECKING:
    from egg_contracts import Contract

# The orchestrator package lives one level up.
_parent_path = Path(__file__).parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

import contract_store  # noqa: E402
from egg_contracts import (  # noqa: E402
    ContractNotFoundError,
    ContractValidationError,
    Role,
    apply_mutation,
    export_contract,
    get_contract_role,
    load_contract,
    save_contract,
    validate_mutation,
)
from egg_contracts import (
    contract_exists as _contract_exists,
)
from lifecycle_auth import require_lifecycle_secret  # noqa: E402

logger = logging.getLogger("orchestrator.contracts")

contracts_bp = Blueprint("contracts", __name__, url_prefix="/api/v1/contracts")
contract_mutations_bp = Blueprint(
    "contract_mutations", __name__, url_prefix="/api/v1/contract-mutations"
)

_VALID_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _coerce_identifier(raw: str) -> int | str:
    """Parse a URL identifier into an int (issue number) or str (pipeline id)."""
    return int(raw) if raw.isdigit() else raw


def _validate_identifier(identifier: int | str) -> tuple[Response, int] | None:
    if isinstance(identifier, int):
        return None
    if not _VALID_IDENTIFIER_RE.match(identifier):
        return _error(
            "Invalid identifier: only alphanumeric characters, hyphens and underscores are allowed",
            400,
        )
    return None


def _error(
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    payload: dict[str, Any] = {"success": False, "message": message}
    if details:
        payload["details"] = details
    return jsonify(payload), status_code


def _success(
    message: str,
    data: dict[str, Any] | None = None,
    source: str | None = None,
) -> tuple[Response, int]:
    payload: dict[str, Any] = {"success": True, "message": message}
    if data is not None:
        payload["data"] = data
    if source is not None:
        payload["source"] = source
    return jsonify(payload), 200


def _resolve_role(value: str) -> Role | None:
    normalized = value.lower()
    try:
        return Role(normalized)
    except ValueError:
        return get_contract_role(normalized)


def _role_from_request() -> Role | None:
    """Read the caller's role from the forwarded header / body / env.

    The gateway forwards the session-verified role via ``X-Egg-Role``
    so the orchestrator need not re-authenticate contract calls.
    """
    header_role = request.headers.get("X-Egg-Role")
    if header_role:
        return _resolve_role(header_role)

    # Body fallback exists for internal/dev callers that bypass the
    # gateway.  The gateway itself strips these fields from forwarded
    # bodies (it sends the verified role via X-Egg-Role instead).
    body = request.get_json(silent=True) or {}
    body_role = body.get("role") or body.get("actor_role")
    if body_role:
        return _resolve_role(body_role)

    env_role = os.environ.get("EGG_AGENT_ROLE")
    if env_role:
        return _resolve_role(env_role)

    return None


def _pipeline_context() -> tuple[str | None, str | None]:
    """Extract pipeline_id and repo hint from the request."""
    body = request.get_json(silent=True) or {}
    pipeline_id = body.get("pipeline_id") or request.args.get("pipeline_id")
    repo_hint = body.get("repo") or request.args.get("repo")
    return pipeline_id, repo_hint


def _worktree_for_request() -> tuple[Path | None, tuple[Response, int] | None]:
    """Resolve the shared pipeline worktree from the request context.

    Returns ``(worktree, error)`` where exactly one is non-None.
    """
    pipeline_id, repo_hint = _pipeline_context()
    if not pipeline_id:
        return None, _error("Missing pipeline_id in request", status_code=400)

    worktree = contract_store.resolve_pipeline_worktree(pipeline_id, repo_hint)
    if worktree is None:
        return None, _error(
            f"Pipeline worktree not found for {pipeline_id}",
            status_code=404,
        )
    return worktree, None


def _branch_read_contract(
    identifier: int | str,
    pipeline_id: str,
) -> Contract | None:
    """Fall back to reading the committed contract from the pipeline's branch.

    Used by the GET paths when the shared worktree has been pruned — the
    ``.egg-state/contracts/<pipeline_id>.json`` file committed to the
    feature branch is authoritative after the pipeline's final commit
    and stays accessible via ``git show`` for the life of the PR.

    Returns the loaded ``Contract`` on success, ``None`` when the
    pipeline record can't be located or the branch has no such file.
    """
    # Lazy import: routes/__init__.py pulls in flask/state_store at
    # import time; importing at module top would make contracts.py
    # depend on initialisation order. Matches the pattern used by
    # signals.py / phases.py / decisions.py.
    from routes import get_state_store_for_pipeline
    from state_store import InvalidPipelineIdError, PipelineNotFoundError

    try:
        store, pipeline = get_state_store_for_pipeline(pipeline_id)
    except PipelineNotFoundError, InvalidPipelineIdError:
        return None
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "Branch-read fallback could not resolve pipeline",
            extra={"pipeline_id": pipeline_id, "error": str(exc)},
        )
        return None

    # The pipeline tip is pushed to ``egg/<id>/work`` so slice integration
    # branches can coexist as siblings — see
    # :func:`routes.pipelines._ensure_pipeline_work_ref` for the rationale
    # (#2399). The fallback shape mirrors the actual remote ref the
    # contract was committed to.
    branch = pipeline.branch or f"egg/{pipeline_id}/work"
    return contract_store.load_contract_from_branch(identifier, store.repo_path, branch)


@contracts_bp.route("/<identifier>", methods=["GET"])
def get_contract(identifier: str) -> tuple[Response, int]:
    """Return the contract for *identifier*.

    Prefers the live shared-worktree copy. When the worktree has already
    been pruned (typical after the pipeline reaches PR / complete), falls
    back to reading the committed contract from the pipeline's branch so
    post-hoc callers — PR review, auditing, follow-up analysis — can
    still retrieve it (#1977).
    """
    ident = _coerce_identifier(identifier)
    validation_error = _validate_identifier(ident)
    if validation_error:
        return validation_error

    include_audit = request.args.get("include_audit_log", "false").lower() == "true"

    worktree, error = _worktree_for_request()
    if error is None:
        assert worktree is not None
        try:
            with contract_store.lock_for(ident):
                contract = load_contract(ident, worktree)
        except ContractNotFoundError:
            return _error(
                f"Contract for {'#' + str(ident) if isinstance(ident, int) else ident} not found",
                status_code=404,
            )
        except ContractValidationError as exc:
            return _error(f"Contract validation failed: {exc}", status_code=500)

        return _success(
            "Contract retrieved",
            data=export_contract(contract, include_audit_log=include_audit),
            source="worktree",
        )

    # Worktree missing — try the branch before surfacing 404.
    pipeline_id, _repo_hint = _pipeline_context()
    if not pipeline_id:
        return error

    try:
        contract = _branch_read_contract(ident, pipeline_id)
    except ContractValidationError as exc:
        return _error(f"Contract validation failed: {exc}", status_code=500)
    if contract is None:
        return error
    return _success(
        "Contract retrieved",
        data=export_contract(contract, include_audit_log=include_audit),
        source="branch",
    )


@contracts_bp.route("/<identifier>/exists", methods=["GET"])
def contract_exists(identifier: str) -> tuple[Response, int]:
    ident = _coerce_identifier(identifier)
    validation_error = _validate_identifier(ident)
    if validation_error:
        return validation_error

    worktree, error = _worktree_for_request()
    if error is None:
        assert worktree is not None
        exists = _contract_exists(ident, worktree)
        return _success(
            "Contract exists" if exists else "Contract does not exist",
            data={"exists": exists},
            source="worktree",
        )

    # Worktree missing — check the branch. "Does this pipeline ever
    # have a contract?" is a reasonable archival query (#1977).
    pipeline_id, _repo_hint = _pipeline_context()
    if not pipeline_id:
        return error

    try:
        contract = _branch_read_contract(ident, pipeline_id)
    except ContractValidationError as exc:
        return _error(f"Contract validation failed: {exc}", status_code=500)
    if contract is None:
        return error
    return _success(
        "Contract exists",
        data={"exists": True},
        source="branch",
    )


@contracts_bp.route("/<identifier>/mutate", methods=["POST"])
def mutate_contract(identifier: str) -> tuple[Response, int]:
    """Apply a role-validated mutation to the live contract."""
    ident = _coerce_identifier(identifier)
    validation_error = _validate_identifier(ident)
    if validation_error:
        return validation_error

    body = request.get_json()
    if body is None:
        return _error("Missing request body")
    if not isinstance(body, dict):
        return _error("Request body must be a JSON object")

    field_path = body.get("field_path")
    new_value = body.get("new_value", ...)  # sentinel: allow explicit None
    if not field_path:
        return _error("Missing field_path")
    if new_value is ...:
        return _error("Missing new_value")

    role = _role_from_request()
    if role is None:
        return _error(
            "Cannot determine agent role for contract mutation",
            status_code=403,
        )

    worktree, error = _worktree_for_request()
    if error:
        return error
    assert worktree is not None

    actor = body.get("actor", "agent")
    reason = body.get("reason")

    with contract_store.lock_for(ident):
        try:
            contract = load_contract(ident, worktree)
        except ContractNotFoundError:
            return _error(
                f"Contract for {'#' + str(ident) if isinstance(ident, int) else ident} not found",
                status_code=404,
            )
        except ContractValidationError as exc:
            return _error(f"Contract validation failed: {exc}", status_code=500)

        result = apply_mutation(
            contract=contract,
            role=role,
            actor=actor,
            field_path=field_path,
            new_value=new_value,
            reason=reason,
        )

        if not result.success:
            logger.warning(
                "Contract mutation rejected",
                extra={
                    "identifier": str(ident),
                    "role": role.value,
                    "field_path": field_path,
                    "error": result.message,
                    "error_kind": result.error_kind,
                },
            )
            # 403 only for authorization rejections; value/path errors
            # are 400 so a client doesn't retry them as if a different
            # role might succeed (#2495).
            status_code = 403 if result.error_kind == "authorization" else 400
            return _error(
                result.message,
                status_code=status_code,
                details={"role": role.value, "field_path": field_path},
            )

        assert result.contract is not None
        try:
            save_contract(result.contract, worktree)
        except Exception as exc:
            logger.error(
                "Failed to save contract",
                extra={"identifier": str(ident), "error": str(exc)},
            )
            return _error(f"Failed to save contract: {exc}", status_code=500)

    logger.info(
        "Contract mutation applied",
        extra={
            "identifier": str(ident),
            "role": role.value,
            "actor": actor,
            "field_path": field_path,
        },
    )

    _audit_confirmed_assignee_reassignment(result.contract, field_path, new_value, actor)

    return _success(
        "Mutation applied successfully",
        data={"contract": export_contract(result.contract, include_audit_log=False)},
    )


_TASK_ROLE_PATH_RE = re.compile(r"^phases\.(\d+)\.tasks\.\d+\.role$")


def _audit_confirmed_assignee_reassignment(
    contract: Contract,
    field_path: str,
    new_value: Any,
    actor: str,
) -> None:
    """Log when a ``tasks.*.role`` mutation targets a confirmed producer (#3124).

    Reassigning a task to a producer that has already CONFIRMED its
    slice consensus used to deadlock silently: the assignee could never
    re-enter WORKING, and the #3114 completeness gate held the slice
    open over the undelivered row. The next-action route now reopens
    the producer's participation on its next poll
    (``routes.consensus._maybe_reopen_confirmed_producer``), so this
    hook is observability only — it makes the reassign-after-confirm
    moment visible in the orchestrator log instead of leaving the
    reopen to be discovered post-hoc. Best-effort: any failure here
    must not affect the mutation that already succeeded.
    """
    try:
        match = _TASK_ROLE_PATH_RE.match(field_path)
        if not match or not isinstance(new_value, str):
            return

        pipeline_id, _repo_hint = _pipeline_context()
        if not pipeline_id:
            return

        slice_idx = int(match.group(1))
        slices = list(getattr(contract, "slices", None) or [])
        slice_id = getattr(slices[slice_idx], "id", None) if slice_idx < len(slices) else None

        from peer_consensus import get_peer_consensus_tracker

        tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
        if tracker is None:
            return
        if new_value in tracker.confirmed_roles:
            logger.warning(
                "Task reassigned to an already-confirmed producer; its consensus "
                "participation will reopen on its next next-action poll (#3124)",
                extra={
                    "pipeline_id": pipeline_id,
                    "slice_id": slice_id,
                    "field_path": field_path,
                    "new_assignee": new_value,
                    "actor": actor,
                    # The reopen is contingent on the producer wrapper
                    # still polling next-action. If the wrapper already
                    # exited after CONFIRMED (the #3124 motivating
                    # scenario for slices that closed before the
                    # mutation landed), no poll will fire and the
                    # operator-grade fallback
                    # (POST /api/v1/contracts/<id>/tasks/<task>/complete
                    # or the executable "Mark task <id> complete" HITL
                    # option) is the only path forward.
                    "reopen_requires_active_poll": True,
                },
            )
    except Exception:  # pragma: no cover — observability must not break mutations
        logger.debug("Confirmed-assignee reassignment audit skipped", exc_info=True)


@contracts_bp.route("/<identifier>/tasks/<task_id>/complete", methods=["POST"])
@require_lifecycle_secret
def operator_complete_task(identifier: str, task_id: str) -> tuple[Response, int]:
    """Mark a contract task complete as an audited operator action (#3124).

    The in-band remediation for a task no live agent is permitted to
    satisfy (e.g. reassigned to a producer that already CONFIRMED).
    Replaces the ``kubectl exec`` + ``EGG_AGENT_ROLE=<role>
    egg-contract complete-task`` impersonation workaround.

    URL params:
        identifier: Pipeline id (preferred) or issue number.
        task_id: Contract task id (e.g. ``TASK-2-3``).

    Request body (all optional)::

        {"commit": "<sha evidence>",
         "reason": "<why the operator is attesting completion>"}

    Auth: lifecycle-secret guarded — operator/host surface only.
    Sandbox agents keep using ``mcp__task__complete`` under their own
    role; this route is not proxied by the gateway.
    """
    # Lazy import — operator_actions pulls in contract_store locking and
    # egg_contracts; importing per-call mirrors the heavy-dependency
    # seams used by routes/decisions.py.
    from operator_actions import OperatorActionError, complete_task_as_operator

    ident = _coerce_identifier(identifier)
    validation_error = _validate_identifier(ident)
    if validation_error:
        return validation_error

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return _error("Request body must be a JSON object")

    pipeline_id = body.get("pipeline_id") or (str(ident) if not isinstance(ident, int) else None)
    if not pipeline_id:
        return _error(
            "Cannot resolve pipeline worktree: pass the pipeline id as the "
            "URL identifier or in the request body as 'pipeline_id'",
            status_code=400,
        )

    # Scope the audit actor under the ``operator:`` namespace so a
    # body-provided string can't be set to a sandbox-agent role like
    # ``coder``/``reviewer`` and read in the audit log as an agent
    # mutation. The route is lifecycle-secret guarded so this is
    # belt-and-braces, but it keeps the audit trail honest if two
    # different host-side tools call this endpoint with their own
    # actor suffixes.
    actor_suffix = body.get("actor")
    actor = f"operator:{actor_suffix}" if actor_suffix else "operator"

    try:
        result = complete_task_as_operator(
            pipeline_id,
            task_id,
            commit=body.get("commit"),
            reason=body.get("reason", ""),
            actor=actor,
            issue_number=ident if isinstance(ident, int) else None,
        )
    except OperatorActionError as exc:
        return _error(exc.message, status_code=exc.status_code)

    logger.info(
        "Operator marked task complete",
        pipeline_id=pipeline_id,
        task_id=task_id,
        actor=actor,
        source=getattr(request, "egg_source", "unknown"),
    )

    return _success(f"Task {task_id} marked complete by operator", data=result)


@contract_mutations_bp.route("/validate", methods=["POST"])
def validate_contract_mutation() -> tuple[Response, int]:
    """Dry-run a mutation and report whether it would be accepted.

    Role permissions are independent of contract contents, so this
    endpoint doesn't take an identifier — it just validates role
    against field_path/new_value via the shared validator.
    """
    body = request.get_json()
    if body is None:
        return _error("Missing request body")
    if not isinstance(body, dict):
        return _error("Request body must be a JSON object")

    field_path = body.get("field_path")
    new_value = body.get("new_value", ...)
    if not field_path:
        return _error("Missing field_path")
    if new_value is ...:
        return _error("Missing new_value")

    role = _role_from_request()
    if role is None:
        return _error("Cannot determine agent role", status_code=403)

    result = validate_mutation(role, field_path, new_value)
    if result.valid:
        return _success("Mutation allowed")
    return _error(
        result.message,
        status_code=403,
        details={
            "role": role.value,
            "field_path": result.field_path,
            "required_role": result.required_role,
        },
    )
