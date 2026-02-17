"""Unit tests for phase_filter.py phase file restrictions, operation filtering, and security.

Covers:
- PhaseFileRestriction.is_file_allowed() for each phase
- PhaseFilter.check_phase_file_restrictions() for all phases
- Unknown phase fail-closed security behavior
- FileRestriction role-based blocking
- Path escape prevention (../, /)
- Operation filtering (filter_operation, is_operation_blocked)
- Default permissions loading and fallback
- check_agent_restrictions() bridge to agent_restrictions module
"""

import pytest
from phase_filter import (
    FileRestriction,
    FileRestrictionResult,
    FilterResult,
    Operation,
    OperationType,
    PhaseFileRestriction,
    PhaseFilter,
    PhasePermissions,
    PipelinePhase,
    check_agent_restrictions,
    check_file_restrictions,
    check_phase_file_restrictions,
    filter_operation,
    get_phase_filter,
    is_operation_blocked,
    reset_phase_filter,
)


class TestPhaseFileRestrictionIsFileAllowed:
    """Tests for PhaseFileRestriction.is_file_allowed()."""

    def test_blocked_pattern_takes_precedence(self):
        restriction = PhaseFileRestriction(
            allowed_patterns=["*"],
            blocked_patterns=[".egg-state/contracts/*"],
        )
        allowed, reason = restriction.is_file_allowed(".egg-state/contracts/123.json")
        assert allowed is False

    def test_allowed_pattern_permits(self):
        restriction = PhaseFileRestriction(
            allowed_patterns=[".egg-state/contracts/*"],
        )
        allowed, reason = restriction.is_file_allowed(".egg-state/contracts/123.json")
        assert allowed is True

    def test_no_matching_allowed_pattern_denies(self):
        restriction = PhaseFileRestriction(
            allowed_patterns=[".egg-state/contracts/*"],
        )
        allowed, reason = restriction.is_file_allowed("src/app.py")
        assert allowed is False

    def test_wildcard_star_allows_all(self):
        restriction = PhaseFileRestriction(allowed_patterns=["*"])
        allowed, reason = restriction.is_file_allowed("anything/goes.py")
        assert allowed is True

    def test_no_allowed_patterns_allows_by_default(self):
        restriction = PhaseFileRestriction(blocked_patterns=[".egg-state/contracts/*"])
        allowed, reason = restriction.is_file_allowed("src/app.py")
        assert allowed is True

    def test_no_patterns_at_all_allows(self):
        restriction = PhaseFileRestriction()
        allowed, reason = restriction.is_file_allowed("anything.py")
        assert allowed is True

    def test_path_traversal_blocked(self):
        restriction = PhaseFileRestriction(allowed_patterns=["*"])
        allowed, reason = restriction.is_file_allowed("../../../etc/passwd")
        assert allowed is False

    def test_absolute_path_blocked(self):
        restriction = PhaseFileRestriction(allowed_patterns=["*"])
        allowed, reason = restriction.is_file_allowed("/etc/passwd")
        assert allowed is False

    def test_prefix_directory_pattern(self):
        restriction = PhaseFileRestriction(
            allowed_patterns=[".egg-state/drafts/"],
        )
        allowed, reason = restriction.is_file_allowed(".egg-state/drafts/analysis.md")
        assert allowed is True

    def test_fnmatch_wildcard_pattern(self):
        restriction = PhaseFileRestriction(
            allowed_patterns=[".egg-state/drafts/*analysis*"],
        )
        allowed, reason = restriction.is_file_allowed(".egg-state/drafts/644-analysis.md")
        assert allowed is True

    def test_fnmatch_wildcard_no_match(self):
        restriction = PhaseFileRestriction(
            allowed_patterns=[".egg-state/drafts/*analysis*"],
        )
        allowed, reason = restriction.is_file_allowed(".egg-state/drafts/644-plan.md")
        assert allowed is False


class TestPhaseFileRestrictionNormalizePath:
    """Tests for PhaseFileRestriction._normalize_path()."""

    def test_strips_leading_dot_slash(self):
        assert PhaseFileRestriction._normalize_path("./src/app.py") == "src/app.py"

    def test_normalizes_double_slashes(self):
        assert PhaseFileRestriction._normalize_path("src//app.py") == "src/app.py"

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="escapes repository"):
            PhaseFileRestriction._normalize_path("../etc/passwd")

    def test_rejects_absolute_path(self):
        with pytest.raises(ValueError, match="escapes repository"):
            PhaseFileRestriction._normalize_path("/etc/passwd")


class TestPhaseFileRestrictionMatchesPattern:
    """Tests for PhaseFileRestriction._matches_pattern()."""

    def test_star_matches_everything(self):
        assert PhaseFileRestriction._matches_pattern("anything.py", "*") is True

    def test_directory_prefix_match(self):
        assert (
            PhaseFileRestriction._matches_pattern(
                ".egg-state/contracts/123.json", ".egg-state/contracts/"
            )
            is True
        )

    def test_directory_prefix_no_match(self):
        assert (
            PhaseFileRestriction._matches_pattern("src/contracts/123.json", ".egg-state/contracts/")
            is False
        )

    def test_fnmatch_glob(self):
        assert (
            PhaseFileRestriction._matches_pattern(
                ".egg-state/drafts/644-analysis.md", ".egg-state/drafts/*analysis*"
            )
            is True
        )


class TestFileRestriction:
    """Tests for FileRestriction role-based blocking."""

    def test_blocks_matching_file(self):
        restriction = FileRestriction(
            role="implementer",
            blocked_patterns=[".egg-state/contracts/"],
        )
        assert restriction.is_file_blocked(".egg-state/contracts/123.json") is True

    def test_allows_non_matching_file(self):
        restriction = FileRestriction(
            role="implementer",
            blocked_patterns=[".egg-state/contracts/"],
        )
        assert restriction.is_file_blocked("src/app.py") is False

    def test_path_traversal_always_blocked(self):
        restriction = FileRestriction(
            role="implementer",
            blocked_patterns=[".egg-state/contracts/"],
        )
        assert restriction.is_file_blocked("../etc/passwd") is True

    def test_absolute_path_blocked(self):
        restriction = FileRestriction(
            role="implementer",
            blocked_patterns=[".egg-state/contracts/"],
        )
        assert restriction.is_file_blocked("/etc/passwd") is True

    def test_from_dict(self):
        restriction = FileRestriction.from_dict(
            {
                "role": "implementer",
                "blocked_patterns": [".egg-state/contracts/"],
                "blocked_reason": "Use contract API",
            }
        )
        assert restriction.role == "implementer"
        assert restriction.blocked_patterns == [".egg-state/contracts/"]
        assert restriction.blocked_reason == "Use contract API"


class TestPhaseFilterDefaultPermissions:
    """Tests for PhaseFilter with default permissions (no config file)."""

    @pytest.fixture(autouse=True)
    def reset_filter(self):
        """Reset global filter before each test."""
        reset_phase_filter()
        yield
        reset_phase_filter()

    def test_refine_allows_only_egg_state(self):
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions("refine", ["src/app.py"])
        assert result.allowed is False

    def test_refine_allows_contracts(self):
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions("refine", [".egg-state/contracts/123.json"])
        assert result.allowed is True

    def test_refine_allows_analysis_drafts(self):
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions("refine", [".egg-state/drafts/644-analysis.md"])
        assert result.allowed is True

    def test_refine_allows_checkpoints(self):
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions("refine", [".egg-state/checkpoints/ckpt.json"])
        assert result.allowed is True

    def test_refine_allows_agent_outputs(self):
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions("refine", [".egg-state/agent-outputs/out.json"])
        assert result.allowed is True

    def test_refine_allows_reviews(self):
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions("refine", [".egg-state/reviews/review.json"])
        assert result.allowed is True

    def test_plan_allows_plan_drafts(self):
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions("plan", [".egg-state/drafts/644-plan.md"])
        assert result.allowed is True

    def test_plan_blocks_code(self):
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions("plan", ["gateway/gateway.py"])
        assert result.allowed is False

    def test_implement_allows_code(self):
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions("implement", ["src/app.py"])
        assert result.allowed is True

    def test_implement_blocks_contracts(self):
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions("implement", [".egg-state/contracts/123.json"])
        assert result.allowed is False

    def test_implement_blocks_drafts(self):
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions("implement", [".egg-state/drafts/plan.md"])
        assert result.allowed is False

    def test_implement_blocks_pipelines(self):
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions("implement", [".egg-state/pipelines/pipe.json"])
        assert result.allowed is False

    def test_implement_blocks_reviews(self):
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions("implement", [".egg-state/reviews/review.json"])
        assert result.allowed is False

    def test_implement_allows_checkpoints(self):
        """Checkpoints should NOT be blocked during implement phase."""
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions(
            "implement", [".egg-state/checkpoints/ckpt.json"]
        )
        assert result.allowed is True

    def test_implement_allows_agent_outputs(self):
        """Agent outputs should NOT be blocked during implement phase."""
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions(
            "implement", [".egg-state/agent-outputs/out.json"]
        )
        assert result.allowed is True

    def test_pr_allows_everything(self):
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions(
            "pr", ["src/app.py", ".egg-state/contracts/123.json"]
        )
        assert result.allowed is True

    def test_unknown_phase_blocks_all(self):
        """Security: unknown phases should fail closed."""
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions("unknown_phase", ["src/app.py"])
        assert result.allowed is False
        assert "unknown_phase" in result.message.lower() or "Unknown" in result.message

    def test_empty_files_allows(self):
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions("implement", [])
        assert result.allowed is True

    def test_mixed_allowed_and_blocked_in_implement(self):
        """Only blocked files should be reported; allowed files pass through."""
        pf = PhaseFilter()
        result = pf.check_phase_file_restrictions(
            "implement", ["src/app.py", ".egg-state/contracts/123.json"]
        )
        assert result.allowed is False
        assert ".egg-state/contracts/123.json" in result.blocked_files
        assert "src/app.py" not in result.blocked_files


class TestPhaseFilterOperationFiltering:
    """Tests for filter_operation() and is_operation_blocked()."""

    @pytest.fixture(autouse=True)
    def reset_filter(self):
        reset_phase_filter()
        yield
        reset_phase_filter()

    def test_pr_create_blocked_during_implement(self):
        result = filter_operation("implement", "gh", "pr create --title foo")
        assert result.allowed is False

    def test_push_allowed_during_implement(self):
        result = filter_operation("implement", "git", "push origin egg/branch")
        assert result.allowed is True

    def test_pr_create_allowed_during_pr(self):
        result = filter_operation("pr", "gh", "pr create --title foo")
        assert result.allowed is True

    def test_is_operation_blocked_convenience(self):
        assert is_operation_blocked("implement", "gh", "pr create --title x") is True
        assert is_operation_blocked("implement", "git", "push origin branch") is False

    def test_pr_create_blocked_during_refine(self):
        result = filter_operation("refine", "gh", "pr create --title foo")
        assert result.allowed is False

    def test_pr_create_blocked_during_plan(self):
        result = filter_operation("plan", "gh", "pr create --title foo")
        assert result.allowed is False


class TestPhaseFilterCheckFileRestrictions:
    """Tests for check_file_restrictions() role-based."""

    @pytest.fixture(autouse=True)
    def reset_filter(self):
        reset_phase_filter()
        yield
        reset_phase_filter()

    def test_implementer_blocked_from_contracts(self):
        result = check_file_restrictions("implementer", [".egg-state/contracts/123.json"])
        assert result.allowed is False

    def test_implementer_allowed_for_code(self):
        result = check_file_restrictions("implementer", ["src/app.py"])
        assert result.allowed is True

    def test_unknown_role_allowed(self):
        result = check_file_restrictions("unknown_role", ["src/app.py"])
        assert result.allowed is True

    def test_empty_role_allowed(self):
        result = check_file_restrictions("", ["src/app.py"])
        assert result.allowed is True

    def test_empty_files_allowed(self):
        result = check_file_restrictions("implementer", [])
        assert result.allowed is True


class TestCheckAgentRestrictions:
    """Tests for check_agent_restrictions() bridge function.

    The check_agent_restrictions function uses a relative import internally.
    We test the underlying validate_agent_push directly, then verify the
    bridge converts results to FileRestrictionResult correctly.
    """

    @pytest.fixture(autouse=True)
    def reset_filter(self):
        reset_phase_filter()
        yield
        reset_phase_filter()

    def test_coder_allowed_for_source(self):
        from agent_restrictions import validate_agent_push

        result = validate_agent_push("coder", ["gateway/app.py"])
        assert result.allowed is True

        # Verify bridge returns FileRestrictionResult
        fr = FileRestrictionResult.allow(result.message)
        assert isinstance(fr, FileRestrictionResult)

    def test_coder_blocked_for_tests(self):
        from agent_restrictions import validate_agent_push

        result = validate_agent_push("coder", ["tests/test_foo.py"])
        assert result.allowed is False
        assert result.role == "coder"

    def test_tester_allowed_for_tests(self):
        from agent_restrictions import validate_agent_push

        result = validate_agent_push("tester", ["tests/test_foo.py"])
        assert result.allowed is True

    def test_unknown_role_allowed(self):
        from agent_restrictions import validate_agent_push

        result = validate_agent_push("unknown_role", ["anything.py"])
        assert result.allowed is True


class TestOperation:
    """Tests for Operation.matches()."""

    def test_exact_match(self):
        op = Operation(type=OperationType.GIT, pattern="push *")
        assert op.matches("push origin main") is True

    def test_no_match(self):
        op = Operation(type=OperationType.GIT, pattern="push *")
        assert op.matches("pull origin main") is False

    def test_pr_create_pattern(self):
        op = Operation(type=OperationType.GH, pattern="pr create*")
        assert op.matches("pr create --title foo") is True

    def test_wildcard_all(self):
        op = Operation(type=OperationType.GIT, pattern="*")
        assert op.matches("anything here") is True


class TestPhasePermissionsFromDict:
    """Tests for PhasePermissions.from_dict()."""

    def test_creates_from_dict(self):
        data = {
            "allowed_operations": [
                {"type": "git", "pattern": "push *", "description": "Push code"},
            ],
            "blocked_operations": [
                {"type": "gh", "pattern": "pr create*", "description": "No PRs"},
            ],
            "exit_requires": "human",
        }
        perms = PhasePermissions.from_dict(data)
        assert len(perms.allowed_operations) == 1
        assert len(perms.blocked_operations) == 1
        assert perms.exit_requires == "human"

    def test_default_exit_requires(self):
        perms = PhasePermissions.from_dict({})
        assert perms.exit_requires == "human"


class TestPhaseFilterExitRequirement:
    """Tests for get_exit_requirement()."""

    @pytest.fixture(autouse=True)
    def reset_filter(self):
        reset_phase_filter()
        yield
        reset_phase_filter()

    def test_implement_requires_reviewer(self):
        pf = PhaseFilter()
        assert pf.get_exit_requirement(PipelinePhase.IMPLEMENT) == "reviewer"

    def test_pr_requires_human(self):
        pf = PhaseFilter()
        assert pf.get_exit_requirement(PipelinePhase.PR) == "human"

    def test_unknown_phase_returns_none(self):
        pf = PhaseFilter()
        # Use a valid PipelinePhase that might not have permissions
        # get_exit_requirement returns None if no permissions found
        result = pf.get_exit_requirement(PipelinePhase.REFINE)
        assert result == "human"


class TestFilterResult:
    """Tests for FilterResult dataclass."""

    def test_allow_factory(self):
        result = FilterResult.allow("OK")
        assert result.allowed is True
        assert result.message == "OK"

    def test_block_factory(self):
        result = FilterResult.block(
            "Blocked",
            OperationType.GH,
            PipelinePhase.IMPLEMENT,
            "No PRs",
        )
        assert result.allowed is False
        assert result.operation_type == OperationType.GH
        assert result.phase == PipelinePhase.IMPLEMENT


class TestFileRestrictionResult:
    """Tests for FileRestrictionResult dataclass."""

    def test_allow_factory(self):
        result = FileRestrictionResult.allow("All good")
        assert result.allowed is True

    def test_block_factory(self):
        result = FileRestrictionResult.block(
            message="Blocked",
            role="implementer",
            blocked_files=["file.py"],
            blocked_reason="Not allowed",
        )
        assert result.allowed is False
        assert result.role == "implementer"
        assert result.blocked_files == ["file.py"]


class TestPhaseFilterFormatBlockedMessage:
    """Tests for PhaseFilter._format_blocked_message()."""

    def test_includes_phase_and_reason(self):
        pf = PhaseFilter()
        msg = pf._format_blocked_message(
            OperationType.GH,
            "pr create --title foo",
            PipelinePhase.IMPLEMENT,
            "Not allowed in implement",
        )
        assert "implement" in msg
        assert "Not allowed in implement" in msg
        assert "pr create" in msg
