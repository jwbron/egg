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
    """Tests for PhaseFilter file restriction methods.

    Per #1903, per-role file restrictions are derived from
    ``shared/egg_restrictions/patterns.py``; the JSON
    ``file_restrictions`` key is ignored at load time. These tests
    exercise the derived restrictions using fine-grained roles
    (``coder``/``tester``/``documenter``) that replaced the legacy
    coarse ``implementer`` role.
    """

    @pytest.fixture
    def permissions_file_phases_only(self) -> Path:
        """Create a temporary permissions file with phases only.

        The ``file_restrictions`` key is intentionally absent — derived
        restrictions come from ``AGENT_PATTERNS`` regardless.
        """
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
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(permissions, f)
            return Path(f.name)

    def test_derived_restrictions_include_coder(self, permissions_file_phases_only: Path):
        """Derived restrictions should include the canonical agent roles."""
        pf = PhaseFilter(permissions_path=permissions_file_phases_only)
        restrictions = pf.get_file_restrictions()

        # AGENT_PATTERNS is the single source of truth — every role with
        # blocked_patterns shows up here.
        roles = {r.role for r in restrictions}
        assert "coder" in roles
        assert "tester" in roles
        assert "documenter" in roles

        coder = next(r for r in restrictions if r.role == "coder")
        assert ".egg-state/" in coder.blocked_patterns
        assert ".egg-state/agent-outputs/" in coder.block_exempt_patterns

    def test_legacy_file_restrictions_key_emits_deprecation(self, tmp_path: Path):
        """A stale config carrying ``file_restrictions`` warns but does not
        change derived behavior (#1903)."""
        permissions = {
            "schemaVersion": "1.0",
            "phases": {},
            "file_restrictions": [
                {"role": "implementer", "blocked_patterns": [".egg-state/contracts/"]},
            ],
        }
        path = tmp_path / "p.json"
        path.write_text(json.dumps(permissions))

        pf = PhaseFilter(permissions_path=path)
        with pytest.warns(DeprecationWarning, match="file_restrictions"):
            pf.get_file_restrictions()

    def test_get_file_restrictions_for_role(self, permissions_file_phases_only: Path):
        """Should return restrictions for specific role."""
        pf = PhaseFilter(permissions_path=permissions_file_phases_only)

        coder_restrictions = pf.get_file_restrictions_for_role("coder")
        assert len(coder_restrictions) == 1

        # Unknown roles have no restrictions (fail-open; the patterns.py
        # layer at gateway.py:1648 fails closed for unknown roles).
        unknown_restrictions = pf.get_file_restrictions_for_role("not_a_role")
        assert len(unknown_restrictions) == 0

    def test_get_file_restrictions_for_role_case_insensitive(
        self, permissions_file_phases_only: Path
    ):
        """Role matching should be case-insensitive."""
        pf = PhaseFilter(permissions_path=permissions_file_phases_only)

        restrictions = pf.get_file_restrictions_for_role("CODER")
        assert len(restrictions) == 1

        restrictions = pf.get_file_restrictions_for_role("Coder")
        assert len(restrictions) == 1

    def test_check_file_restrictions_blocked(self, permissions_file_phases_only: Path):
        """Should block files matching restriction patterns."""
        pf = PhaseFilter(permissions_path=permissions_file_phases_only)

        result = pf.check_file_restrictions(
            "coder",
            ["src/main.py", ".egg-state/contracts/123.json", "gateway/app.py"],
        )

        assert result.allowed is False
        assert ".egg-state/contracts/123.json" in result.blocked_files
        assert len(result.blocked_files) == 1
        assert result.role == "coder"

    def test_check_file_restrictions_allowed(self, permissions_file_phases_only: Path):
        """Should allow files not matching restriction patterns."""
        pf = PhaseFilter(permissions_path=permissions_file_phases_only)

        result = pf.check_file_restrictions(
            "coder",
            ["src/main.py", "gateway/app.py", ".egg-state/agent-outputs/out.json"],
        )

        assert result.allowed is True
        assert result.blocked_files == []

    def test_check_file_restrictions_multiple_blocked(self, permissions_file_phases_only: Path):
        """Should detect multiple blocked files."""
        pf = PhaseFilter(permissions_path=permissions_file_phases_only)

        result = pf.check_file_restrictions(
            "coder",
            [".egg-state/contracts/123.json", ".egg-state/drafts/plan.md", "src/code.py"],
        )

        assert result.allowed is False
        assert len(result.blocked_files) == 2
        assert ".egg-state/contracts/123.json" in result.blocked_files
        assert ".egg-state/drafts/plan.md" in result.blocked_files

    def test_check_file_restrictions_no_restrictions_for_role(
        self, permissions_file_phases_only: Path
    ):
        """Unknown roles have no restrictions at this layer (fail-open)."""
        pf = PhaseFilter(permissions_path=permissions_file_phases_only)

        result = pf.check_file_restrictions(
            "unknown_role",
            [".egg-state/contracts/123.json"],
        )

        assert result.allowed is True

    def test_check_file_restrictions_empty_role(self, permissions_file_phases_only: Path):
        """Empty role should be allowed."""
        pf = PhaseFilter(permissions_path=permissions_file_phases_only)

        result = pf.check_file_restrictions(
            "",
            [".egg-state/contracts/123.json"],
        )

        assert result.allowed is True

    def test_check_file_restrictions_empty_files(self, permissions_file_phases_only: Path):
        """Empty file list should be allowed."""
        pf = PhaseFilter(permissions_path=permissions_file_phases_only)

        result = pf.check_file_restrictions("coder", [])

        assert result.allowed is True


class TestDefaultFileRestrictions:
    """Tests for default file restrictions (when no config file)."""

    @pytest.fixture(autouse=True)
    def reset_filter(self):
        """Reset the global filter before each test."""
        phase_filter._filter = None
        yield
        phase_filter._filter = None

    def test_default_restrictions_block_coder_contracts(self):
        """Derived restrictions should block coder from modifying contracts.

        Pre-#1903 a coarse "implementer" role guarded contracts. After
        #1903 the fine-grained role (coder) blocks the broader
        ``.egg-state/`` prefix, which still covers contracts.
        """
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))

        result = pf.check_file_restrictions(
            "coder",
            [".egg-state/contracts/123.json"],
        )

        assert result.allowed is False
        assert ".egg-state/contracts/123.json" in result.blocked_files

    def test_default_restrictions_allow_reviewer_contract_role(self):
        """Reviewer-contract role can write into ``.egg-state/contracts/``."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))

        result = pf.check_file_restrictions(
            "reviewer_contract",
            [".egg-state/contracts/123.json"],
        )

        assert result.allowed is True

    def test_defaults_applied_when_config_lacks_file_restrictions_key(self):
        """Derived restrictions apply regardless of whether the JSON config
        had a (now-ignored) ``file_restrictions`` key (#1903)."""
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
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(permissions, f)
            temp_path = Path(f.name)

        try:
            pf = PhaseFilter(permissions_path=temp_path)

            result = pf.check_file_restrictions(
                "coder",
                [".egg-state/contracts/123.json"],
            )

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

    def test_convenience_function_blocks_coder_from_contracts(self):
        """Module-level function should block coder from contracts."""
        result = check_file_restrictions(
            "coder",
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
        """Module-level function should allow normal files for coder."""
        result = check_file_restrictions(
            "coder",
            ["src/main.py", "gateway/app.py"],
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

    def test_plan_phase_mixed_allowed_and_disallowed_files(self):
        """Plan phase blocks push when allowed files are mixed with disallowed files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "plan",
            [
                ".egg-state/agent-outputs/task-planner-output.json",
                ".egg-state/contracts/plan.json",
                "src/main.py",
            ],
        )

        assert result.allowed is False
        assert "src/main.py" in result.blocked_files
        # Allowed files should NOT appear in blocked_files
        assert ".egg-state/agent-outputs/task-planner-output.json" not in result.blocked_files
        assert ".egg-state/contracts/plan.json" not in result.blocked_files

    def test_plan_phase_allows_multiple_state_files(self):
        """Plan phase allows push with multiple allowed .egg-state/ files."""
        from phase_filter import check_phase_file_restrictions

        result = check_phase_file_restrictions(
            "plan",
            [
                ".egg-state/agent-outputs/task-planner-output.json",
                ".egg-state/contracts/plan.json",
                ".egg-state/reviews/architect-review.md",
                ".egg-state/checkpoints/checkpoint-1.json",
            ],
        )

        assert result.allowed is True


class TestIssueCommentBlocking:
    """Tests for issue comment/edit blocking across phases.

    Validates that issue comment and issue edit operations are blocked
    in refine, plan, and implement phases (defense layer 1).
    See: https://github.com/jwbron/egg/issues/1153
    """

    @pytest.fixture(autouse=True)
    def reset_filter(self):
        """Reset the global filter before each test."""
        phase_filter._filter = None
        yield
        phase_filter._filter = None

    def test_issue_comment_blocked_in_refine(self):
        """Issue comment is blocked during refine phase."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(PipelinePhase.REFINE, OperationType.GH, "issue comment 123")
        assert result.allowed is False

    def test_issue_comment_blocked_in_plan(self):
        """Issue comment is blocked during plan phase."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(PipelinePhase.PLAN, OperationType.GH, "issue comment 456")
        assert result.allowed is False

    def test_issue_comment_blocked_in_implement(self):
        """Issue comment is blocked during implement phase."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(PipelinePhase.IMPLEMENT, OperationType.GH, "issue comment 789")
        assert result.allowed is False

    def test_issue_edit_blocked_in_refine(self):
        """Issue edit is blocked during refine phase."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(PipelinePhase.REFINE, OperationType.GH, "issue edit 123")
        assert result.allowed is False

    def test_issue_edit_blocked_in_plan(self):
        """Issue edit is blocked during plan phase."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(PipelinePhase.PLAN, OperationType.GH, "issue edit 456")
        assert result.allowed is False

    def test_issue_edit_blocked_in_implement(self):
        """Issue edit is blocked during implement phase."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(PipelinePhase.IMPLEMENT, OperationType.GH, "issue edit 789")
        assert result.allowed is False

    def test_issue_comment_allowed_in_pr_phase(self):
        """Issue comment is not blocked in PR phase (no restriction)."""
        pf = PhaseFilter(permissions_path=Path("/nonexistent"))
        result = pf.filter_operation(PipelinePhase.PR, OperationType.GH, "issue comment 123")
        assert result.allowed is True

    def test_issue_comment_blocked_via_convenience_function(self):
        """is_operation_blocked correctly reports issue comment as blocked."""
        assert is_operation_blocked("refine", "gh", "issue comment 123") is True
        assert is_operation_blocked("plan", "gh", "issue comment 456") is True
        assert is_operation_blocked("implement", "gh", "issue comment 789") is True
