"""Tests for the ``apply_epic`` agent's refine-mode prompt and role registration.

#1557 TASK-1-10 (refine portion only) ships:

- ``APPLY_EPIC_REFINE_PROMPT`` — the prompt string the orchestrator
  hands the apply_epic agent after refine HITL approval.
- ``AgentRole.APPLY_EPIC`` + ``APPLY_EPIC_ROLE`` — the role enum + its
  ``AgentRoleDefinition`` (responsibilities, file-access boundary,
  contract-role mapping).

These tests cover the prompt's structural contract (the agent reads
the analysis draft, dispatches gateway-mediated edits, opens HITL
gates on divergence) and the registration invariants (role enrolled
in registries, HITL tool surfaced via the responsibilities, dependency
list empty so the agent joins as a peer producer).
"""

from __future__ import annotations

from agent_prompts import APPLY_EPIC_REFINE_PROMPT
from egg_contracts.agent_roles import (
    AGENT_ROLE_TO_CONTRACT_ROLE,
    AGENT_ROLES,
    APPLY_EPIC_ROLE,
    AgentRole,
)
from egg_contracts.roles import Role

# ---------------------------------------------------------------------------
# Prompt-content tests
# ---------------------------------------------------------------------------


class TestApplyEpicRefinePromptContent:
    """The refine-mode prompt must surface the operational contract."""

    def test_prompt_is_non_empty_and_names_refine_mode(self):
        """Smoke test: the prompt is non-trivial and tagged refine-mode."""
        assert isinstance(APPLY_EPIC_REFINE_PROMPT, str)
        assert len(APPLY_EPIC_REFINE_PROMPT) > 500
        assert "refine mode" in APPLY_EPIC_REFINE_PROMPT
        assert "APPLY_EPIC" in APPLY_EPIC_REFINE_PROMPT

    def test_prompt_includes_epic_key_env_var(self):
        """The prompt references the gateway-exported epic-key env var."""
        # The orchestrator exports $EGG_JIRA_EPIC_KEY into the sandbox
        # (see ``orchestrator/routes/pipelines.py`` ~L19669). The prompt
        # references it by name so the agent knows where to read the
        # target key.
        assert "$EGG_JIRA_EPIC_KEY" in APPLY_EPIC_REFINE_PROMPT

    def test_prompt_references_refined_analysis_path(self):
        """The prompt names the refine draft path (refined-analysis input)."""
        # The agent reads the operator-approved refine output from
        # ``.egg-state/drafts/<prefix>-analysis.md`` — this is the
        # "refined-analysis path" the agent operates on.
        assert ".egg-state/drafts/" in APPLY_EPIC_REFINE_PROMPT
        assert "-analysis.md" in APPLY_EPIC_REFINE_PROMPT

    def test_prompt_names_gateway_mediated_verbs(self):
        """Every Jira mutation is dispatched through gateway endpoints.

        The refine-mode prompt must point the agent at gateway routes —
        never at raw Atlassian endpoints — so the egg trust boundary is
        preserved (risk R7 — no credentials in the sandbox).
        """
        # ``ticket/get`` reads the live Description for the divergence
        # check; ``ticket/edit`` performs the wholesale rewrite.
        assert "/api/v1/jira/ticket/get" in APPLY_EPIC_REFINE_PROMPT
        assert "/api/v1/jira/ticket/edit" in APPLY_EPIC_REFINE_PROMPT

    def test_prompt_routes_through_gateway_not_direct_atlassian(self):
        """No raw Atlassian Cloud URLs may leak into the prompt.

        The refine agent runs in the sandbox; surfacing a direct
        ``*.atlassian.net`` URL would invite the agent to bypass the
        gateway (and exfiltrate ``JIRA_API_TOKEN`` if it leaked in).
        """
        assert "atlassian.net" not in APPLY_EPIC_REFINE_PROMPT
        # Raw Atlassian REST API paths must not appear either.
        assert "/rest/api/" not in APPLY_EPIC_REFINE_PROMPT

    def test_prompt_mentions_register_open_question_for_divergence(self):
        """The prompt routes concurrent-edit divergence through HITL.

        When the live Description sha256 differs from the recorded one
        the agent opens a HITL gate via the ``register_open_question``
        MCP tool rather than overwriting silently.
        """
        assert "mcp__sdlc__register_open_question" in APPLY_EPIC_REFINE_PROMPT
        # Both resolution options are surfaced.
        assert "Confirm overwrite" in APPLY_EPIC_REFINE_PROMPT
        assert "Skip" in APPLY_EPIC_REFINE_PROMPT

    def test_prompt_documents_concurrent_edit_sha256_check(self):
        """The trade-off note on concurrent-edit divergence is surfaced.

        The refine agent fetches the live Description, sha256s it, and
        compares against the recorded ``refine_description_sha256`` —
        diverging hashes pause for HITL. This is the trust-boundary
        trade-off the prompt must articulate.
        """
        assert "sha256" in APPLY_EPIC_REFINE_PROMPT
        assert "refine_description_sha256" in APPLY_EPIC_REFINE_PROMPT
        # The trade-off framing: operator edited after kick-off.
        assert "operator" in APPLY_EPIC_REFINE_PROMPT.lower()

    def test_prompt_documents_idempotent_artifact_persistence(self):
        """The persisted artifact is the EpicApplyArtifact agent-outputs JSON."""
        assert "EpicApplyArtifact" in APPLY_EPIC_REFINE_PROMPT
        assert ".egg-state/agent-outputs/" in APPLY_EPIC_REFINE_PROMPT
        assert "-epic-apply.json" in APPLY_EPIC_REFINE_PROMPT

    def test_prompt_calls_out_decision_9(self):
        """The wholesale-rewrite policy is anchored to #1557 decision-9.

        Without the decision reference the agent has no anchor for
        ‘wholesale rewrite’ — a future contributor could read the prompt
        and over-engineer a diff/merge path.
        """
        assert "decision-9" in APPLY_EPIC_REFINE_PROMPT
        assert "wholesale rewrite" in APPLY_EPIC_REFINE_PROMPT.lower()

    def test_prompt_documents_failure_handling(self):
        """4xx/5xx gateway errors are caught and surfaced on the artifact."""
        assert "Failure handling" in APPLY_EPIC_REFINE_PROMPT
        assert "status=" in APPLY_EPIC_REFINE_PROMPT  # ``status="failed"`` etc.


# ---------------------------------------------------------------------------
# AgentRole registration tests
# ---------------------------------------------------------------------------


class TestApplyEpicAgentRoleRegistration:
    """The role + role-definition must be registered and consistent."""

    def test_apply_epic_role_enum_value(self):
        """The role enum is the string ``"apply_epic"`` (used in env vars)."""
        assert AgentRole.APPLY_EPIC.value == "apply_epic"

    def test_apply_epic_role_in_registry(self):
        """The role is registered in ``AGENT_ROLES``."""
        assert AgentRole.APPLY_EPIC in AGENT_ROLES
        assert AGENT_ROLES[AgentRole.APPLY_EPIC] is APPLY_EPIC_ROLE

    def test_apply_epic_role_no_dependencies(self):
        """The agent runs after HITL approval — no producer-role deps.

        Empty ``dependencies`` lets ``apply_epic`` join the producer
        roster from the start (it must show up in the BRC consensus
        tracker rather than waiting on a peer).
        """
        assert APPLY_EPIC_ROLE.dependencies == []

    def test_apply_epic_role_contract_mapping_is_implementer(self):
        """Contract-role mapping is IMPLEMENTER (writes contract fields).

        The agent appends ``applied_edits[]`` entries to its
        ``epic_apply`` artifact, which is an implementer-class
        write.
        """
        mapped = AGENT_ROLE_TO_CONTRACT_ROLE[AgentRole.APPLY_EPIC]
        assert mapped == Role.IMPLEMENTER

    def test_apply_epic_role_responsibilities_mention_register_open_question(self):
        """The role's responsibilities surface the HITL MCP tool.

        The agent opens HITL gates on concurrent-edit divergence and
        in-flight target mutation — ``mcp__sdlc__register_open_question``
        is the only MCP surface that creates contract decisions.
        """
        joined = "\n".join(APPLY_EPIC_ROLE.responsibilities)
        assert "mcp__sdlc__register_open_question" in joined

    def test_apply_epic_role_writes_only_agent_outputs(self):
        """The agent's only write target is ``.egg-state/agent-outputs/``.

        Source code, docs, and other state directories are explicitly
        blocked. Without these blocks the agent could overwrite the
        coder's deliverables under the trust boundary.
        """
        allowed = APPLY_EPIC_ROLE.file_access.allowed_write
        assert ".egg-state/agent-outputs/" in allowed

        blocked = APPLY_EPIC_ROLE.file_access.blocked_write
        # The agent must NOT be able to write source / docs / tests /
        # contracts / drafts / pipelines / reviews.
        for path in (
            "src/",
            "lib/",
            "shared/",
            "gateway/",
            "sandbox/",
            "orchestrator/",
            "docs/",
            "tests/",
            ".egg-state/contracts/",
            ".egg-state/drafts/",
            ".egg-state/pipelines/",
            ".egg-state/reviews/",
        ):
            assert path in blocked, f"apply_epic role must block writes to {path} (trust boundary)"

    def test_apply_epic_role_cannot_write_source_files(self):
        """The role's ``can_write`` check rejects source-tree paths."""
        for path in (
            "src/foo.py",
            "orchestrator/routes/pipelines.py",
            "gateway/gateway.py",
            "shared/egg_contracts/agent_roles.py",
            "docs/architecture/orchestrator.md",
            "tests/test_anything.py",
            ".egg-state/drafts/1557-analysis.md",
            ".egg-state/contracts/issue-1557.json",
        ):
            assert APPLY_EPIC_ROLE.file_access.can_write(path) is False, (
                f"apply_epic must NOT be allowed to write {path}"
            )

    def test_apply_epic_role_can_write_agent_output_artifact(self):
        """The role's ``can_write`` check accepts its single output path."""
        assert APPLY_EPIC_ROLE.file_access.can_write(".egg-state/agent-outputs/x.json") is True

    def test_apply_epic_role_role_field_matches_enum(self):
        """The definition's ``role`` field round-trips to the enum."""
        assert APPLY_EPIC_ROLE.role == AgentRole.APPLY_EPIC

    def test_apply_epic_role_description_mentions_jira_epic(self):
        """The description names the Jira-epic scope so role-listing UIs
        surface it. Regression guard against a generic 'apply step'
        description that would leak the role into non-epic pipelines."""
        assert "Jira" in APPLY_EPIC_ROLE.description
        assert "epic" in APPLY_EPIC_ROLE.description.lower()
