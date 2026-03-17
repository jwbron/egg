"""Tests for agent role definitions, categories, and roster consistency.

Validates:
- All roles have a category assigned
- Category groupings are correct
- get_roles_by_category returns expected sets
- New utility roles (AUTOFIXER, CONFLICT_RESOLVER) have valid definitions
- AgentRole enum is the canonical source (sync check)
"""

from egg_contracts.agent_roles import (
    AGENT_ROLES,
    AgentCategory,
    AgentRole,
    AgentRoleDefinition,
    get_role_definition,
    get_roles_by_category,
)


class TestAgentCategory:
    """Tests for AgentCategory enum."""

    def test_all_categories_exist(self):
        """All five categories are defined."""
        assert AgentCategory.EXECUTION == "execution"
        assert AgentCategory.ANALYSIS == "analysis"
        assert AgentCategory.REVIEW == "review"
        assert AgentCategory.UTILITY == "utility"
        assert AgentCategory.INTERFACE == "interface"
        assert len(AgentCategory) == 5

    def test_all_roles_have_category(self):
        """Every role in AGENT_ROLES has a non-None category."""
        for role, defn in AGENT_ROLES.items():
            assert defn.category is not None, f"Role {role.value} has no category assigned"

    def test_category_is_agent_category_instance(self):
        """All categories are AgentCategory enum values."""
        for role, defn in AGENT_ROLES.items():
            assert isinstance(defn.category, AgentCategory), (
                f"Role {role.value} category is {type(defn.category)}, expected AgentCategory"
            )


class TestGetRolesByCategory:
    """Tests for get_roles_by_category helper."""

    def test_execution_roles(self):
        """Execution category contains coder, tester, documenter."""
        roles = get_roles_by_category(AgentCategory.EXECUTION)
        assert AgentRole.CODER in roles
        assert AgentRole.TESTER in roles
        assert AgentRole.DOCUMENTER in roles
        assert len(roles) == 3

    def test_analysis_roles(self):
        """Analysis category contains architect, task_planner, risk_analyst, refiner."""
        roles = get_roles_by_category(AgentCategory.ANALYSIS)
        assert AgentRole.ARCHITECT in roles
        assert AgentRole.TASK_PLANNER in roles
        assert AgentRole.RISK_ANALYST in roles
        assert AgentRole.REFINER in roles
        assert len(roles) == 4

    def test_review_roles(self):
        """Review category contains all 5 reviewer subtypes."""
        roles = get_roles_by_category(AgentCategory.REVIEW)
        assert AgentRole.REVIEWER_CODE in roles
        assert AgentRole.REVIEWER_CONTRACT in roles
        assert AgentRole.REVIEWER_AGENT_DESIGN in roles
        assert AgentRole.REVIEWER_REFINE in roles
        assert AgentRole.REVIEWER_PLAN in roles
        assert len(roles) == 5

    def test_utility_roles(self):
        """Utility category contains autofixer and conflict_resolver."""
        roles = get_roles_by_category(AgentCategory.UTILITY)
        assert AgentRole.AUTOFIXER in roles
        assert AgentRole.CONFLICT_RESOLVER in roles
        assert len(roles) == 2

    def test_interface_roles(self):
        """Interface category contains overseer and inspector."""
        roles = get_roles_by_category(AgentCategory.INTERFACE)
        assert AgentRole.OVERSEER in roles
        assert AgentRole.INSPECTOR in roles
        assert len(roles) == 2

    def test_all_roles_covered_by_categories(self):
        """Every role in the enum appears in exactly one category."""
        all_categorized = set()
        for cat in AgentCategory:
            roles = get_roles_by_category(cat)
            for role in roles:
                assert role not in all_categorized, (
                    f"Role {role.value} appears in multiple categories"
                )
                all_categorized.add(role)

        # Every role in AGENT_ROLES should be categorized
        for role in AGENT_ROLES:
            assert role in all_categorized, f"Role {role.value} is not in any category"


class TestNewUtilityRoles:
    """Tests for AUTOFIXER and CONFLICT_RESOLVER roles."""

    def test_autofixer_enum_value(self):
        """AUTOFIXER has correct string value."""
        assert AgentRole.AUTOFIXER == "autofixer"

    def test_conflict_resolver_enum_value(self):
        """CONFLICT_RESOLVER has correct string value."""
        assert AgentRole.CONFLICT_RESOLVER == "conflict_resolver"

    def test_autofixer_definition_exists(self):
        """AUTOFIXER has a valid role definition."""
        defn = get_role_definition(AgentRole.AUTOFIXER)
        assert isinstance(defn, AgentRoleDefinition)
        assert defn.role == AgentRole.AUTOFIXER
        assert defn.category == AgentCategory.UTILITY
        assert len(defn.responsibilities) > 0
        assert defn.description != ""

    def test_conflict_resolver_definition_exists(self):
        """CONFLICT_RESOLVER has a valid role definition."""
        defn = get_role_definition(AgentRole.CONFLICT_RESOLVER)
        assert isinstance(defn, AgentRoleDefinition)
        assert defn.role == AgentRole.CONFLICT_RESOLVER
        assert defn.category == AgentCategory.UTILITY
        assert len(defn.responsibilities) > 0
        assert defn.description != ""

    def test_autofixer_file_access(self):
        """AUTOFIXER can write source/config, blocked from docs and contracts."""
        defn = get_role_definition(AgentRole.AUTOFIXER)
        fa = defn.file_access
        # Can write source code
        assert fa.can_write("src/main.py")
        assert fa.can_write("lib/utils.ts")
        assert fa.can_write("config/settings.yml")
        # Blocked from docs
        assert not fa.can_write("docs/guide.md")
        # Blocked from contracts
        assert not fa.can_write(".egg-state/contracts/123.json")

    def test_conflict_resolver_file_access(self):
        """CONFLICT_RESOLVER can write source+test+docs+config, blocked from .egg-state/."""
        defn = get_role_definition(AgentRole.CONFLICT_RESOLVER)
        fa = defn.file_access
        # Can write source, tests, docs, config
        assert fa.can_write("src/main.py")
        assert fa.can_write("tests/test_foo.py")
        assert fa.can_write("docs/guide.md")
        assert fa.can_write("config/settings.yml")
        # Blocked from .egg-state/
        assert not fa.can_write(".egg-state/contracts/123.json")
        assert not fa.can_write(".egg-state/pipelines/state.json")

    def test_autofixer_depends_on_coder(self):
        """AUTOFIXER depends on CODER."""
        defn = get_role_definition(AgentRole.AUTOFIXER)
        assert AgentRole.CODER in defn.dependencies


class TestRosterCompleteness:
    """Tests for overall roster consistency."""

    def test_total_role_count(self):
        """AgentRole enum has exactly 16 members."""
        assert len(AgentRole) == 16

    def test_agent_roles_registry_matches_enum(self):
        """AGENT_ROLES dict has an entry for every role in the enum."""
        for role in AgentRole:
            assert role in AGENT_ROLES, f"Role {role.value} missing from AGENT_ROLES registry"

    def test_role_definition_role_matches_key(self):
        """Each AGENT_ROLES entry has a definition whose .role matches the key."""
        for role, defn in AGENT_ROLES.items():
            assert defn.role == role, (
                f"AGENT_ROLES key {role.value} doesn't match definition role {defn.role.value}"
            )

    def test_inspector_role_exists(self):
        """INSPECTOR role exists with INTERFACE category."""
        defn = get_role_definition(AgentRole.INSPECTOR)
        assert defn.category == AgentCategory.INTERFACE


class TestCanonicalSourceSync:
    """Tests verifying other modules import from the canonical source."""

    def test_orchestrator_models_imports_canonical(self):
        """orchestrator.models.AgentRole should be the same class as egg_contracts."""
        from orchestrator.models import AgentRole as OrchestratorAgentRole

        # They should be literally the same class object
        assert OrchestratorAgentRole is AgentRole, (
            "orchestrator.models.AgentRole is not imported from egg_contracts.agent_roles"
        )

    def test_egg_orchestrator_types_imports_canonical(self):
        """egg_orchestrator.types.AgentRole should be the same class as egg_contracts."""
        from egg_orchestrator.types import AgentRole as TypesAgentRole

        # They should be literally the same class object
        assert TypesAgentRole is AgentRole, (
            "egg_orchestrator.types.AgentRole is not imported from egg_contracts.agent_roles"
        )
