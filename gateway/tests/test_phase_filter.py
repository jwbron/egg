"""
Tests for Phase Filter module.

Tests cover:
- Operation matching
- Phase permission loading
- Blocked/allowed operation filtering
- Exit requirements
"""

import json
import tempfile
from pathlib import Path

import phase_filter
import pytest
from phase_filter import (
    FileRestriction,
    FileRestrictionResult,
    Operation,
    OperationType,
    PhaseFilter,
    PhasePermissions,
    PipelinePhase,
    check_file_restrictions,
    filter_operation,
    is_operation_blocked,
    reset_phase_filter,
)


class TestOperation:
    """Tests for Operation class."""

    def test_matches_exact(self):
        """Exact pattern matches exact command."""
        op = Operation(OperationType.GIT, "push origin main")
        assert op.matches("push origin main") is True
        assert op.matches("push origin develop") is False

    def test_matches_wildcard(self):
        """Wildcard pattern matches multiple commands."""
        op = Operation(OperationType.GIT, "push *")
        assert op.matches("push origin main") is True
        assert op.matches("push upstream develop") is True
        assert op.matches("pull origin main") is False

    def test_matches_multiple_wildcards(self):
        """Multiple wildcards work correctly."""
        op = Operation(OperationType.GH, "issue * *")
        assert op.matches("issue comment 123") is True
        assert op.matches("issue edit 456") is True
        assert op.matches("pr comment 123") is False


class TestPhasePermissions:
    """Tests for PhasePermissions class."""

    def test_from_dict_basic(self):
        """Create PhasePermissions from dictionary."""
        data = {
            "allowed_operations": [
                {"type": "git", "pattern": "push *", "description": "Push code"}
            ],
            "blocked_operations": [
                {"type": "gh", "pattern": "pr create *", "description": "No PRs"}
            ],
            "exit_requires": "reviewer",
        }
        permissions = PhasePermissions.from_dict(data)

        assert len(permissions.allowed_operations) == 1
        assert len(permissions.blocked_operations) == 1
        assert permissions.exit_requires == "reviewer"
        assert permissions.allowed_operations[0].type == OperationType.GIT
        assert permissions.blocked_operations[0].pattern == "pr create *"

    def test_from_dict_empty_lists(self):
        """Handle empty operation lists."""
        data = {
            "allowed_operations": [],
            "blocked_operations": [],
            "exit_requires": "human",
        }
        permissions = PhasePermissions.from_dict(data)

        assert len(permissions.allowed_operations) == 0
        assert len(permissions.blocked_operations) == 0


class TestPhaseFilter:
    """Tests for PhaseFilter class."""

    @pytest.fixture
    def custom_permissions_file(self) -> Path:
        """Create a temporary permissions file."""
        permissions = {
            "schemaVersion": "1.0",
            "phases": {
                "refine": {
                    "allowed_operations": [
                        {"type": "gh", "pattern": "issue comment *", "description": "Comment"},
                    ],
                    "blocked_operations": [
                        {"type": "git", "pattern": "push *", "description": "No push"},
                    ],
                    "exit_requires": "human",
                },
                "implement": {
                    "allowed_operations": [
                        {"type": "git", "pattern": "push *", "description": "Push code"},
                    ],
                    "blocked_operations": [
                        {"type": "gh", "pattern": "pr create *", "description": "No PR yet"},
                    ],
                    "exit_requires": "reviewer",
                },
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(permissions, f)
            return Path(f.name)

    def test_load_from_file(self, custom_permissions_file: Path):
        """Load permissions from a file."""
        pf = PhaseFilter(permissions_path=custom_permissions_file)
        permissions = pf.get_permissions(PipelinePhase.REFINE)

        assert permissions is not None
        assert permissions.exit_requires == "human"
        assert len(permissions.blocked_operations) == 1

    def test_default_permissions_when_no_file(self):
        """Use default permissions when file doesn't exist."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent/path.json"))
        permissions = pf.get_permissions(PipelinePhase.IMPLEMENT)

        assert permissions is not None
        assert permissions.exit_requires == "reviewer"

    def test_filter_blocked_operation(self, custom_permissions_file: Path):
        """Blocked operations are correctly identified."""
        pf = PhaseFilter(permissions_path=custom_permissions_file)
        result = pf.filter_operation(
            PipelinePhase.REFINE,
            OperationType.GIT,
            "push origin main",
        )

        assert result.allowed is False
        assert result.phase == PipelinePhase.REFINE
        assert result.operation_type == OperationType.GIT
        assert "push" in result.message.lower()

    def test_filter_allowed_operation(self, custom_permissions_file: Path):
        """Allowed operations are correctly identified."""
        pf = PhaseFilter(permissions_path=custom_permissions_file)
        result = pf.filter_operation(
            PipelinePhase.REFINE,
            OperationType.GH,
            "issue comment 123",
        )

        assert result.allowed is True

    def test_filter_not_explicitly_blocked(self, custom_permissions_file: Path):
        """Operations not explicitly blocked are allowed."""
        pf = PhaseFilter(permissions_path=custom_permissions_file)
        result = pf.filter_operation(
            PipelinePhase.REFINE,
            OperationType.EGG_CONTRACT,
            "show",
        )

        # Not in blocked list, so allowed
        assert result.allowed is True

    def test_is_operation_blocked_helper(self, custom_permissions_file: Path):
        """is_operation_blocked helper works correctly."""
        pf = PhaseFilter(permissions_path=custom_permissions_file)

        assert (
            pf.is_operation_blocked(PipelinePhase.REFINE, OperationType.GIT, "push origin main")
            is True
        )
        assert (
            pf.is_operation_blocked(PipelinePhase.IMPLEMENT, OperationType.GIT, "push origin main")
            is False
        )

    def test_get_exit_requirement(self, custom_permissions_file: Path):
        """Get exit requirement for a phase."""
        pf = PhaseFilter(permissions_path=custom_permissions_file)

        assert pf.get_exit_requirement(PipelinePhase.REFINE) == "human"
        assert pf.get_exit_requirement(PipelinePhase.IMPLEMENT) == "reviewer"


class TestFilterOperationFunction:
    """Tests for the convenience filter_operation function."""

    def test_filter_with_strings(self):
        """filter_operation accepts strings."""
        # Reset global filter to use defaults
        phase_filter._filter = None

        result = filter_operation("implement", "gh", "pr create")

        assert result.allowed is False
        assert "pr create" in result.message.lower() or "pr" in str(result.blocked_reason).lower()

    def test_filter_with_enums(self):
        """filter_operation accepts enums."""
        phase_filter._filter = None

        result = filter_operation(
            PipelinePhase.IMPLEMENT,
            OperationType.GIT,
            "push origin main",
        )

        assert result.allowed is True


class TestIsOperationBlockedFunction:
    """Tests for the convenience is_operation_blocked function."""

    def test_push_allowed_during_refine(self):
        """Git push is allowed during refine phase (issue #543 change)."""
        # Previously blocked, now allowed with file restrictions
        phase_filter._filter = None

        assert is_operation_blocked("refine", "git", "push origin main") is False

    def test_allowed_during_implement(self):
        """Git push is allowed during implement phase."""
        phase_filter._filter = None

        assert is_operation_blocked("implement", "git", "push origin main") is False

    def test_pr_create_blocked_until_pr_phase(self):
        """PR create is blocked until PR phase."""
        phase_filter._filter = None

        assert is_operation_blocked("refine", "gh", "pr create") is True
        assert is_operation_blocked("plan", "gh", "pr create") is True
        assert is_operation_blocked("implement", "gh", "pr create") is True
        assert is_operation_blocked("pr", "gh", "pr create") is False


class TestDefaultPermissions:
    """Tests for default permission configuration."""

    @pytest.fixture(autouse=True)
    def reset_filter(self):
        """Reset the global filter before each test."""
        phase_filter._filter = None
        yield
        phase_filter._filter = None

    def test_refine_phase_allows_push(self):
        """Refine phase allows git push (with file restrictions applied separately)."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(PipelinePhase.REFINE, OperationType.GIT, "push origin main")
        assert result.allowed is True

    def test_refine_phase_blocks_pr_create(self):
        """Refine phase blocks PR creation."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(PipelinePhase.REFINE, OperationType.GH, "pr create")
        assert result.allowed is False

    def test_plan_phase_allows_push(self):
        """Plan phase allows git push (with file restrictions applied separately)."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(PipelinePhase.PLAN, OperationType.GIT, "push origin main")
        assert result.allowed is True

    def test_implement_phase_allows_push(self):
        """Implement phase allows git push."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(PipelinePhase.IMPLEMENT, OperationType.GIT, "push origin main")
        assert result.allowed is True

    def test_implement_phase_blocks_pr_create(self):
        """Implement phase blocks PR creation."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(PipelinePhase.IMPLEMENT, OperationType.GH, "pr create")
        assert result.allowed is False

    def test_pr_phase_allows_pr_create(self):
        """PR phase allows PR creation."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(PipelinePhase.PR, OperationType.GH, "pr create")
        assert result.allowed is True

    def test_pr_phase_allows_push(self):
        """PR phase allows git push."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(PipelinePhase.PR, OperationType.GIT, "push origin main")
        assert result.allowed is True


class TestResetPhaseFilter:
    """Tests for reset_phase_filter function."""

    def test_reset_clears_cached_filter(self):
        """reset_phase_filter clears the cached filter instance."""
        # Access the filter to cache it
        from phase_filter import get_phase_filter

        _ = get_phase_filter()
        assert phase_filter._filter is not None

        # Reset should clear it
        reset_phase_filter()
        assert phase_filter._filter is None

    def test_reset_allows_new_instance(self):
        """After reset, get_phase_filter creates a new instance."""
        from phase_filter import get_phase_filter

        filter1 = get_phase_filter()
        reset_phase_filter()
        filter2 = get_phase_filter()

        assert filter1 is not filter2


class TestPatternEdgeCases:
    """Tests for pattern matching edge cases."""

    @pytest.fixture(autouse=True)
    def reset_filter(self):
        """Reset the global filter before each test."""
        phase_filter._filter = None
        yield
        phase_filter._filter = None

    def test_pr_create_without_args_matches_pattern(self):
        """'pr create' without arguments matches 'pr create*' pattern."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        # In refine phase, pr create should be blocked
        result = pf.filter_operation(PipelinePhase.REFINE, OperationType.GH, "pr create")
        assert result.allowed is False

        # In pr phase, pr create should be allowed
        result = pf.filter_operation(PipelinePhase.PR, OperationType.GH, "pr create")
        assert result.allowed is True

    def test_pr_create_with_args_matches_pattern(self):
        """'pr create --title foo' matches 'pr create*' pattern."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(
            PipelinePhase.PR, OperationType.GH, "pr create --title 'Test PR'"
        )
        assert result.allowed is True

    def test_partial_command_does_not_match_blocked_pattern(self):
        """Commands that partially match blocked patterns should not be blocked."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        # 'push-status' should not match 'push *' pattern
        result = pf.filter_operation(PipelinePhase.REFINE, OperationType.GIT, "push-status")
        # push-status doesn't match "push *" because there's no space after push
        assert result.allowed is True

    def test_git_push_without_remote_matches_pattern(self):
        """'git push' without remote matches 'push *' pattern."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        # Note: 'push *' requires at least one character after 'push '
        # 'push' alone won't match 'push *', it would need 'push something'
        result = pf.filter_operation(PipelinePhase.IMPLEMENT, OperationType.GIT, "push origin")
        assert result.allowed is True

    def test_pr_creates_typo_not_blocked(self):
        """'pr creates' (typo) should not be blocked by 'pr create*' pattern."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        # 'pr creates' matches 'pr create*' because * matches 's'
        result = pf.filter_operation(PipelinePhase.REFINE, OperationType.GH, "pr creates")
        assert result.allowed is False  # It does match, so it's blocked in refine


class TestFileRestriction:
    """Tests for FileRestriction dataclass."""

    def test_from_dict_basic(self):
        """Create FileRestriction from dictionary."""
        data = {
            "role": "implementer",
            "blocked_patterns": [".egg-state/contracts/"],
            "blocked_reason": "Contract files are protected",
        }
        restriction = FileRestriction.from_dict(data)

        assert restriction.role == "implementer"
        assert restriction.blocked_patterns == [".egg-state/contracts/"]
        assert restriction.blocked_reason == "Contract files are protected"

    def test_is_file_blocked_matching_pattern(self):
        """Files matching blocked pattern should be detected."""
        restriction = FileRestriction(
            role="implementer",
            blocked_patterns=[".egg-state/contracts/"],
            blocked_reason="Protected",
        )

        assert restriction.is_file_blocked(".egg-state/contracts/123.json") is True
        assert restriction.is_file_blocked(".egg-state/contracts/456.json") is True

    def test_is_file_blocked_non_matching(self):
        """Files not matching pattern should not be blocked."""
        restriction = FileRestriction(
            role="implementer",
            blocked_patterns=[".egg-state/contracts/"],
            blocked_reason="Protected",
        )

        assert restriction.is_file_blocked("src/main.py") is False
        assert restriction.is_file_blocked("README.md") is False
        assert restriction.is_file_blocked(".egg-state/drafts/plan.md") is False

    def test_path_normalization_leading_dot_slash(self):
        """Leading ./ should be normalized."""
        restriction = FileRestriction(
            role="implementer",
            blocked_patterns=[".egg-state/contracts/"],
            blocked_reason="Protected",
        )

        # ./path should be normalized to path
        assert restriction.is_file_blocked("./.egg-state/contracts/123.json") is True

    def test_path_normalization_double_slash(self):
        """Double slashes should be normalized."""
        restriction = FileRestriction(
            role="implementer",
            blocked_patterns=[".egg-state/contracts/"],
            blocked_reason="Protected",
        )

        # Double slashes should be normalized
        assert restriction.is_file_blocked(".egg-state//contracts/123.json") is True

    def test_multiple_patterns(self):
        """Multiple blocked patterns should all be checked."""
        restriction = FileRestriction(
            role="implementer",
            blocked_patterns=[".egg-state/contracts/", "secrets/"],
            blocked_reason="Protected",
        )

        assert restriction.is_file_blocked(".egg-state/contracts/123.json") is True
        assert restriction.is_file_blocked("secrets/api_key.txt") is True
        assert restriction.is_file_blocked("src/main.py") is False


class TestFileRestrictionResult:
    """Tests for FileRestrictionResult dataclass."""

    def test_allow_factory(self):
        """FileRestrictionResult.allow creates allowed result."""
        result = FileRestrictionResult.allow("Files allowed")

        assert result.allowed is True
        assert result.message == "Files allowed"
        assert result.blocked_files == []

    def test_block_factory(self):
        """FileRestrictionResult.block creates blocked result."""
        result = FileRestrictionResult.block(
            message="Files blocked",
            role="implementer",
            blocked_files=[".egg-state/contracts/123.json"],
            blocked_reason="Contract files protected",
        )

        assert result.allowed is False
        assert result.message == "Files blocked"
        assert result.role == "implementer"
        assert result.blocked_files == [".egg-state/contracts/123.json"]
        assert result.blocked_reason == "Contract files protected"


class TestPhaseFilterFileRestrictions:
    """Tests for PhaseFilter file restriction methods."""

    @pytest.fixture
    def permissions_file_with_restrictions(self) -> Path:
        """Create a temporary permissions file with file restrictions."""
        permissions = {
            "schemaVersion": "1.0",
            "phases": {
                "implement": {
                    "allowed_operations": [
                        {"type": "git", "pattern": "push *", "description": "Push code"},
                    ],
                    "blocked_operations": [],
                    "exit_requires": "reviewer",
                },
            },
            "file_restrictions": [
                {
                    "role": "implementer",
                    "blocked_patterns": [".egg-state/contracts/"],
                    "blocked_reason": "Contract files can only be modified through the contract API",
                },
            ],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(permissions, f)
            return Path(f.name)

    def test_load_file_restrictions_from_file(self, permissions_file_with_restrictions: Path):
        """File restrictions should be loaded from JSON file."""
        pf = PhaseFilter(permissions_path=permissions_file_with_restrictions)
        restrictions = pf.get_file_restrictions()

        assert len(restrictions) == 1
        assert restrictions[0].role == "implementer"
        assert ".egg-state/contracts/" in restrictions[0].blocked_patterns

    def test_get_file_restrictions_for_role(self, permissions_file_with_restrictions: Path):
        """Should return restrictions for specific role."""
        pf = PhaseFilter(permissions_path=permissions_file_with_restrictions)

        implementer_restrictions = pf.get_file_restrictions_for_role("implementer")
        assert len(implementer_restrictions) == 1

        reviewer_restrictions = pf.get_file_restrictions_for_role("reviewer")
        assert len(reviewer_restrictions) == 0

    def test_get_file_restrictions_for_role_case_insensitive(
        self, permissions_file_with_restrictions: Path
    ):
        """Role matching should be case-insensitive."""
        pf = PhaseFilter(permissions_path=permissions_file_with_restrictions)

        restrictions = pf.get_file_restrictions_for_role("IMPLEMENTER")
        assert len(restrictions) == 1

        restrictions = pf.get_file_restrictions_for_role("Implementer")
        assert len(restrictions) == 1

    def test_check_file_restrictions_blocked(self, permissions_file_with_restrictions: Path):
        """Should block files matching restriction patterns."""
        pf = PhaseFilter(permissions_path=permissions_file_with_restrictions)

        result = pf.check_file_restrictions(
            "implementer",
            ["src/main.py", ".egg-state/contracts/123.json", "README.md"],
        )

        assert result.allowed is False
        assert ".egg-state/contracts/123.json" in result.blocked_files
        assert len(result.blocked_files) == 1
        assert result.role == "implementer"

    def test_check_file_restrictions_allowed(self, permissions_file_with_restrictions: Path):
        """Should allow files not matching restriction patterns."""
        pf = PhaseFilter(permissions_path=permissions_file_with_restrictions)

        result = pf.check_file_restrictions(
            "implementer",
            ["src/main.py", "tests/test_main.py", "README.md"],
        )

        assert result.allowed is True
        assert result.blocked_files == []

    def test_check_file_restrictions_multiple_blocked(
        self, permissions_file_with_restrictions: Path
    ):
        """Should detect multiple blocked files."""
        pf = PhaseFilter(permissions_path=permissions_file_with_restrictions)

        result = pf.check_file_restrictions(
            "implementer",
            [".egg-state/contracts/123.json", ".egg-state/contracts/456.json", "src/code.py"],
        )

        assert result.allowed is False
        assert len(result.blocked_files) == 2
        assert ".egg-state/contracts/123.json" in result.blocked_files
        assert ".egg-state/contracts/456.json" in result.blocked_files

    def test_check_file_restrictions_no_restrictions_for_role(
        self, permissions_file_with_restrictions: Path
    ):
        """Roles without restrictions should be allowed."""
        pf = PhaseFilter(permissions_path=permissions_file_with_restrictions)

        result = pf.check_file_restrictions(
            "reviewer",
            [".egg-state/contracts/123.json"],  # Would be blocked for implementer
        )

        assert result.allowed is True

    def test_check_file_restrictions_empty_role(self, permissions_file_with_restrictions: Path):
        """Empty role should be allowed."""
        pf = PhaseFilter(permissions_path=permissions_file_with_restrictions)

        result = pf.check_file_restrictions(
            "",
            [".egg-state/contracts/123.json"],
        )

        assert result.allowed is True

    def test_check_file_restrictions_empty_files(self, permissions_file_with_restrictions: Path):
        """Empty file list should be allowed."""
        pf = PhaseFilter(permissions_path=permissions_file_with_restrictions)

        result = pf.check_file_restrictions("implementer", [])

        assert result.allowed is True


class TestDefaultFileRestrictions:
    """Tests for default file restrictions (when no config file)."""

    @pytest.fixture(autouse=True)
    def reset_filter(self):
        """Reset the global filter before each test."""
        phase_filter._filter = None
        yield
        phase_filter._filter = None

    def test_default_restrictions_block_implementer_contracts(self):
        """Default restrictions should block implementer from modifying contracts."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))

        result = pf.check_file_restrictions(
            "implementer",
            [".egg-state/contracts/123.json"],
        )

        assert result.allowed is False
        assert ".egg-state/contracts/123.json" in result.blocked_files

    def test_default_restrictions_allow_reviewer_contracts(self):
        """Default restrictions should allow reviewer to modify contracts."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))

        result = pf.check_file_restrictions(
            "reviewer",
            [".egg-state/contracts/123.json"],
        )

        assert result.allowed is True

    def test_defaults_applied_when_config_lacks_file_restrictions_key(self):
        """Default restrictions should be applied when config exists but lacks file_restrictions.

        SECURITY: This ensures protection for legacy configs that predate the
        file_restrictions feature. Without this, a config file missing the
        file_restrictions key would silently disable all file restrictions.
        """
        # Create a config file WITHOUT the file_restrictions key
        permissions = {
            "schemaVersion": "1.0",
            "phases": {
                "implement": {
                    "allowed_operations": [
                        {"type": "git", "pattern": "push *", "description": "Push code"},
                    ],
                    "blocked_operations": [],
                    "exit_requires": "reviewer",
                },
            },
            # Intentionally NO file_restrictions key
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(permissions, f)
            temp_path = Path(f.name)

        try:
            pf = PhaseFilter(permissions_path=temp_path)

            # Should use default restrictions when key is missing
            result = pf.check_file_restrictions(
                "implementer",
                [".egg-state/contracts/123.json"],
            )

            # Default restrictions should block implementer from contracts
            assert result.allowed is False
            assert ".egg-state/contracts/123.json" in result.blocked_files
        finally:
            temp_path.unlink()


class TestCheckFileRestrictionsFunction:
    """Tests for the convenience check_file_restrictions function."""

    @pytest.fixture(autouse=True)
    def reset_filter(self):
        """Reset the global filter before each test."""
        phase_filter._filter = None
        yield
        phase_filter._filter = None

    def test_convenience_function_blocks_implementer(self):
        """Module-level function should block implementer from contracts."""
        result = check_file_restrictions(
            "implementer",
            [".egg-state/contracts/123.json"],
        )

        assert result.allowed is False

    def test_convenience_function_allows_reviewer(self):
        """Module-level function should allow reviewer to modify contracts."""
        result = check_file_restrictions(
            "reviewer",
            [".egg-state/contracts/123.json"],
        )

        assert result.allowed is True

    def test_convenience_function_allows_normal_files(self):
        """Module-level function should allow normal files for implementer."""
        result = check_file_restrictions(
            "implementer",
            ["src/main.py", "README.md"],
        )

        assert result.allowed is True


class TestPhaseFileRestrictions:
    """Tests for phase-based file restrictions (issue #543)."""

    def setup_method(self):
        """Reset the global filter instance before each test."""
        import phase_filter

        phase_filter._filter = None

    def teardown_method(self):
        """Reset after each test."""
        import phase_filter

        phase_filter._filter = None

    def test_refine_phase_allows_contracts(self):
        """Refine phase should allow .egg-state/contracts/ files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "refine",
            [".egg-state/contracts/123.json"],
        )

        assert result.allowed is True

    def test_refine_phase_allows_analysis_drafts(self):
        """Refine phase should allow analysis draft files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "refine",
            [".egg-state/drafts/local-abc-analysis.md"],
        )

        assert result.allowed is True

    def test_refine_phase_allows_checkpoints(self):
        """Refine phase should allow checkpoint files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "refine",
            [".egg-state/checkpoints/commit-abc123.json"],
        )

        assert result.allowed is True

    def test_refine_phase_blocks_code_files(self):
        """Refine phase should block code files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "refine",
            ["src/main.py", "README.md"],
        )

        assert result.allowed is False

    def test_plan_phase_allows_plan_drafts(self):
        """Plan phase should allow plan draft files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "plan",
            [".egg-state/drafts/local-abc-plan.md"],
        )

        assert result.allowed is True

    def test_plan_phase_blocks_analysis_drafts(self):
        """Plan phase should block analysis draft files (not plan)."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "plan",
            [".egg-state/drafts/local-abc-analysis.md"],
        )

        # Plan phase only allows *plan* drafts, not *analysis*
        assert result.allowed is False

    def test_implement_phase_allows_code(self):
        """Implement phase should allow code files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "implement",
            ["src/main.py", "tests/test_main.py", "README.md"],
        )

        assert result.allowed is True

    def test_implement_phase_blocks_contracts(self):
        """Implement phase should block contract files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "implement",
            [".egg-state/contracts/123.json"],
        )

        assert result.allowed is False

    def test_implement_phase_blocks_drafts(self):
        """Implement phase should block draft files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "implement",
            [".egg-state/drafts/plan.md"],
        )

        assert result.allowed is False

    def test_implement_phase_allows_checkpoints(self):
        """Implement phase should allow checkpoint files (not in blocked list)."""
        from phase_filter import check_phase_file_restrictions

        # Checkpoints are allowed because they don't match any blocked pattern
        result = check_phase_file_restrictions(
            "implement",
            [".egg-state/checkpoints/commit-abc123.json"],
        )

        assert result.allowed is True

    def test_pr_phase_allows_everything(self):
        """PR phase should allow all files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "pr",
            [
                "src/main.py",
                ".egg-state/contracts/123.json",
                ".egg-state/drafts/plan.md",
                "README.md",
            ],
        )

        assert result.allowed is True

    def test_mixed_files_partial_block(self):
        """When some files are blocked, result indicates blocked files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "implement",
            [
                "src/main.py",  # allowed
                ".egg-state/contracts/123.json",  # blocked
                "tests/test_main.py",  # allowed
            ],
        )

        assert result.allowed is False
        assert ".egg-state/contracts/123.json" in result.blocked_files
        assert "src/main.py" not in result.blocked_files

    def test_unknown_phase_blocks_by_default(self):
        """Unknown phase strings block files by default (fail-closed security)."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "unknown_phase",
            ["src/main.py"],
        )

        # Unknown phases block by default for security (fail-closed)
        assert result.allowed is False
        assert "unknown" in result.message.lower()
        assert "src/main.py" in result.blocked_files

    def test_empty_files_list_allowed(self):
        """Empty files list should be allowed."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "implement",
            [],
        )

        assert result.allowed is True

    def test_path_escape_blocked(self):
        """Paths that escape the repository should be blocked."""
        from phase_filter import check_phase_file_restrictions

        # Path traversal attempt
        result = check_phase_file_restrictions(
            "implement",
            [".egg-state/contracts/../../../etc/passwd"],
        )
        assert result.allowed is False
        assert "escapes repository" in result.message.lower() or result.blocked_files

        # Direct parent reference
        result = check_phase_file_restrictions(
            "implement",
            ["../outside_repo.txt"],
        )
        assert result.allowed is False

        # Absolute path
        result = check_phase_file_restrictions(
            "implement",
            ["/etc/passwd"],
        )
        assert result.allowed is False

    def test_phasefilerestriction_from_dict(self):
        """PhaseFileRestriction.from_dict should parse correctly."""
        from phase_filter import PhaseFileRestriction

        data = {
            "allowed_patterns": [".egg-state/contracts/*", ".egg-state/checkpoints/*"],
            "blocked_patterns": [".egg-state/pipelines/*"],
            "description": "Test restriction",
        }

        restriction = PhaseFileRestriction.from_dict(data)

        assert restriction.allowed_patterns == [
            ".egg-state/contracts/*",
            ".egg-state/checkpoints/*",
        ]
        assert restriction.blocked_patterns == [".egg-state/pipelines/*"]
        assert restriction.description == "Test restriction"

    def test_phasefilerestriction_is_file_allowed_blocked_priority(self):
        """Blocked patterns take priority over allowed patterns."""
        from phase_filter import PhaseFileRestriction

        restriction = PhaseFileRestriction(
            allowed_patterns=[".egg-state/*"],
            blocked_patterns=[".egg-state/secrets/*"],
        )

        allowed, _ = restriction.is_file_allowed(".egg-state/contracts/123.json")
        assert allowed is True

        blocked, _ = restriction.is_file_allowed(".egg-state/secrets/token.txt")
        assert blocked is False

    def test_phasefilerestriction_wildcard_allow_all(self):
        """Special '*' pattern allows all files."""
        from phase_filter import PhaseFileRestriction

        restriction = PhaseFileRestriction(
            allowed_patterns=["*"],
        )

        allowed, _ = restriction.is_file_allowed("anything/goes/here.py")
        assert allowed is True

    def test_path_traversal_normalized_and_blocked(self):
        """Path traversal attempts are normalized and correctly blocked."""
        from phase_filter import check_phase_file_restrictions

        # Path traversal attempt: foo/../.egg-state/contracts/123.json
        # Should normalize to .egg-state/contracts/123.json and be blocked
        result = check_phase_file_restrictions(
            "implement",
            ["foo/../.egg-state/contracts/123.json"],
        )

        assert result.allowed is False
        assert any(".egg-state/contracts" in f for f in result.blocked_files)

    def test_path_traversal_multiple_levels_blocked(self):
        """Multiple levels of path traversal are normalized and blocked."""
        from phase_filter import check_phase_file_restrictions

        # Multiple traversal: a/b/c/../../../.egg-state/drafts/plan.md
        result = check_phase_file_restrictions(
            "implement",
            ["a/b/c/../../../.egg-state/drafts/plan.md"],
        )

        assert result.allowed is False

    def test_path_traversal_mixed_with_valid_files(self):
        """Path traversal blocked even when mixed with valid files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "implement",
            [
                "src/main.py",  # Valid
                "foo/../.egg-state/pipelines/state.json",  # Traversal to blocked path
            ],
        )

        assert result.allowed is False
        assert len(result.blocked_files) == 1

    def test_plan_phase_allows_agent_outputs(self):
        """Plan phase should allow .egg-state/agent-outputs/ files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "plan",
            [".egg-state/agent-outputs/task-planner-output.json"],
        )

        assert result.allowed is True

    def test_plan_phase_allows_reviews(self):
        """Plan phase should allow .egg-state/reviews/ files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "plan",
            [".egg-state/reviews/architect-review.md"],
        )

        assert result.allowed is True

    def test_refine_phase_allows_agent_outputs(self):
        """Refine phase should allow .egg-state/agent-outputs/ files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "refine",
            [".egg-state/agent-outputs/risk-analyst-output.json"],
        )

        assert result.allowed is True

    def test_refine_phase_allows_reviews(self):
        """Refine phase should allow .egg-state/reviews/ files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "refine",
            [".egg-state/reviews/risk-review.md"],
        )

        assert result.allowed is True
