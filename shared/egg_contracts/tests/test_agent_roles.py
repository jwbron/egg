"""Tests for the new ``reviewer_security`` and ``reviewer_concurrency`` roles.

Covers TASK-1-3 (b) of issue #1965:

- ``AgentRole("reviewer_security")`` and ``AgentRole("reviewer_concurrency")``
  resolve to enum members.
- Both have ``AgentRoleDefinition`` entries in ``AGENT_ROLES``.
- Both map to ``Role.REVIEWER`` via ``AGENT_ROLE_TO_CONTRACT_ROLE`` /
  ``get_contract_role()``.
- Both appear in ``_PHASE_REVIEWERS["implement"]`` and the result of
  ``get_roles_for_phase("implement")``.
- Both share the same ``blocked_write`` shape as ``REVIEWER_CODE_ROLE``
  (uses the canonical ``_REVIEWER_BLOCKED_WRITE`` list).
- Both belong to ``AgentCategory.REVIEW``.
- Neither is in ``EGG_ONLY_REVIEWERS`` — they apply to every repo.
"""

from __future__ import annotations

import pytest

from egg_contracts.agent_roles import (
    _PHASE_REVIEWERS,
    AGENT_ROLE_TO_CONTRACT_ROLE,
    AGENT_ROLES,
    DOCUMENTER_ROLE,
    EGG_ONLY_REVIEWERS,
    REVIEWER_CODE_ROLE,
    AgentCategory,
    AgentRole,
    get_contract_role,
    get_role_definition,
    get_roles_for_phase,
)
from egg_contracts.roles import Role


class TestNewLensReviewersResolveAsEnum:
    """``AgentRole`` membership for the new lens reviewers."""

    def test_reviewer_security_resolves(self) -> None:
        assert AgentRole("reviewer_security") is AgentRole.REVIEWER_SECURITY
        assert AgentRole.REVIEWER_SECURITY.value == "reviewer_security"

    def test_reviewer_concurrency_resolves(self) -> None:
        assert AgentRole("reviewer_concurrency") is AgentRole.REVIEWER_CONCURRENCY
        assert AgentRole.REVIEWER_CONCURRENCY.value == "reviewer_concurrency"


class TestNewLensReviewersDefinitions:
    """Role definitions for the new lens reviewers."""

    @pytest.mark.parametrize(
        "role",
        [AgentRole.REVIEWER_SECURITY, AgentRole.REVIEWER_CONCURRENCY],
    )
    def test_definition_registered_in_agent_roles(self, role: AgentRole) -> None:
        assert role in AGENT_ROLES, f"{role.value} missing from AGENT_ROLES dict"

    @pytest.mark.parametrize(
        "role",
        [AgentRole.REVIEWER_SECURITY, AgentRole.REVIEWER_CONCURRENCY],
    )
    def test_definition_lookup_works(self, role: AgentRole) -> None:
        defn = get_role_definition(role)
        assert defn.role is role
        assert defn.category is AgentCategory.REVIEW

    @pytest.mark.parametrize(
        "role",
        [AgentRole.REVIEWER_SECURITY, AgentRole.REVIEWER_CONCURRENCY],
    )
    def test_blocked_write_matches_reviewer_code(self, role: AgentRole) -> None:
        """New lens reviewers share the canonical reviewer blocked-write list.

        The plan instructs the coder to reuse ``_REVIEWER_BLOCKED_WRITE`` (the
        same list that ``REVIEWER_CODE_ROLE`` uses) so we assert structural
        equality with ``REVIEWER_CODE_ROLE.file_access.blocked_write``.
        """
        defn = get_role_definition(role)
        assert defn.file_access.blocked_write == REVIEWER_CODE_ROLE.file_access.blocked_write, (
            f"{role.value} blocked_write should reuse _REVIEWER_BLOCKED_WRITE "
            "(the same list as REVIEWER_CODE_ROLE) — see TASK-1-1 in the plan."
        )

    def test_string_lookup_through_helper(self) -> None:
        """``get_role_definition`` accepts the raw string value."""
        sec = get_role_definition("reviewer_security")
        assert sec.role is AgentRole.REVIEWER_SECURITY
        conc = get_role_definition("reviewer_concurrency")
        assert conc.role is AgentRole.REVIEWER_CONCURRENCY


class TestNewLensReviewersContractRoleMapping:
    """Both new roles map to ``Role.REVIEWER`` for contract-API auth."""

    @pytest.mark.parametrize(
        "role",
        [AgentRole.REVIEWER_SECURITY, AgentRole.REVIEWER_CONCURRENCY],
    )
    def test_contract_role_via_dict(self, role: AgentRole) -> None:
        assert AGENT_ROLE_TO_CONTRACT_ROLE.get(role) == Role.REVIEWER

    @pytest.mark.parametrize(
        "role_value",
        ["reviewer_security", "reviewer_concurrency"],
    )
    def test_contract_role_via_helper(self, role_value: str) -> None:
        assert get_contract_role(role_value) == Role.REVIEWER


class TestNewLensReviewersInImplementPhase:
    """Both lens reviewers participate in the implement phase."""

    def test_in_phase_reviewers_dict(self) -> None:
        implement_reviewers = _PHASE_REVIEWERS["implement"]
        assert AgentRole.REVIEWER_SECURITY in implement_reviewers
        assert AgentRole.REVIEWER_CONCURRENCY in implement_reviewers

    def test_in_get_roles_for_phase_implement(self) -> None:
        roles = get_roles_for_phase("implement")
        assert AgentRole.REVIEWER_SECURITY in roles
        assert AgentRole.REVIEWER_CONCURRENCY in roles

    def test_in_get_roles_for_phase_implement_for_non_egg_repo(self) -> None:
        """The new lens reviewers are NOT egg-only — they apply to every repo."""
        roles = get_roles_for_phase("implement", repo="some-org/some-repo")
        assert AgentRole.REVIEWER_SECURITY in roles
        assert AgentRole.REVIEWER_CONCURRENCY in roles

    def test_not_in_plan_or_refine_phase_rosters(self) -> None:
        plan_roles = get_roles_for_phase("plan")
        refine_roles = get_roles_for_phase("refine")
        for role in (AgentRole.REVIEWER_SECURITY, AgentRole.REVIEWER_CONCURRENCY):
            assert role not in plan_roles
            assert role not in refine_roles


class TestNewLensReviewersNotEggOnly:
    """The new lens reviewers run on every repo, not just jwbron/egg."""

    def test_not_in_egg_only_set(self) -> None:
        assert AgentRole.REVIEWER_SECURITY not in EGG_ONLY_REVIEWERS
        assert AgentRole.REVIEWER_CONCURRENCY not in EGG_ONLY_REVIEWERS


class TestDocumenterRoleSnapshotFraming:
    """``DOCUMENTER_ROLE`` carries snapshot-not-ledger framing while its gateway
    write boundaries stay byte-identical (#3288).

    The role description/responsibilities were reframed from "documentation for
    the changes" to current-state documentation. That wording change MUST NOT
    touch the ``FileAccessPattern`` — ``allowed_write`` / ``blocked_write`` are
    a hard gateway constraint, so these tests pin both lists exactly.
    """

    # Gateway write boundaries — must remain byte-identical after the wording
    # change. The blocked_write list keeps code/test/.github surfaces out of
    # the documenter's reach.
    _EXPECTED_ALLOWED_WRITE = [
        "docs/",
        "**/README.md",
        "**/*.md",
        ".egg-state/agent-outputs/",
    ]
    _EXPECTED_BLOCKED_WRITE = [
        "**/*.py",
        "**/*.ts",
        "**/*.tsx",
        "**/*.js",
        "**/*.jsx",
        "**/*.go",
        "**/*.java",
        "tests/",
        ".egg-state/contracts/",
        ".github/",
    ]

    def test_allowed_write_boundaries_unchanged(self) -> None:
        assert DOCUMENTER_ROLE.file_access.allowed_write == self._EXPECTED_ALLOWED_WRITE

    def test_blocked_write_boundaries_unchanged(self) -> None:
        assert DOCUMENTER_ROLE.file_access.blocked_write == self._EXPECTED_BLOCKED_WRITE

    def test_lookup_helper_returns_same_boundaries(self) -> None:
        """The registry lookup returns the same boundary lists."""
        defn = get_role_definition(AgentRole.DOCUMENTER)
        assert defn.file_access.allowed_write == self._EXPECTED_ALLOWED_WRITE
        assert defn.file_access.blocked_write == self._EXPECTED_BLOCKED_WRITE

    def test_documenter_cannot_write_code_or_tests(self) -> None:
        """Behavioral check on the boundaries: code/test paths stay blocked,
        markdown stays writable."""
        fa = DOCUMENTER_ROLE.file_access
        assert fa.can_write("orchestrator/routes/pipelines.py") is False
        assert fa.can_write("shared/egg_contracts/tests/test_agent_roles.py") is False
        assert fa.can_write(".github/PULL_REQUEST_TEMPLATE.md") is False
        assert fa.can_write("docs/architecture/brc-memory.md") is True
        assert fa.can_write("orchestrator/README.md") is True

    def test_role_metadata_stable(self) -> None:
        """Role identity, category, and coder dependency are unchanged."""
        assert DOCUMENTER_ROLE.role == AgentRole.DOCUMENTER
        assert DOCUMENTER_ROLE.category == AgentCategory.EXECUTION
        assert AgentRole.CODER in DOCUMENTER_ROLE.dependencies

    def test_description_uses_snapshot_framing(self) -> None:
        """Description expresses current-state documentation, not 'the changes'."""
        description = DOCUMENTER_ROLE.description.lower()
        assert "current state" in description
        assert "for the changes" not in description

    def test_responsibilities_forbid_sdlc_artifacts(self) -> None:
        """Responsibilities carry the snapshot rule and the no-SDLC-artifact
        prohibition (slice/TASK/phase/HITL ids out of docs)."""
        joined = " ".join(DOCUMENTER_ROLE.responsibilities).lower()
        assert "snapshot" in joined
        assert "sdlc artifact" in joined
        assert "slice number" in joined
        assert "task-n" in joined
        # Prefers rationale over chronology.
        assert "rationale" in joined
