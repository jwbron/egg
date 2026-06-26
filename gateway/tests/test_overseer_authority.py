"""Slice-6 overseer-authority gateway contract (issue #2270, task-6-2).

Slice-6's ``CorrectiveExecutor`` (§4) executes ``open_operator_hitl`` by writing
a contract decision **via the orchestrator identity at the REAL enforcement
point** — ``gateway/agent_restrictions.py`` (per-role file patterns) plus
contract RBAC — *not* through the agent gateway and *not* gated by
``roles.py:can_modify``. The architecture note in the ticket: this "dissolves
the gateway 403" precisely because the orchestrator is the control plane, while
an **agent** identity remains denied from touching the contract.

This module pins the gateway half of that authority story — the **deny side**,
which is the security invariant the whole authority model rests on:

* the OVERSEER *agent* is a gated role and is **denied** writes to
  ``.egg-state/contracts/`` (this denial *is* the "gateway 403"); and
* the orchestrator / control-plane identity is **not** one of the gateway's
  per-role agent patterns at all — so its contract-decision write is a
  control-plane operation, not an agent push subject to these restrictions.

These are **strict, pass-today** regression guards: slice-6 must *preserve*
this boundary, never weaken it (e.g. by adding ``.egg-state/contracts/`` to the
overseer's allow-list to "let the overseer open a HITL"). The authorized-path
half — that the executor actually opens the decision through the injected
orchestrator-identity writer — is pinned in
``orchestrator/tests/test_corrective_executor.py``.
"""

from __future__ import annotations

import pytest
from agent_restrictions import (
    OVERSEER_PATTERNS,
    get_agent_pattern,
    partition_files_by_role,
)

_CONTRACT_PATHS = [
    ".egg-state/contracts/issue-2270-overhaul.json",
    ".egg-state/contracts/issue-1.json",
]

# Identities that drive the corrective-executor write path but are NOT agents —
# the control plane. The gateway has no per-role agent pattern for these.
_CONTROL_PLANE_IDENTITIES = ["orchestrator", "system"]


# ---------------------------------------------------------------------------
# The deny side: an overseer AGENT cannot write the contract (the "403").
# ---------------------------------------------------------------------------


class TestOverseerAgentDeniedContractWrites:
    """The overseer agent role is denied contract writes at the gateway."""

    @pytest.mark.parametrize("path", _CONTRACT_PATHS)
    def test_overseer_blocked_from_contracts(self, path: str) -> None:
        allowed, blocked = partition_files_by_role("overseer", [path])
        assert allowed == []
        assert blocked == [path], (
            "the overseer agent must remain blocked from .egg-state/contracts/ — "
            "this denial IS the gateway 403 that slice-6 routes around via the "
            "orchestrator identity (#2270 §4)"
        )

    def test_overseer_pattern_blocks_contracts_directly(self) -> None:
        """The block is in the pattern itself, not an accident of path matching."""
        for path in _CONTRACT_PATHS:
            assert OVERSEER_PATTERNS.can_write(path) is False

    def test_overseer_write_surface_is_oversight_logs_only(self) -> None:
        """Sanity: the overseer may write only its oversight/agent-output logs —
        never source, tests, docs, or contracts (oversight-logs-only role)."""
        assert OVERSEER_PATTERNS.can_write(".egg-state/oversight/run.log") is True
        for blocked in (
            "orchestrator/routes/pipelines.py",
            "gateway/agent_restrictions.py",
            ".egg-state/contracts/issue-2270-overhaul.json",
        ):
            assert OVERSEER_PATTERNS.can_write(blocked) is False


class TestOtherAgentsDeniedContractWrites:
    """Other producer agents are likewise denied direct contract writes."""

    @pytest.mark.parametrize("role", ["coder", "tester", "documenter", "reviewer_code"])
    def test_agent_roles_blocked_from_contracts(self, role: str) -> None:
        path = ".egg-state/contracts/issue-2270-overhaul.json"
        allowed, blocked = partition_files_by_role(role, [path])
        assert path in blocked
        assert path not in allowed


# ---------------------------------------------------------------------------
# The dissolve: the control plane is not an agent, so it is not gated here.
# ---------------------------------------------------------------------------


class TestControlPlaneNotGatedAsAgent:
    """The orchestrator/control-plane identity has no per-role agent pattern.

    The executor's ``open_operator_hitl`` runs as the orchestrator, which does
    not push through the agent file-restriction gateway — that is *why* the 403
    is dissolved. The boundary is the agent file-pattern layer, not
    ``roles.py:can_modify``.
    """

    @pytest.mark.parametrize("identity", _CONTROL_PLANE_IDENTITIES)
    def test_control_plane_is_not_a_gated_agent_role(self, identity: str) -> None:
        assert get_agent_pattern(identity) is None, (
            f"{identity!r} must NOT be a gateway-gated agent role — the "
            "control-plane contract write is not an agent push (#2270 §4)"
        )


# ---------------------------------------------------------------------------
# Forward-looking: if slice-6 adds a gateway corrective-authority guardrail,
# it must bound the closed vocabulary. Present-only — never a no-op forever
# because the strict deny guards above always run.
# ---------------------------------------------------------------------------

_AUTHORITY_FN_CANDIDATES = (
    "check_overseer_corrective_action",
    "check_corrective_action",
    "check_overseer_authority",
    "authorize_corrective_action",
)
_EXPECTED_ACTIONS = ("nudge_agent", "respawn_cohort", "open_operator_hitl")


def _authority_fn():
    import agent_restrictions

    for name in _AUTHORITY_FN_CANDIDATES:
        fn = getattr(agent_restrictions, name, None)
        if callable(fn):
            return name, fn
    return None, None


class TestCorrectiveAuthorityGuardrailIfPresent:
    """If the coder adds a gateway corrective-authority check, it must bound the
    closed vocabulary and reject anything outside it."""

    def test_guardrail_bounds_the_closed_vocabulary(self) -> None:
        name, fn = _authority_fn()
        if fn is None:
            pytest.skip(
                "no gateway corrective-authority guardrail added in slice-6 — "
                "the deny-side invariant is covered by the strict guards above"
            )
        # An out-of-vocabulary action must not be authorized by any plausible
        # boolean/result shape the guardrail returns.
        for bad in ("force_merge", "delete_repo", "none", ""):
            result = fn(action=bad)
            allowed = getattr(result, "allowed", result)
            assert not allowed, (
                f"{name} authorized out-of-vocabulary action {bad!r} — the "
                "corrective vocabulary must stay closed (#2270 §4)"
            )
