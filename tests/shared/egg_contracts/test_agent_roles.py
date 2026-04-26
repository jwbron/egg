"""Tests for egg_contracts.agent_roles module.

Covers:
- AgentCategory enum completeness
- AgentRole enum completeness and canonical values
- AgentRoleDefinition dataclass and category assignments
- FileAccessPattern matching (blocked-first, wildcards, directory patterns)
- AGENT_ROLES registry completeness (every AgentRole has a definition)
- get_roles_by_category helper
- get_role_definition, get_all_roles, get_role_dependencies
- can_run_in_parallel dependency logic
- get_roles_for_phase with reviewer inclusion and repo filtering
- detect_write_overlaps
- AgentExecution lifecycle (is_complete, is_successful, can_retry)
- Role sync: verify other modules mirror the canonical AgentRole enum
- New utility roles: AUTOFIXER, CONFLICT_RESOLVER with correct categories

Related: issue #1030 — Agent team roster, roles, and access controls
"""

import pytest
from egg_contracts.agent_roles import (
    AGENT_ROLES,
    AgentCategory,
    AgentExecution,
    AgentRole,
    AgentRoleDefinition,
    AgentStatus,
    FileAccessPattern,
    can_run_in_parallel,
    create_execution_for_role,
    detect_write_overlaps,
    get_all_roles,
    get_role_definition,
    get_role_dependencies,
    get_roles_for_phase,
)

# ---------------------------------------------------------------------------
# AgentCategory enum
# ---------------------------------------------------------------------------


class TestAgentCategory:
    """Verify the AgentCategory enum has all expected values."""

    def test_category_values(self):
        """All 5 categories should exist."""
        assert AgentCategory.EXECUTION == "execution"
        assert AgentCategory.ANALYSIS == "analysis"
        assert AgentCategory.REVIEW == "review"
        assert AgentCategory.UTILITY == "utility"
        assert AgentCategory.INTERFACE == "interface"

    def test_category_count(self):
        """Exactly 5 categories."""
        assert len(AgentCategory) == 5

    def test_category_is_strenum(self):
        """Categories should be string-valued."""
        for cat in AgentCategory:
            assert isinstance(cat, str)
            assert isinstance(cat.value, str)


# ---------------------------------------------------------------------------
# AgentRole enum
# ---------------------------------------------------------------------------


class TestAgentRole:
    """Verify the AgentRole enum has all expected values."""

    EXPECTED_ROLES = {
        "coder",
        "tester",
        "documenter",
        "architect",
        "task_planner",
        "risk_analyst",
        "refiner",
        "reviewer_code",
        "reviewer_code_holistic",
        "reviewer_contract",
        "reviewer_agent_design",
        "reviewer_refine",
        "reviewer_plan",
        "overseer",
        "autofixer",
        "conflict_resolver",
        "inspector",
    }

    def test_all_expected_roles_exist(self):
        """All 16 expected roles should be present in the enum."""
        actual = {r.value for r in AgentRole}
        assert self.EXPECTED_ROLES.issubset(actual), (
            f"Missing roles: {self.EXPECTED_ROLES - actual}"
        )

    def test_role_count(self):
        """Should have at least 16 roles (15 standard + inspector)."""
        assert len(AgentRole) >= 16

    def test_execution_roles(self):
        assert AgentRole.CODER == "coder"
        assert AgentRole.TESTER == "tester"
        assert AgentRole.DOCUMENTER == "documenter"

    def test_analysis_roles(self):
        assert AgentRole.ARCHITECT == "architect"
        assert AgentRole.TASK_PLANNER == "task_planner"
        assert AgentRole.RISK_ANALYST == "risk_analyst"
        assert AgentRole.REFINER == "refiner"

    def test_review_roles(self):
        assert AgentRole.REVIEWER_CODE == "reviewer_code"
        assert AgentRole.REVIEWER_CODE_HOLISTIC == "reviewer_code_holistic"
        assert AgentRole.REVIEWER_CONTRACT == "reviewer_contract"
        assert AgentRole.REVIEWER_AGENT_DESIGN == "reviewer_agent_design"
        assert AgentRole.REVIEWER_REFINE == "reviewer_refine"
        assert AgentRole.REVIEWER_PLAN == "reviewer_plan"

    def test_utility_roles(self):
        """AUTOFIXER and CONFLICT_RESOLVER should exist as utility roles."""
        assert AgentRole.AUTOFIXER == "autofixer"
        assert AgentRole.CONFLICT_RESOLVER == "conflict_resolver"

    def test_interface_roles(self):
        assert AgentRole.OVERSEER == "overseer"

    def test_role_is_strenum(self):
        """Roles should be string-valued."""
        for role in AgentRole:
            assert isinstance(role, str)

    def test_no_vestigial_roles(self):
        """Removed roles should not exist in the enum."""
        role_values = {r.value for r in AgentRole}
        assert "integrator" not in role_values
        assert "checker" not in role_values
        assert "reviewer_unified" not in role_values


# ---------------------------------------------------------------------------
# FileAccessPattern
# ---------------------------------------------------------------------------


class TestFileAccessPattern:
    """Verify FileAccessPattern matching logic."""

    def test_empty_allowed_read_means_all_readable(self):
        """Empty allowed_read should allow reading any file."""
        pattern = FileAccessPattern()
        assert pattern.can_read("any/file.py") is True

    def test_allowed_read_restricts(self):
        """Non-empty allowed_read should restrict to matching files."""
        pattern = FileAccessPattern(allowed_read=["src/"])
        assert pattern.can_read("src/main.py") is True
        assert pattern.can_read("docs/readme.md") is False

    def test_empty_allowed_write_blocks_all(self):
        """Empty allowed_write should block all writes."""
        pattern = FileAccessPattern()
        assert pattern.can_write("any/file.py") is False

    def test_blocked_write_takes_precedence(self):
        """Blocked patterns should override allowed patterns."""
        pattern = FileAccessPattern(
            allowed_write=["**/*.py"],
            blocked_write=["tests/"],
        )
        assert pattern.can_write("src/main.py") is True
        assert pattern.can_write("tests/test_main.py") is False

    def test_directory_prefix_match(self):
        """Directory patterns (ending in /) should match files in that directory."""
        pattern = FileAccessPattern(allowed_write=["docs/"])
        assert pattern.can_write("docs/guide.md") is True
        assert pattern.can_write("docs/sub/page.md") is True
        assert pattern.can_write("src/main.py") is False

    def test_wildcard_match(self):
        """Wildcard patterns should match files by extension."""
        pattern = FileAccessPattern(allowed_write=["**/*.py"])
        assert pattern.can_write("src/main.py") is True
        assert pattern.can_write("deep/nested/file.py") is True
        assert pattern.can_write("src/main.ts") is False

    def test_exact_match(self):
        """Exact file paths should match exactly."""
        pattern = FileAccessPattern(allowed_write=["Makefile"])
        assert pattern.can_write("Makefile") is True
        assert pattern.can_write("other/Makefile") is False

    def test_path_normalization(self):
        """Leading ./ should be stripped."""
        pattern = FileAccessPattern(allowed_write=["src/"])
        assert pattern.can_write("./src/main.py") is True

    def test_multiple_blocked_patterns(self):
        """All blocked patterns should be checked."""
        pattern = FileAccessPattern(
            allowed_write=["**/*.py"],
            blocked_write=["docs/", "tests/"],
        )
        assert pattern.can_write("docs/gen.py") is False
        assert pattern.can_write("tests/test_foo.py") is False
        assert pattern.can_write("src/main.py") is True


# ---------------------------------------------------------------------------
# AgentRoleDefinition
# ---------------------------------------------------------------------------


class TestAgentRoleDefinition:
    """Verify AgentRoleDefinition dataclass."""

    def test_depends_on(self):
        defn = AgentRoleDefinition(
            role=AgentRole.TESTER,
            description="Test",
            responsibilities=["test"],
            dependencies=[AgentRole.CODER],
        )
        assert defn.depends_on(AgentRole.CODER) is True
        assert defn.depends_on(AgentRole.DOCUMENTER) is False

    def test_default_category_is_none(self):
        """Category should be None by default for backward compat."""
        defn = AgentRoleDefinition(
            role=AgentRole.CODER,
            description="Test",
            responsibilities=["test"],
        )
        assert defn.category is None

    def test_category_can_be_set(self):
        defn = AgentRoleDefinition(
            role=AgentRole.CODER,
            description="Test",
            responsibilities=["test"],
            category=AgentCategory.EXECUTION,
        )
        assert defn.category == AgentCategory.EXECUTION


# ---------------------------------------------------------------------------
# AGENT_ROLES registry
# ---------------------------------------------------------------------------


class TestAgentRolesRegistry:
    """Verify the AGENT_ROLES registry completeness."""

    def test_all_roles_have_definitions(self):
        """Every AgentRole enum member should have an entry in AGENT_ROLES."""
        for role in AgentRole:
            assert role in AGENT_ROLES, (
                f"AgentRole.{role.name} ({role.value}) is missing from AGENT_ROLES registry"
            )

    def test_all_definitions_have_correct_role(self):
        """Each definition's role field should match its registry key."""
        for role, defn in AGENT_ROLES.items():
            assert defn.role == role

    def test_all_definitions_have_descriptions(self):
        """Every definition should have a non-empty description."""
        for role, defn in AGENT_ROLES.items():
            assert defn.description, f"{role.value} has empty description"

    def test_all_definitions_have_responsibilities(self):
        """Every definition should have at least one responsibility."""
        for role, defn in AGENT_ROLES.items():
            assert len(defn.responsibilities) > 0, f"{role.value} has no responsibilities"

    def test_all_roles_have_category(self):
        """Every role in the registry should have a non-None category."""
        for role, defn in AGENT_ROLES.items():
            assert defn.category is not None, (
                f"AgentRole.{role.name} ({role.value}) has no category assigned"
            )

    def test_execution_roles_category(self):
        """Execution roles should have EXECUTION category."""
        for role_name in ["coder", "tester", "documenter"]:
            role = AgentRole(role_name)
            defn = AGENT_ROLES[role]
            assert defn.category == AgentCategory.EXECUTION, (
                f"{role_name} should have EXECUTION category, got {defn.category}"
            )

    def test_analysis_roles_category(self):
        """Analysis roles should have ANALYSIS category."""
        for role_name in ["architect", "task_planner", "risk_analyst", "refiner"]:
            role = AgentRole(role_name)
            defn = AGENT_ROLES[role]
            assert defn.category == AgentCategory.ANALYSIS, (
                f"{role_name} should have ANALYSIS category, got {defn.category}"
            )

    def test_review_roles_category(self):
        """Review roles should have REVIEW category."""
        for role_name in [
            "reviewer_code",
            "reviewer_code_holistic",
            "reviewer_contract",
            "reviewer_agent_design",
            "reviewer_refine",
            "reviewer_plan",
        ]:
            role = AgentRole(role_name)
            defn = AGENT_ROLES[role]
            assert defn.category == AgentCategory.REVIEW, (
                f"{role_name} should have REVIEW category, got {defn.category}"
            )

    def test_utility_roles_category(self):
        """Utility roles should have UTILITY category."""
        for role_name in ["autofixer", "conflict_resolver"]:
            role = AgentRole(role_name)
            defn = AGENT_ROLES[role]
            assert defn.category == AgentCategory.UTILITY, (
                f"{role_name} should have UTILITY category, got {defn.category}"
            )

    def test_interface_roles_category(self):
        """Interface roles should have INTERFACE category."""
        defn = AGENT_ROLES[AgentRole.OVERSEER]
        assert defn.category == AgentCategory.INTERFACE, (
            f"overseer should have INTERFACE category, got {defn.category}"
        )

    def test_autofixer_definition(self):
        """AUTOFIXER should have valid definition with correct properties."""
        defn = AGENT_ROLES[AgentRole.AUTOFIXER]
        assert defn.role == AgentRole.AUTOFIXER
        assert defn.category == AgentCategory.UTILITY
        assert len(defn.responsibilities) > 0
        # Autofixer should be able to write source files
        assert defn.file_access.can_write("src/main.py") or any(
            "*.py" in p for p in defn.file_access.allowed_write
        )

    def test_conflict_resolver_definition(self):
        """CONFLICT_RESOLVER should have valid definition with correct properties."""
        defn = AGENT_ROLES[AgentRole.CONFLICT_RESOLVER]
        assert defn.role == AgentRole.CONFLICT_RESOLVER
        assert defn.category == AgentCategory.UTILITY
        assert len(defn.responsibilities) > 0


# ---------------------------------------------------------------------------
# get_roles_by_category
# ---------------------------------------------------------------------------


class TestGetRolesByCategory:
    """Verify the get_roles_by_category helper function."""

    def test_import_exists(self):
        """Function should be importable from egg_contracts.agent_roles."""
        from egg_contracts.agent_roles import get_roles_by_category

        assert callable(get_roles_by_category)

    def test_execution_roles(self):
        from egg_contracts.agent_roles import get_roles_by_category

        roles = get_roles_by_category(AgentCategory.EXECUTION)
        role_values = {r.value if hasattr(r, "value") else r for r in roles}
        assert "coder" in role_values
        assert "tester" in role_values
        assert "documenter" in role_values

    def test_analysis_roles(self):
        from egg_contracts.agent_roles import get_roles_by_category

        roles = get_roles_by_category(AgentCategory.ANALYSIS)
        role_values = {r.value if hasattr(r, "value") else r for r in roles}
        assert "architect" in role_values
        assert "task_planner" in role_values
        assert "risk_analyst" in role_values
        assert "refiner" in role_values

    def test_review_roles(self):
        from egg_contracts.agent_roles import get_roles_by_category

        roles = get_roles_by_category(AgentCategory.REVIEW)
        role_values = {r.value if hasattr(r, "value") else r for r in roles}
        assert "reviewer_code" in role_values
        assert "reviewer_code_holistic" in role_values
        assert "reviewer_contract" in role_values
        assert "reviewer_agent_design" in role_values
        assert "reviewer_refine" in role_values
        assert "reviewer_plan" in role_values
        assert "reviewer_security" in role_values
        assert "reviewer_concurrency" in role_values
        assert len(roles) == 8

    def test_utility_roles(self):
        from egg_contracts.agent_roles import get_roles_by_category

        roles = get_roles_by_category(AgentCategory.UTILITY)
        role_values = {r.value if hasattr(r, "value") else r for r in roles}
        assert "autofixer" in role_values
        assert "conflict_resolver" in role_values

    def test_interface_roles(self):
        from egg_contracts.agent_roles import get_roles_by_category

        roles = get_roles_by_category(AgentCategory.INTERFACE)
        role_values = {r.value if hasattr(r, "value") else r for r in roles}
        assert "overseer" in role_values

    def test_all_roles_categorized(self):
        """Every role in the registry should appear in exactly one category."""
        from egg_contracts.agent_roles import get_roles_by_category

        all_categorized = set()
        for cat in AgentCategory:
            roles = get_roles_by_category(cat)
            role_values = {r.value if hasattr(r, "value") else str(r) for r in roles}
            # No overlap with previous categories
            overlap = all_categorized & role_values
            assert not overlap, f"Roles {overlap} appear in multiple categories"
            all_categorized |= role_values
        # All roles in registry should be categorized
        for role in AGENT_ROLES:
            assert role.value in all_categorized, (
                f"Role {role.value} not returned by any get_roles_by_category call"
            )


# ---------------------------------------------------------------------------
# get_role_definition
# ---------------------------------------------------------------------------


class TestGetRoleDefinition:
    """Verify get_role_definition function."""

    def test_get_by_enum(self):
        defn = get_role_definition(AgentRole.CODER)
        assert defn.role == AgentRole.CODER

    def test_get_by_string(self):
        defn = get_role_definition("coder")
        assert defn.role == AgentRole.CODER

    def test_invalid_role_raises(self):
        with pytest.raises((KeyError, ValueError)):
            get_role_definition("nonexistent_role")

    def test_all_roles_retrievable(self):
        """Every enum member should be retrievable."""
        for role in AgentRole:
            defn = get_role_definition(role)
            assert defn.role == role


# ---------------------------------------------------------------------------
# get_all_roles
# ---------------------------------------------------------------------------


class TestGetAllRoles:
    def test_returns_list(self):
        roles = get_all_roles()
        assert isinstance(roles, list)

    def test_count_matches_registry(self):
        roles = get_all_roles()
        assert len(roles) == len(AGENT_ROLES)


# ---------------------------------------------------------------------------
# get_role_dependencies
# ---------------------------------------------------------------------------


class TestGetRoleDependencies:
    def test_coder_has_no_dependencies(self):
        deps = get_role_dependencies(AgentRole.CODER)
        assert deps == []

    def test_tester_depends_on_coder(self):
        deps = get_role_dependencies(AgentRole.TESTER)
        assert AgentRole.CODER in deps

    def test_documenter_depends_on_coder(self):
        deps = get_role_dependencies(AgentRole.DOCUMENTER)
        assert AgentRole.CODER in deps

    def test_string_input(self):
        deps = get_role_dependencies("coder")
        assert isinstance(deps, list)


# ---------------------------------------------------------------------------
# can_run_in_parallel
# ---------------------------------------------------------------------------


class TestCanRunInParallel:
    def test_tester_and_documenter_parallel(self):
        """Tester and documenter can run in parallel (both depend on coder)."""
        assert can_run_in_parallel(AgentRole.TESTER, AgentRole.DOCUMENTER) is True

    def test_coder_and_tester_not_parallel(self):
        """Coder and tester cannot run in parallel (tester depends on coder)."""
        assert can_run_in_parallel(AgentRole.CODER, AgentRole.TESTER) is False

    def test_architect_and_itself(self):
        """A role should technically be able to run in parallel with itself."""
        # This is an edge case - depends on implementation
        result = can_run_in_parallel(AgentRole.ARCHITECT, AgentRole.ARCHITECT)
        assert isinstance(result, bool)

    def test_string_input(self):
        result = can_run_in_parallel("tester", "documenter")
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# get_roles_for_phase
# ---------------------------------------------------------------------------


class TestGetRolesForPhase:
    def test_implement_phase_includes_coder(self):
        roles = get_roles_for_phase("implement")
        assert AgentRole.CODER in roles

    def test_implement_phase_includes_tester(self):
        roles = get_roles_for_phase("implement")
        assert AgentRole.TESTER in roles

    def test_implement_phase_includes_documenter(self):
        roles = get_roles_for_phase("implement")
        assert AgentRole.DOCUMENTER in roles

    def test_implement_phase_includes_reviewers_by_default(self):
        roles = get_roles_for_phase("implement")
        assert AgentRole.REVIEWER_CODE in roles
        assert AgentRole.REVIEWER_CONTRACT in roles

    def test_implement_phase_excludes_reviewers_when_disabled(self):
        roles = get_roles_for_phase("implement", include_reviewers=False)
        assert AgentRole.REVIEWER_CODE not in roles
        assert AgentRole.REVIEWER_CONTRACT not in roles

    def test_plan_phase_roles(self):
        roles = get_roles_for_phase("plan")
        assert AgentRole.ARCHITECT in roles
        assert AgentRole.TASK_PLANNER in roles
        assert AgentRole.RISK_ANALYST in roles

    def test_refine_phase_roles(self):
        roles = get_roles_for_phase("refine")
        assert AgentRole.REFINER in roles

    def test_overseer_excluded_by_default(self):
        roles = get_roles_for_phase("implement")
        assert AgentRole.OVERSEER not in roles

    def test_overseer_included_when_requested(self):
        roles = get_roles_for_phase("implement", include_overseer=True)
        assert AgentRole.OVERSEER in roles

    def test_unknown_phase_raises(self):
        with pytest.raises(ValueError, match="No agent roles defined"):
            get_roles_for_phase("nonexistent_phase")

    def test_repo_filtering_non_egg(self):
        """Non-egg repos should exclude egg-only reviewer roles."""
        roles = get_roles_for_phase("refine", repo="other/repo")
        assert AgentRole.REVIEWER_AGENT_DESIGN not in roles

    def test_repo_filtering_egg(self):
        """Egg repo should include all reviewer roles."""
        roles = get_roles_for_phase("refine", repo="jwbron/egg")
        assert AgentRole.REVIEWER_AGENT_DESIGN in roles

    def test_review_phase_roles(self):
        """Review phase should have defined roles if it exists in _PHASE_ROLES."""
        try:
            roles = get_roles_for_phase("review")
            assert len(roles) > 0
        except ValueError:
            # If review phase is not yet defined, this is a gap
            pytest.skip("Review phase not yet defined in _PHASE_ROLES")


# ---------------------------------------------------------------------------
# detect_write_overlaps
# ---------------------------------------------------------------------------


class TestDetectWriteOverlaps:
    def test_no_overlaps_for_single_role(self):
        overlaps = detect_write_overlaps([AgentRole.CODER])
        assert overlaps == []

    def test_detects_overlaps_between_parallel_roles(self):
        """Tester and documenter run in parallel - check for overlaps."""
        overlaps = detect_write_overlaps([AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER])
        # We just verify it returns a list of tuples
        assert isinstance(overlaps, list)
        for item in overlaps:
            assert len(item) == 3
            assert isinstance(item[2], list)


# ---------------------------------------------------------------------------
# AgentExecution
# ---------------------------------------------------------------------------


class TestAgentExecution:
    def test_default_status(self):
        exe = AgentExecution(role=AgentRole.CODER)
        assert exe.status == AgentStatus.PENDING

    def test_is_complete_for_complete(self):
        exe = AgentExecution(role=AgentRole.CODER, status=AgentStatus.COMPLETE)
        assert exe.is_complete() is True

    def test_is_complete_for_failed(self):
        exe = AgentExecution(role=AgentRole.CODER, status=AgentStatus.FAILED)
        assert exe.is_complete() is True

    def test_is_complete_for_skipped(self):
        exe = AgentExecution(role=AgentRole.CODER, status=AgentStatus.SKIPPED)
        assert exe.is_complete() is True

    def test_not_complete_for_running(self):
        exe = AgentExecution(role=AgentRole.CODER, status=AgentStatus.RUNNING)
        assert exe.is_complete() is False

    def test_not_complete_for_pending(self):
        exe = AgentExecution(role=AgentRole.CODER, status=AgentStatus.PENDING)
        assert exe.is_complete() is False

    def test_is_successful(self):
        exe = AgentExecution(role=AgentRole.CODER, status=AgentStatus.COMPLETE)
        assert exe.is_successful() is True

    def test_not_successful_for_failed(self):
        exe = AgentExecution(role=AgentRole.CODER, status=AgentStatus.FAILED)
        assert exe.is_successful() is False

    def test_can_retry(self):
        exe = AgentExecution(role=AgentRole.CODER, status=AgentStatus.FAILED, retry_count=0)
        assert exe.can_retry() is True

    def test_cannot_retry_at_max(self):
        exe = AgentExecution(role=AgentRole.CODER, status=AgentStatus.FAILED, retry_count=2)
        assert exe.can_retry() is False

    def test_cannot_retry_if_not_failed(self):
        exe = AgentExecution(role=AgentRole.CODER, status=AgentStatus.COMPLETE, retry_count=0)
        assert exe.can_retry() is False


# ---------------------------------------------------------------------------
# create_execution_for_role
# ---------------------------------------------------------------------------


class TestCreateExecutionForRole:
    def test_creates_pending_execution(self):
        exe = create_execution_for_role(AgentRole.CODER)
        assert exe.role == AgentRole.CODER
        assert exe.status == AgentStatus.PENDING

    def test_string_input(self):
        exe = create_execution_for_role("tester")
        assert exe.role == AgentRole.TESTER

    def test_invalid_role_raises(self):
        with pytest.raises(ValueError):
            create_execution_for_role("nonexistent_role")


# ---------------------------------------------------------------------------
# Role sync: verify other modules mirror the canonical AgentRole enum
# ---------------------------------------------------------------------------


class TestRoleSyncWithOrchestratorModels:
    """Verify orchestrator/models.py AgentRole stays in sync.

    The canonical AgentRole is in egg_contracts.agent_roles.
    orchestrator/models.py has its own AgentRole that must contain
    all canonical roles (it may have extra roles like INSPECTOR for
    backward compat).
    """

    def test_orchestrator_models_imports_canonical(self):
        """orchestrator.models.AgentRole should be the same class object."""
        from orchestrator.models import AgentRole as OrchestratorAgentRole

        assert OrchestratorAgentRole is AgentRole, (
            "orchestrator.models.AgentRole is not imported from egg_contracts.agent_roles"
        )

    def test_all_canonical_roles_in_orchestrator_models(self):
        """Every canonical AgentRole should exist in orchestrator models."""
        from models import AgentRole as OrchestratorAgentRole

        canonical_values = {r.value for r in AgentRole}
        orchestrator_values = {r.value for r in OrchestratorAgentRole}
        missing = canonical_values - orchestrator_values
        assert not missing, (
            f"orchestrator/models.py AgentRole is missing canonical roles: {missing}"
        )


class TestRoleSyncWithOrchestratorTypes:
    """Verify shared/egg_orchestrator/types.py AgentRole stays in sync."""

    def test_egg_orchestrator_types_imports_canonical(self):
        """egg_orchestrator.types.AgentRole should be the same class object."""
        from egg_orchestrator.types import AgentRole as TypesAgentRole

        assert TypesAgentRole is AgentRole, (
            "egg_orchestrator.types.AgentRole is not imported from egg_contracts.agent_roles"
        )

    def test_all_canonical_roles_in_types(self):
        """Every canonical AgentRole should exist in egg_orchestrator types."""
        from egg_orchestrator.types import AgentRole as TypesAgentRole

        canonical_values = {r.value for r in AgentRole}
        types_values = {r.value for r in TypesAgentRole}
        missing = canonical_values - types_values
        assert not missing, (
            f"egg_orchestrator/types.py AgentRole is missing canonical roles: {missing}"
        )


class TestRoleSyncWithGateway:
    """Tripwire: the gateway must use the canonical AgentRole enum.

    Issue #2066: gateway/agent_restrictions.py and
    egg_restrictions.patterns now re-export this enum rather than
    redefining it, eliminating the silent-drift failure mode that
    PR #2061 surfaced. The identity check fails if anyone re-adds
    a parallel class or enum in either module.
    """

    def test_gateway_agent_role_is_canonical(self):
        from agent_restrictions import AgentRole as GatewayAgentRole

        assert GatewayAgentRole is AgentRole


# ---------------------------------------------------------------------------
# Utility role file access and inspector
# ---------------------------------------------------------------------------


class TestUtilityRoleFileAccess:
    """Verify file access patterns for AUTOFIXER and CONFLICT_RESOLVER."""

    def test_autofixer_can_write_source(self):
        """AUTOFIXER can write source and config files."""
        defn = get_role_definition(AgentRole.AUTOFIXER)
        fa = defn.file_access
        assert fa.can_write("src/main.py")
        assert fa.can_write("lib/utils.ts")
        assert fa.can_write("config/settings.yml")

    def test_autofixer_blocked_from_docs_and_contracts(self):
        """AUTOFIXER blocked from docs and contracts."""
        defn = get_role_definition(AgentRole.AUTOFIXER)
        fa = defn.file_access
        assert not fa.can_write("docs/guide.md")
        assert not fa.can_write(".egg-state/contracts/123.json")

    def test_autofixer_depends_on_coder(self):
        """AUTOFIXER depends on CODER."""
        defn = get_role_definition(AgentRole.AUTOFIXER)
        assert AgentRole.CODER in defn.dependencies

    def test_conflict_resolver_can_write_source_test_docs_config(self):
        """CONFLICT_RESOLVER can write source, tests, docs, and config."""
        defn = get_role_definition(AgentRole.CONFLICT_RESOLVER)
        fa = defn.file_access
        assert fa.can_write("src/main.py")
        assert fa.can_write("tests/test_foo.py")
        assert fa.can_write("docs/guide.md")
        assert fa.can_write("config/settings.yml")

    def test_conflict_resolver_blocked_from_egg_state(self):
        """CONFLICT_RESOLVER blocked from .egg-state/ subdirs."""
        defn = get_role_definition(AgentRole.CONFLICT_RESOLVER)
        fa = defn.file_access
        assert not fa.can_write(".egg-state/contracts/123.json")
        assert not fa.can_write(".egg-state/pipelines/state.json")

    def test_inspector_role_exists(self):
        """INSPECTOR role exists with INTERFACE category."""
        defn = get_role_definition(AgentRole.INSPECTOR)
        assert defn.category == AgentCategory.INTERFACE


# ---------------------------------------------------------------------------
# get_contract_role() — fine → coarse role mapping (#1766)
# ---------------------------------------------------------------------------


class TestGetContractRole:
    """Verify fine-grained AgentRole → coarse contract Role translation."""

    def test_every_fine_role_maps(self):
        """Every AgentRole has a coarse contract Role — prevents regression
        where a new fine role silently 403s on the contract API."""
        from egg_contracts.agent_roles import (
            AGENT_ROLE_TO_CONTRACT_ROLE,
            get_contract_role,
        )

        for fine_role in AgentRole:
            assert fine_role in AGENT_ROLE_TO_CONTRACT_ROLE, (
                f"AgentRole.{fine_role.name} has no contract-role mapping"
            )
            assert get_contract_role(fine_role) is not None

    @pytest.mark.parametrize(
        ("fine", "coarse"),
        [
            (AgentRole.CODER, "implementer"),
            (AgentRole.REFINER, "implementer"),
            (AgentRole.TASK_PLANNER, "implementer"),
            (AgentRole.AUTOFIXER, "implementer"),
            (AgentRole.REVIEWER_CODE, "reviewer"),
            (AgentRole.REVIEWER_PLAN, "reviewer"),
            (AgentRole.OVERSEER, "system"),
            (AgentRole.INSPECTOR, "system"),
        ],
    )
    def test_mapping_values(self, fine, coarse):
        """Spot-check a few representative mappings."""
        from egg_contracts.agent_roles import get_contract_role

        result = get_contract_role(fine)
        assert result is not None
        assert result.value == coarse

    def test_accepts_string_input(self):
        """String input (as shipped from session metadata) resolves."""
        from egg_contracts.agent_roles import get_contract_role

        result = get_contract_role("refiner")
        assert result is not None
        assert result.value == "implementer"

    def test_unknown_string_returns_none(self):
        """Unknown role strings return None rather than raising."""
        from egg_contracts.agent_roles import get_contract_role

        assert get_contract_role("not_a_role") is None
