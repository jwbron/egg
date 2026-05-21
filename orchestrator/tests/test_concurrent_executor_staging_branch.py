"""Tests for per-role CUSTOM+PR staging branch derivation.

Verifies ``ConcurrentPhaseExecutor.get_worktree_branch()`` produces the
expected namespaced branch names for CUSTOM-mode pipelines targeting a
PR (``egg/custom-pr/{pr}/{short-sha}/{role}``) and falls back sensibly
when required fields are missing, while leaving the issue-mode path
unaffected.
"""

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

# sys.path setup — orchestrator + shared.  ``conftest.py`` already does
# this for pytest runs, but we repeat it here so the module is also
# directly importable (matching the canonical pattern from other
# orchestrator tests).
_project_root = Path(__file__).parent.parent.parent
_orchestrator_path = _project_root / "orchestrator"
_shared_path = _project_root / "shared"
for _p in (_orchestrator_path, _shared_path):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Docker module mock — conftest installs one if docker is missing, but
# we guard again in case this file is imported outside pytest.
if "docker" not in sys.modules:
    _errors_mod = types.ModuleType("docker.errors")
    _errors_mod.DockerException = type("DockerException", (Exception,), {})
    _errors_mod.APIError = type("APIError", (Exception,), {})
    _errors_mod.NotFound = type("NotFound", (Exception,), {})
    _errors_mod.ImageNotFound = type("ImageNotFound", (Exception,), {})
    _docker_mod = MagicMock()
    _docker_mod.errors = _errors_mod
    sys.modules.setdefault("docker", _docker_mod)
    sys.modules.setdefault("docker.errors", _errors_mod)
    sys.modules.setdefault("docker.types", MagicMock())

from concurrent_executor import AgentRole
from concurrent_executor import ConcurrentPhaseExecutor as ConcurrentExecutor
from models import Pipeline, PipelineMode, PipelinePhase, PipelineStatus


def _make_executor(pipeline: Pipeline) -> ConcurrentExecutor:
    """Construct an executor without running ``__init__``.

    The real ``__init__`` needs a spawn_fn and touches review-graph and
    threading primitives that this test does not exercise, so we
    bypass it and assign only the attributes ``get_worktree_branch``
    actually reads.
    """
    executor = ConcurrentExecutor.__new__(ConcurrentExecutor)
    executor.pipeline = pipeline
    executor._roles_override = None
    return executor


def _custom_pr_pipeline(
    *,
    pr_number: int | None,
    pr_head_sha: str | None,
    branch: str | None = None,
    issue_number: int | None = None,
    pipeline_id: str = "custom-pr-test",
) -> Pipeline:
    return Pipeline(
        id=pipeline_id,
        repo="test/repo",
        issue_number=issue_number,
        branch=branch,
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        mode=PipelineMode.CUSTOM,
        pr_number=pr_number,
        pr_head_sha=pr_head_sha,
    )


def _issue_pipeline(
    *,
    branch: str | None = None,
    issue_number: int | None = None,
    pipeline_id: str = "issue-test",
    pr_number: int | None = None,
    pr_head_sha: str | None = None,
) -> Pipeline:
    return Pipeline(
        id=pipeline_id,
        repo="test/repo",
        issue_number=issue_number,
        branch=branch,
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        mode=PipelineMode.ISSUE,
        pr_number=pr_number,
        pr_head_sha=pr_head_sha,
    )


class TestCustomPrStagingBranchHappyPath:
    """Normal CUSTOM+PR path: pr_number + 7+ char SHA produces namespaced branch."""

    def test_coder_gets_namespaced_staging_branch(self):
        pipeline = _custom_pr_pipeline(
            pr_number=42,
            pr_head_sha="abc1234deadbeef5678901234567890abcdefabc",
            branch="feature-x",
        )
        executor = _make_executor(pipeline)

        assert executor.get_worktree_branch(AgentRole.CODER) == "egg/custom-pr/42/abc1234/coder"

    def test_tester_gets_namespaced_staging_branch(self):
        pipeline = _custom_pr_pipeline(
            pr_number=42,
            pr_head_sha="abc1234deadbeef5678901234567890abcdefabc",
            branch="feature-x",
        )
        executor = _make_executor(pipeline)

        assert executor.get_worktree_branch(AgentRole.TESTER) == "egg/custom-pr/42/abc1234/tester"

    def test_documenter_gets_namespaced_staging_branch(self):
        pipeline = _custom_pr_pipeline(
            pr_number=42,
            pr_head_sha="abc1234deadbeef5678901234567890abcdefabc",
            branch="feature-x",
        )
        executor = _make_executor(pipeline)

        assert (
            executor.get_worktree_branch(AgentRole.DOCUMENTER)
            == "egg/custom-pr/42/abc1234/documenter"
        )

    def test_different_pr_and_sha_yields_expected_branch(self):
        pipeline = _custom_pr_pipeline(
            pr_number=7,
            pr_head_sha="def5678cafebabe",
            branch="feature-y",
        )
        executor = _make_executor(pipeline)

        assert executor.get_worktree_branch(AgentRole.CODER) == "egg/custom-pr/7/def5678/coder"


class TestCustomPrStagingBranchHasEggPrefix:
    """All CUSTOM+PR branches must start with 'egg/' so the gateway accepts pushes."""

    def test_all_roles_produce_egg_prefixed_branch(self):
        pipeline = _custom_pr_pipeline(
            pr_number=101,
            pr_head_sha="1234567abcdef890",
            branch="feature-z",
        )
        executor = _make_executor(pipeline)

        for role in (AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER):
            branch = executor.get_worktree_branch(role)
            assert branch.startswith("egg/"), f"role={role.value} branch={branch!r}"
            assert branch.startswith("egg/custom-pr/"), f"role={role.value} branch={branch!r}"


class TestCustomPrStagingBranchPerSha:
    """Same PR, different head SHA → different branch names (no collisions across revisions)."""

    def test_two_shas_yield_different_branches(self):
        p1 = _custom_pr_pipeline(
            pr_number=42,
            pr_head_sha="abc1234deadbeef5678901234567890abcdefabc",
            branch="feature-x",
            pipeline_id="p1",
        )
        p2 = _custom_pr_pipeline(
            pr_number=42,
            pr_head_sha="9999999aaaaaaaabbbbbbbbbcccccccdddddddde",
            branch="feature-x",
            pipeline_id="p2",
        )
        e1 = _make_executor(p1)
        e2 = _make_executor(p2)

        b1 = e1.get_worktree_branch(AgentRole.CODER)
        b2 = e2.get_worktree_branch(AgentRole.CODER)

        assert b1 != b2
        assert b1 == "egg/custom-pr/42/abc1234/coder"
        assert b2 == "egg/custom-pr/42/9999999/coder"

    def test_three_shas_yield_three_distinct_branches(self):
        shas = [
            "abc1234deadbeef",
            "9999999aaaaaaaa",
            "0000000bbbbbbbb",
        ]
        branches: set[str] = set()
        for sha in shas:
            pipeline = _custom_pr_pipeline(
                pr_number=42,
                pr_head_sha=sha,
                branch="feature-x",
                pipeline_id=f"p-{sha[:7]}",
            )
            executor = _make_executor(pipeline)
            branches.add(executor.get_worktree_branch(AgentRole.CODER))

        assert len(branches) == 3


class TestCustomPrStagingBranchPerRole:
    """Same pipeline, different roles → different branch names under a shared prefix."""

    def test_three_roles_yield_three_distinct_branches_same_prefix(self):
        pipeline = _custom_pr_pipeline(
            pr_number=42,
            pr_head_sha="abc1234deadbeef5678901234567890abcdefabc",
            branch="feature-x",
        )
        executor = _make_executor(pipeline)

        roles = [AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER]
        results = [executor.get_worktree_branch(r) for r in roles]

        assert len(set(results)) == 3
        shared_prefix = "egg/custom-pr/42/abc1234/"
        for branch in results:
            assert branch.startswith(shared_prefix)


class TestCustomPrFallbackToPrHeadBranch:
    """When SHA is missing/short or pr_number is missing, fall back to pipeline.branch."""

    def test_none_sha_falls_back_to_pr_head_branch(self):
        pipeline = _custom_pr_pipeline(
            pr_number=42,
            pr_head_sha=None,
            branch="feature-x",
        )
        executor = _make_executor(pipeline)

        assert executor.get_worktree_branch(AgentRole.CODER) == "feature-x"

    def test_empty_sha_falls_back_to_pr_head_branch(self):
        pipeline = _custom_pr_pipeline(
            pr_number=42,
            pr_head_sha="",
            branch="feature-x",
        )
        executor = _make_executor(pipeline)

        assert executor.get_worktree_branch(AgentRole.CODER) == "feature-x"

    def test_short_sha_falls_back_to_pr_head_branch(self):
        # Use SimpleNamespace because Pipeline validator rejects non-hex SHAs.
        pipeline = SimpleNamespace(
            id="custom-pr-test",
            repo="test/repo",
            issue_number=None,
            branch="feature-x",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            mode=PipelineMode.CUSTOM,
            pr_number=42,
            pr_head_sha="short",
        )
        executor = _make_executor(pipeline)

        assert executor.get_worktree_branch(AgentRole.CODER) == "feature-x"

    def test_missing_pr_number_falls_through_to_pr_head_branch(self):
        pipeline = _custom_pr_pipeline(
            pr_number=None,
            pr_head_sha="abc1234deadbeef",
            branch="feature-x",
        )
        executor = _make_executor(pipeline)

        assert executor.get_worktree_branch(AgentRole.CODER) == "feature-x"

    def test_all_missing_falls_through_to_issue_naming(self):
        pipeline = _custom_pr_pipeline(
            pr_number=None,
            pr_head_sha=None,
            branch=None,
            issue_number=99,
            pipeline_id="fallback-99",
        )
        executor = _make_executor(pipeline)

        assert executor.get_worktree_branch(AgentRole.CODER) == "egg/issue-99"


class TestIssueModePathUnaffected:
    """Per-role staging logic must not trigger for issue-mode pipelines."""

    def test_issue_mode_with_branch_returns_branch(self):
        pipeline = _issue_pipeline(branch="feature-x")
        executor = _make_executor(pipeline)

        assert executor.get_worktree_branch(AgentRole.CODER) == "feature-x"

    def test_issue_mode_without_branch_uses_issue_number(self):
        pipeline = _issue_pipeline(
            branch=None,
            issue_number=99,
            pipeline_id="issue-99",
        )
        executor = _make_executor(pipeline)

        assert executor.get_worktree_branch(AgentRole.CODER) == "egg/issue-99"

    def test_issue_mode_without_branch_or_issue_uses_id(self):
        pipeline = _issue_pipeline(
            branch=None,
            issue_number=None,
            pipeline_id="custom-id",
        )
        executor = _make_executor(pipeline)

        assert executor.get_worktree_branch(AgentRole.CODER) == "egg/issue-custom-id"

    def test_issue_mode_with_pr_fields_does_not_produce_custom_pr_branch(self):
        pipeline = _issue_pipeline(
            branch="feature-x",
            pr_number=42,
            pr_head_sha="abc1234deadbeef5678901234567890abcdefabc",
        )
        executor = _make_executor(pipeline)

        branch = executor.get_worktree_branch(AgentRole.CODER)
        assert branch == "feature-x"
        assert "custom-pr" not in branch


class TestShortShaTruncation:
    """Short SHA is exactly the first 7 chars of pr_head_sha."""

    def test_forty_char_sha_truncates_to_seven(self):
        pipeline = _custom_pr_pipeline(
            pr_number=42,
            pr_head_sha="abc1234deadbeef5678901234567890abcdefabc",
            branch="feature-x",
        )
        executor = _make_executor(pipeline)

        branch = executor.get_worktree_branch(AgentRole.CODER)
        # Branch format: egg/custom-pr/{pr}/{short-sha}/{role}
        parts = branch.split("/")
        # ["egg", "custom-pr", "42", "abc1234", "coder"]
        assert parts[3] == "abc1234"
        assert len(parts[3]) == 7

    def test_exactly_seven_char_sha_is_used_verbatim(self):
        pipeline = _custom_pr_pipeline(
            pr_number=42,
            pr_head_sha="abc1234",
            branch="feature-x",
        )
        executor = _make_executor(pipeline)

        branch = executor.get_worktree_branch(AgentRole.CODER)
        parts = branch.split("/")
        assert parts[3] == "abc1234"
        assert branch == "egg/custom-pr/42/abc1234/coder"
