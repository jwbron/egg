"""File-restriction self-check + impasse reporting handlers (#2529).

Two cheap handlers an agent calls when its assigned task looks
structurally impossible:

- ``check_file_restriction(req)`` — pure local read against
  ``shared/egg_restrictions/patterns.py``. No gateway round-trip; the
  pattern registry is statically resolvable inside the sandbox image.
- ``report_impasse(req)`` — persists a typed
  :class:`egg_contracts.Impasse` under ``AgentOutput.impasse`` (the
  same JSON file used for ``handoff_data`` today). The orchestrator
  scans for impasses post-phase and routes — see
  ``orchestrator/impasse_routing.py``.

The agent should call ``check_file_restriction`` *before* burning
tokens on exploration, and ``report_impasse`` once it has decided the
task is impossible — never together, and never alongside a code
commit. Once impasse is reported the agent should exit cleanly; the
orchestrator will either delegate the task to ``suggested_role`` or
escalate to HITL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from egg_agent_tools.handlers._gateway import (
    get_agent_role,
    get_contract_identifier,
    get_repo_path,
)
from egg_agent_tools.handlers.errors import HandlerError

_VALID_CATEGORIES = {"wrong_role", "plan_bug", "external_blocker", "unknown"}


def _load_pattern_registry() -> dict[str, Any]:
    """Lazily import the pattern registry so host-side tests don't drag
    the whole sandbox-Python tree on every module load."""
    from egg_restrictions.patterns import AGENT_PATTERNS

    return AGENT_PATTERNS


def _alternative_role(blocked_role: str, file_path: str) -> str | None:
    """Return the single producer role that *can* write ``file_path`` if
    one exists, else ``None``.

    Limited to the producer trio (``coder``/``tester``/``documenter``) —
    cross-phase roles like ``overseer`` or ``conflict_resolver`` are not
    valid suggestions for an impasse delegation.
    """
    registry = _load_pattern_registry()
    candidates: list[str] = []
    for role in ("coder", "tester", "documenter"):
        if role == blocked_role:
            continue
        pattern = registry.get(role)
        if pattern is None:
            continue
        if pattern.can_write(file_path):
            candidates.append(role)
    if len(candidates) == 1:
        return candidates[0]
    return None


def check_file_restriction(req: dict[str, Any]) -> dict[str, Any]:
    """Check whether the named role can write the named path(s).

    Pure read against the pattern registry — does not mutate state and
    does not call the gateway. Used by the agent before deciding to
    explore a file or hand off the task.

    No CLI counterpart: pattern matching is pure CPU and the registry
    ships in the sandbox image; a CLI shim would just shell out to
    re-import the same module. Decision-13 rationale.

    Request:
        path (str | list[str]): a single path or a list. Required.
        role (str): role to check. Defaults to ``EGG_AGENT_ROLE``.

    Response (single path):
        {
            ok: True,
            role: "coder",
            path: "tests/test_x.py",
            can_write: False,
            reason: "matches blocked pattern '**/test_*.py'",
            alternative_role: "tester",
        }

    Response (list of paths):
        {
            ok: True,
            role: "coder",
            results: [
                {path, can_write, reason, alternative_role}, ...
            ],
        }

    ``alternative_role`` is populated only when exactly one producer
    role (other than the queried one) can write the path. Multi-role
    or no-role coverage returns ``None`` — the agent should treat that
    as "ask for HITL", not "guess a role".
    """
    raw_path = req.get("path")
    if raw_path is None:
        raise HandlerError("'path' is required (string or list of strings)")

    role = req.get("role") or get_agent_role()
    if not role:
        raise HandlerError("'role' required. Set EGG_AGENT_ROLE or pass 'role' explicitly.")

    registry = _load_pattern_registry()
    pattern = registry.get(role)
    if pattern is None:
        raise HandlerError(f"Unknown role {role!r}. Known roles: {sorted(registry.keys())}")

    def _check_one(path: str) -> dict[str, Any]:
        can_write = pattern.can_write(path)
        if can_write:
            return {
                "path": path,
                "can_write": True,
                "reason": "matches an allowed pattern",
                "alternative_role": None,
            }
        return {
            "path": path,
            "can_write": False,
            "reason": (
                f"role {role!r} is blocked from {path!r} by shared/egg_restrictions/patterns.py"
            ),
            "alternative_role": _alternative_role(role, path),
        }

    if isinstance(raw_path, list):
        if not raw_path:
            raise HandlerError("'path' list cannot be empty")
        results = []
        for entry in raw_path:
            if not isinstance(entry, str) or not entry:
                raise HandlerError("'path' list entries must be non-empty strings")
            results.append(_check_one(entry))
        return {"ok": True, "role": role, "results": results}

    if not isinstance(raw_path, str) or not raw_path:
        raise HandlerError("'path' must be a non-empty string or list")

    single = _check_one(raw_path)
    return {"ok": True, "role": role, **single}


def report_impasse(req: dict[str, Any]) -> dict[str, Any]:
    """Persist a typed :class:`egg_contracts.Impasse` to the agent's
    output file so the orchestrator can route post-phase.

    No CLI counterpart: this is a structured runtime signal that lives
    inside the agent-output JSON the orchestrator already collects;
    introducing a parallel CLI surface would just create a second
    write path that could drift from the MCP one. Decision-13
    rationale.

    Request:
        category (str): one of ``wrong_role`` / ``plan_bug`` /
            ``external_blocker`` / ``unknown``. Required.
        reason (str): human-readable explanation. Required.
        task_id (str): contract task ID, e.g. ``task-1-3``. Optional;
            the orchestrator infers it from the slice's task list when
            omitted.
        suggested_role (str): for ``wrong_role`` only — the producer
            role that *can* write the blocked files. Use the
            ``alternative_role`` returned by
            :func:`check_file_restriction`.
        blocked_files (list[str]): files the assigned role cannot
            write. Optional but recommended for ``wrong_role``.
        evidence (dict): free-form structured evidence to surface in
            the HITL decision body. Optional.
        role (str): override (defaults to ``EGG_AGENT_ROLE``).
        identifier / repo_path: optional overrides.

    Response:
        {
            ok: True,
            written_to: "<path>",
            category: "...",
            suggested_role: "...",
            guidance: "Stop work and exit. Do not commit code; the "
                      "orchestrator will route this impasse "
                      "post-phase.",
        }

    The handler does not touch the contract — the orchestrator owns
    role-flips and the ``delegation_attempts`` counter. The agent
    should not call any other producer tool after this returns.
    """
    category = req.get("category")
    if not category or not isinstance(category, str):
        raise HandlerError(f"'category' is required: one of {sorted(_VALID_CATEGORIES)}")
    if category not in _VALID_CATEGORIES:
        raise HandlerError(
            f"Unknown category {category!r}. Expected one of {sorted(_VALID_CATEGORIES)}"
        )

    reason = req.get("reason")
    if not reason or not isinstance(reason, str):
        raise HandlerError("'reason' is required (non-empty string)")

    role = req.get("role") or get_agent_role()
    if not role:
        raise HandlerError("'role' required. Set EGG_AGENT_ROLE or pass 'role' explicitly.")

    suggested_role = req.get("suggested_role")
    if suggested_role is not None and not isinstance(suggested_role, str):
        raise HandlerError("'suggested_role' must be a string when provided")
    if category == "wrong_role":
        # ``wrong_role`` is the only auto-delegateable category; without
        # ``suggested_role`` the orchestrator-side router can only
        # escalate to HITL, which silently degrades the producer's
        # deliberately-set ``category=wrong_role`` signal into
        # "always-escalate". Reject at the handler boundary and point
        # the agent at ``check_file_restriction`` so the fix lands in
        # the same iteration.
        if not suggested_role:
            raise HandlerError(
                "'suggested_role' is required for category='wrong_role'. "
                "Call mcp__sdlc__check_file_restriction first to discover "
                "the producer role that *can* write the blocked files, "
                "then pass it as suggested_role. Use category='unknown' "
                "if no single producer role covers the impasse."
            )
        if suggested_role == role:
            raise HandlerError(
                "'suggested_role' must differ from the impassed role "
                f"({role!r}); a wrong_role impasse cannot delegate to itself."
            )

    blocked_files = req.get("blocked_files") or []
    if not isinstance(blocked_files, list) or not all(
        isinstance(f, str) and f for f in blocked_files
    ):
        raise HandlerError("'blocked_files' must be a list of non-empty strings")

    evidence = req.get("evidence") or {}
    if not isinstance(evidence, dict):
        raise HandlerError("'evidence' must be a dict when provided")

    task_id = req.get("task_id")
    if task_id is not None and not isinstance(task_id, str):
        raise HandlerError("'task_id' must be a string when provided")
    # ``wrong_role`` triggers an auto-delegation against a specific
    # task — the role-match fallback in the orchestrator-side router
    # is fragile when a slice contains multiple tasks per role or
    # role-less tasks. Require an explicit task_id so the routing
    # never has to guess. Other categories (plan_bug,
    # external_blocker, unknown) escalate either way and tolerate
    # task-level ambiguity.
    if category == "wrong_role" and not task_id:
        raise HandlerError(
            "'task_id' is required for category='wrong_role' so the "
            "orchestrator can route precisely. Look it up in your "
            "spawn prompt or via `egg-contract show`."
        )

    repo_path = Path(req.get("repo_path") or get_repo_path())
    identifier = req.get("identifier") or req.get("issue") or req.get("pipeline_id")
    if identifier is None:
        identifier = get_contract_identifier()
    # ``identifier`` may legitimately be None for ad-hoc / standalone
    # agents — ``save_agent_output`` falls back to the unprefixed path
    # in that case.

    impasse_payload: dict[str, Any] = {
        "category": category,
        "reason": reason,
        "task_id": task_id,
        "suggested_role": suggested_role,
        "blocked_files": list(blocked_files),
        "evidence": dict(evidence),
        "created_at": datetime.now(UTC).isoformat(),
    }

    # Load any existing output for this role/identifier so we don't
    # clobber handoff_data, files_changed, etc. Falls back to a fresh
    # dict when the file doesn't exist yet (the common case — the
    # agent typically calls report_impasse before any handoff write).
    from egg_contracts.agent_roles import AgentRole as ContractAgentRole
    from egg_contracts.orchestrator import (
        load_agent_output,
        save_agent_output,
    )

    try:
        contract_role = ContractAgentRole(role)
    except ValueError as exc:
        raise HandlerError(
            f"Role {role!r} is not a known contract AgentRole; cannot "
            "persist impasse to the role-keyed agent-output file."
        ) from exc

    existing = load_agent_output(repo_path, contract_role, identifier=identifier)
    if not isinstance(existing, dict):
        existing = {}

    output: dict[str, Any] = dict(existing)
    output["role"] = role
    output["impasse"] = impasse_payload
    # Preserve the timestamp shape that AgentOutput.from_dict expects
    # so the orchestrator-side loader doesn't synthesise a different
    # one on its read.
    output.setdefault("timestamp", datetime.now(UTC).isoformat())

    written_path = save_agent_output(repo_path, contract_role, output, identifier=identifier)

    return {
        "ok": True,
        "written_to": str(written_path),
        "category": category,
        "suggested_role": suggested_role,
        "task_id": task_id,
        "guidance": (
            "Impasse recorded. Stop all further work for this task and "
            "exit cleanly. Do not commit code or invent a workaround — "
            "the orchestrator will read this signal post-phase and "
            "either delegate to suggested_role (first attempt) or "
            "escalate to HITL (second attempt or no eligible role)."
        ),
    }
