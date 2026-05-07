"""
Tests for auto PR creation functions (_build_pr_body, _auto_create_pr).
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from models import Pipeline, PipelinePhase, PipelineStatus
from routes.pipelines import (
    _auto_create_pr,
    _build_pr_body,
    _compute_gateway_mode,
    _detect_default_branch,
    _handle_pr_creation_failure,
)


def _make_pipeline(
    issue_number=42,
    repo="owner/repo",
    branch="egg/issue-42",
):
    """Create a Pipeline for testing."""
    return Pipeline(
        id=f"issue-{issue_number}" if issue_number else "local-test",
        issue_number=issue_number,
        repo=repo,
        branch=branch,
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.PR,
    )


def _make_contract_json(
    issue_number=42,
    issue_title="Fix the auth bug",
    pr_title="Fix authentication bypass in login flow",
    pr_description="Fixes a bypass where unauthenticated users could access protected routes.\n\nCloses #42",
):
    """Create a contract JSON dict for testing."""
    contract = {
        "schemaVersion": "1.0",
        "issue": {
            "number": issue_number,
            "title": issue_title,
            "url": f"https://github.com/owner/repo/issues/{issue_number}",
        },
        "current_phase": "pr",
        "phases": [],
    }
    if pr_title:
        contract["pr"] = {"title": pr_title, "description": pr_description or ""}
    return contract


class TestBuildPrBody:
    """Tests for _build_pr_body."""

    def test_uses_contract_pr_metadata(self, tmp_path):
        """Test that PR title/body come from contract PR metadata."""
        pipeline = _make_pipeline()
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        contract_file = contract_dir / "42.json"
        contract_file.write_text(json.dumps(_make_contract_json()))

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert title == "Fix authentication bypass in login flow"
        assert "Fixes a bypass" in body
        assert "Closes #42" in body

    def test_falls_back_to_issue_title(self, tmp_path):
        """Test fallback to issue title when no PR metadata."""
        pipeline = _make_pipeline()
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        contract_file = contract_dir / "42.json"
        contract_file.write_text(json.dumps(_make_contract_json(pr_title=None)))

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert title == "Fix the auth bug"

    def test_falls_back_to_pipeline_id(self, tmp_path):
        """Test fallback to pipeline ID when no contract exists."""
        pipeline = _make_pipeline()

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert "issue-42" in title

    def test_does_not_include_commit_log(self, tmp_path):
        """Test that commit log is NOT included in body (GitHub shows it natively)."""
        pipeline = _make_pipeline()

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert "## Commits" not in body

    def test_does_not_include_diff_stats(self, tmp_path):
        """Test that diff stats are NOT included in body (GitHub shows it natively)."""
        pipeline = _make_pipeline()

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert "## Changes" not in body

    def test_includes_issue_reference_when_no_description(self, tmp_path):
        """Test that issue reference is added when no PR description."""
        pipeline = _make_pipeline()

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert "Closes #42" in body

    def test_body_ends_with_authored_by(self, tmp_path):
        """Test that body ends with attribution."""
        pipeline = _make_pipeline()

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert "Authored-by: egg" in body

    def test_includes_pipeline_context_section(self, tmp_path):
        """Test that pipeline context section is included in body."""
        pipeline = _make_pipeline()

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert "## Pipeline Context" in body
        assert "Pipeline: `issue-42`" in body
        assert "Issue: #42" in body

    def test_pipeline_context_before_authored_by(self, tmp_path):
        """Test that pipeline context appears before the authored-by line."""
        pipeline = _make_pipeline()

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        context_pos = body.index("## Pipeline Context")
        authored_pos = body.index("Authored-by: egg")
        assert context_pos < authored_pos

    def test_body_stays_well_under_github_limit(self, tmp_path):
        """Test that body without git log/diff stays well under 65536 chars."""
        pipeline = _make_pipeline()
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        # Use a reasonably long PR description
        contract_file = contract_dir / "42.json"
        contract_file.write_text(json.dumps(_make_contract_json(pr_description="A" * 10_000)))

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert len(body) < 65_536


def _write_plan_draft(tmp_path, issue_number, *, title, description, test_plan, manual_steps):
    """Helper: write a plan draft with a ``pr:`` yaml-tasks block.

    Mirrors the layout the planner produces, which the plan parser reads
    via ``parse_plan``.

    Note: YAML is assembled via string concatenation — inputs must be
    simple strings with no quotes, newlines, or special YAML characters.
    """
    drafts_dir = tmp_path / ".egg-state" / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    plan_path = drafts_dir / f"{issue_number}-plan.md"
    plan_path.write_text(
        "# Plan\n\n"
        "```yaml\n"
        "# yaml-tasks\n"
        "pr:\n"
        f'  title: "{title}"\n'
        "  description: |\n"
        f"    {description}\n"
        "  test_plan: |\n"
        f"    {test_plan}\n"
        "  manual_steps: |\n"
        f"    {manual_steps}\n"
        "phases:\n"
        "  - id: 1\n"
        "    name: Phase 1\n"
        "    goal: Do something\n"
        "    tasks: []\n"
        "```\n"
    )
    return plan_path


class TestBuildPrBodyPlanDraftFallback:
    """Tests for the plan-draft fallback in _build_pr_body (#1825 / #1829).

    When ``contract.pr`` is missing (e.g. the plan-phase contract write did
    not reach the branch tip), ``_build_pr_body`` should parse the plan
    draft on disk and use its ``pr:`` block.
    """

    def test_uses_plan_draft_when_contract_has_no_pr(self, tmp_path):
        """Plan draft is used when contract.pr is absent."""
        pipeline = _make_pipeline()
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        (contract_dir / "42.json").write_text(json.dumps(_make_contract_json(pr_title=None)))
        _write_plan_draft(
            tmp_path,
            42,
            title="Fix the auth bug via plan draft",
            description="From the plan draft description.",
            test_plan="- Automated: pytest passes",
            manual_steps="Pre-merge: run migration",
        )

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert title == "Fix the auth bug via plan draft"
        assert "From the plan draft description." in body
        assert "## Test Plan" in body
        assert "pytest passes" in body
        assert "## Manual Steps" in body
        assert "run migration" in body
        # Plan draft provides its own description, so the Closes link must not appear.
        assert "Closes #42" not in body

    def test_uses_plan_draft_when_no_contract_at_all(self, tmp_path):
        """Plan draft is used even when contract load fails entirely."""
        pipeline = _make_pipeline()
        _write_plan_draft(
            tmp_path,
            42,
            title="Draft-only title",
            description="Draft-only description.",
            test_plan="- Test X",
            manual_steps="None",
        )

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert title == "Draft-only title"
        assert "Draft-only description." in body
        assert "Test X" in body

    def test_contract_pr_beats_plan_draft(self, tmp_path):
        """Contract PR metadata wins over plan draft when both exist."""
        pipeline = _make_pipeline()
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        (contract_dir / "42.json").write_text(
            json.dumps(
                _make_contract_json(
                    pr_title="From contract",
                    pr_description="From contract description.",
                )
            )
        )
        _write_plan_draft(
            tmp_path,
            42,
            title="From plan draft",
            description="Plan-draft description.",
            test_plan="- X",
            manual_steps="None",
        )

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert title == "From contract"
        assert "From contract description." in body
        assert "From plan draft" not in body
        assert "Plan-draft description." not in body

    def test_falls_through_to_issue_title_when_draft_missing(self, tmp_path):
        """When neither contract.pr nor plan draft has PR metadata, issue title is used."""
        pipeline = _make_pipeline()
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        (contract_dir / "42.json").write_text(json.dumps(_make_contract_json(pr_title=None)))

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert title == "Fix the auth bug"

    def test_unparseable_plan_draft_falls_through(self, tmp_path):
        """A plan draft with no pr: block falls through to the issue title."""
        pipeline = _make_pipeline()
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        (contract_dir / "42.json").write_text(json.dumps(_make_contract_json(pr_title=None)))
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)
        (drafts_dir / "42-plan.md").write_text("# Plan with no yaml-tasks block\n\nJust prose.\n")

        title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert title == "Fix the auth bug"


class TestBuildPrBodyFallbackBanner:
    """Regression tests for #1975 — when PR metadata falls through to the
    issue-title/generic stub, the body must surface a visible banner
    (and the caller must mark the PR as draft) so reviewers don't
    silently merge a planner-broken PR whose body is empty.
    """

    def test_banner_present_when_yaml_tasks_parse_fails(self, tmp_path):
        """A plan draft with a broken yaml-tasks block emits a banner
        containing the specific PyYAML error message."""
        pipeline = _make_pipeline()
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        (contract_dir / "42.json").write_text(json.dumps(_make_contract_json(pr_title=None)))

        # Reproduces the #1932 failure mode from #1974: unquoted `: int` in
        # a scalar makes PyYAML think a nested mapping starts mid-line.
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)
        (drafts_dir / "42-plan.md").write_text(
            "# Plan\n\n"
            "```yaml\n"
            "# yaml-tasks\n"
            "phases:\n"
            "  - id: 1\n"
            "    name: Phase 1\n"
            "    tasks:\n"
            "      - id: TASK-1-1\n"
            "        description: Add `sequence: int = 0` field to `Event`\n"
            "```\n"
        )

        title, body, used_stub_fallback = _build_pr_body(pipeline, tmp_path)

        assert used_stub_fallback is True
        # Tier 3 still fills in the issue title, but the banner signals it.
        assert title == "Fix the auth bug"
        assert "Automated PR metadata fell back to the issue title" in body
        assert "Opened as a draft to block merge" in body
        # The specific YAML scanner error surfaces in the body so reviewers
        # can see the failure without digging through orchestrator logs.
        assert "Invalid YAML in yaml-tasks" in body
        assert "mapping values are not allowed here" in body
        # The plan draft path is surfaced so the reader can find the file.
        assert ".egg-state/drafts/42-plan.md" in body
        # Banner comes before the generic "Closes #N" / placeholder test plan
        # so a human reader sees the warning before the stub content.
        assert body.index("fell back to the issue title") < body.index("Closes #42")
        assert body.index("fell back to the issue title") < body.index("## Test Plan")

    def test_banner_absent_when_contract_provides_metadata(self, tmp_path):
        """No banner is emitted when contract.pr is populated (tier 1)."""
        pipeline = _make_pipeline()
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        (contract_dir / "42.json").write_text(json.dumps(_make_contract_json()))

        _title, body, used_stub_fallback = _build_pr_body(pipeline, tmp_path)

        assert used_stub_fallback is False
        assert "fell back to the issue title" not in body
        assert "Opened as a draft" not in body

    def test_banner_absent_when_plan_draft_parses_cleanly(self, tmp_path):
        """No banner when tier 2 recovers PR metadata from the plan draft."""
        pipeline = _make_pipeline()
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        (contract_dir / "42.json").write_text(json.dumps(_make_contract_json(pr_title=None)))
        _write_plan_draft(
            tmp_path,
            42,
            title="From plan draft",
            description="Plan-draft description.",
            test_plan="- X",
            manual_steps="None",
        )

        _title, body, used_stub_fallback = _build_pr_body(pipeline, tmp_path)

        assert used_stub_fallback is False
        assert "fell back to the issue title" not in body

    def test_banner_notes_missing_draft(self, tmp_path):
        """When no plan draft exists on disk, the banner says so."""
        pipeline = _make_pipeline()
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        (contract_dir / "42.json").write_text(json.dumps(_make_contract_json(pr_title=None)))

        _title, body, used_stub_fallback = _build_pr_body(pipeline, tmp_path)

        assert used_stub_fallback is True
        assert "fell back to the issue title" in body
        assert "Plan draft not found" in body
        assert ".egg-state/drafts/42-plan.md" in body


class TestBuildPrBodyGithubStaging:
    """Tests for the `.github-staging/` auto-step in _build_pr_body (issue #2508).

    Producer agents are blocked from `.github/` by role patterns. The
    convention introduced in #2508 has agents stage proposed `.github/`
    changes under top-level `.github-staging/`; the PR builder detects
    them and emits a manual step asking the human reviewer to move the
    files into `.github/` before merge.
    """

    def test_no_step_when_staging_dir_absent(self, tmp_path):
        """No manual step when `.github-staging/` does not exist."""
        pipeline = _make_pipeline()

        _title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert ".github-staging" not in body
        assert "Move staged" not in body

    def test_no_step_when_staging_dir_empty(self, tmp_path):
        """No manual step when `.github-staging/` exists but contains no files."""
        pipeline = _make_pipeline()
        (tmp_path / ".github-staging").mkdir()

        _title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert "Move staged" not in body

    def test_emits_step_when_staging_dir_has_files(self, tmp_path):
        """Manual step lists each staged file and tells reviewer to move them."""
        pipeline = _make_pipeline()
        staging = tmp_path / ".github-staging"
        (staging / "workflows").mkdir(parents=True)
        (staging / "workflows" / "test-e2e.yml").write_text("name: e2e\n")
        (staging / "CODEOWNERS").write_text("* @team\n")

        _title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert "## Manual Steps" in body
        assert "Move staged `.github/` changes" in body
        assert "`.github-staging/workflows/test-e2e.yml`" in body
        assert "`.github-staging/CODEOWNERS`" in body
        assert "git mv" in body

    def test_step_merged_with_planner_manual_steps(self, tmp_path):
        """Planner-supplied manual_steps and the auto step share one section."""
        pipeline = _make_pipeline()
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        contract = _make_contract_json()
        contract["pr"]["manual_steps"] = "Pre-merge: run db migration."
        (contract_dir / "42.json").write_text(json.dumps(contract))
        staging = tmp_path / ".github-staging" / "workflows"
        staging.mkdir(parents=True)
        (staging / "ci.yml").write_text("name: ci\n")

        _title, body, _ = _build_pr_body(pipeline, tmp_path)

        # Both the planner step and the auto step appear under one
        # `## Manual Steps` heading (only one heading in the body).
        assert body.count("## Manual Steps") == 1
        assert "Pre-merge: run db migration." in body
        assert "Move staged `.github/` changes" in body
        assert "`.github-staging/workflows/ci.yml`" in body

    def test_ignores_subdirectories_with_no_files(self, tmp_path):
        """Empty subdirectories under `.github-staging/` don't emit the step."""
        pipeline = _make_pipeline()
        (tmp_path / ".github-staging" / "workflows").mkdir(parents=True)

        _title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert "Move staged" not in body

    def test_drops_symlinks_from_staged_paths(self, tmp_path):
        """Symlinks under `.github-staging/` are filtered out (issue #2508).

        ``Path.is_file()`` follows symlinks, so without an explicit
        ``is_symlink()`` guard a malicious staged file pointing at
        ``/etc/passwd`` would survive into the manual-step file list,
        the reviewer's `git mv` would preserve it, and `.github/...`
        would land in the repo as a symlink. This test regression-locks
        the guard so the helper stays the choke point.
        """
        pipeline = _make_pipeline()
        staging = tmp_path / ".github-staging" / "workflows"
        staging.mkdir(parents=True)
        # Real file alongside a symlink — only the real file should
        # appear in the rendered step.
        (staging / "ci.yml").write_text("name: ci\n")
        target = tmp_path / "outside-target.yml"
        target.write_text("name: outside\n")
        (staging / "evil-symlink.yml").symlink_to(target)

        _title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert "Move staged `.github/` changes" in body
        assert "`.github-staging/workflows/ci.yml`" in body
        # The symlink must NOT be surfaced — it would pass `is_file()`
        # but the guard above drops it before the rel-path is recorded.
        assert "evil-symlink.yml" not in body

    def test_drops_step_when_only_symlinks_staged(self, tmp_path):
        """When `.github-staging/` contains only symlinks, no step is emitted.

        Mirrors :meth:`test_no_step_when_staging_dir_empty` for the
        symlink-only case — the symlink-filter must not leave the
        helper in a state where it emits a header with an empty file
        list.
        """
        pipeline = _make_pipeline()
        staging = tmp_path / ".github-staging"
        staging.mkdir()
        target = tmp_path / "outside-target.yml"
        target.write_text("name: outside\n")
        (staging / "only-symlink.yml").symlink_to(target)

        _title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert "Move staged" not in body

    def test_drops_step_when_staging_dir_is_symlink(self, tmp_path):
        """When `.github-staging` itself is a symlink, no step is emitted.

        ``Path.is_dir()`` follows symlinks, and the per-entry
        ``is_symlink()`` guard only checks leaf components — so without
        a guard on the staging dir itself, a malicious
        ``.github-staging -> /etc`` would let ``rglob`` enumerate host
        files into the manual-step file list. Regression-locks the
        directory-as-symlink guard added alongside the per-entry one.
        """
        pipeline = _make_pipeline()
        # Real directory with a regular file the rglob would otherwise pick up.
        real_target = tmp_path / "real-target"
        real_target.mkdir()
        (real_target / "evil.yml").write_text("name: evil\n")
        # `.github-staging` itself is a symlink to that directory.
        (tmp_path / ".github-staging").symlink_to(real_target)

        _title, body, _ = _build_pr_body(pipeline, tmp_path)

        assert "Move staged" not in body
        assert "evil.yml" not in body


class TestAutoCreatePr:
    """Tests for _auto_create_pr."""

    def test_creates_pr_via_gateway(self):
        """Test that _auto_create_pr calls gateway.create_pr with metadata."""
        pipeline = _make_pipeline()
        spawner = MagicMock()
        spawner.gateway.create_pr.return_value = "https://github.com/owner/repo/pull/1"

        with (
            patch(
                "routes.pipelines._build_pr_body",
                return_value=("Fix auth", "Body text", False),
            ),
            patch("routes.pipelines.get_default_branch", return_value="main"),
        ):
            result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result == "https://github.com/owner/repo/pull/1"
        spawner.gateway.create_pr.assert_called_once_with(
            pipeline_id="issue-42",
            repo="owner/repo",
            title="Fix auth",
            body="Body text",
            head="egg/issue-42",
            base="main",
            issue_number=42,
            agent_role="orchestrator",
            mode="public",
            draft=False,
        )

    def test_creates_draft_pr_in_private_mode(self):
        """Test that _auto_create_pr creates a draft PR in private mode."""
        pipeline = _make_pipeline()
        spawner = MagicMock()
        spawner.gateway.create_pr.return_value = "https://github.com/owner/repo/pull/2"

        with (
            patch(
                "routes.pipelines._build_pr_body",
                return_value=("Fix auth", "Body text", False),
            ),
            patch("routes.pipelines.get_default_branch", return_value="main"),
        ):
            result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner, gateway_mode="private")

        assert result == "https://github.com/owner/repo/pull/2"
        spawner.gateway.create_pr.assert_called_once_with(
            pipeline_id="issue-42",
            repo="owner/repo",
            title="Fix auth",
            body="Body text",
            head="egg/issue-42",
            base="main",
            issue_number=42,
            agent_role="orchestrator",
            mode="private",
            draft=True,
        )

    def test_returns_none_when_no_repo(self):
        """Test that _auto_create_pr returns None when repo is missing."""
        pipeline = _make_pipeline(repo=None)
        spawner = MagicMock()

        result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result is None
        spawner.gateway.create_pr.assert_not_called()

    def test_returns_none_when_no_branch(self):
        """Test that _auto_create_pr returns None when branch is missing."""
        pipeline = _make_pipeline(branch=None)
        spawner = MagicMock()

        result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result is None
        spawner.gateway.create_pr.assert_not_called()

    def test_returns_none_on_gateway_error(self):
        """Test that _auto_create_pr returns None on gateway error."""
        pipeline = _make_pipeline()
        spawner = MagicMock()
        spawner.gateway.create_pr.side_effect = Exception("Gateway unreachable")

        with (
            patch("routes.pipelines._build_pr_body", return_value=("Title", "Body", False)),
            patch("routes.pipelines.get_default_branch", return_value="main"),
        ):
            result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result is None

    def test_proceeds_to_create_pr_when_refresh_helper_raises(self):
        """Regression #2224 PR 2: a bug-in-helper raise must not block PR creation.

        ``_refresh_pipeline_branch_against_current_base`` already swallows
        its own errors, but ``_auto_create_pr`` wraps the call in an outer
        ``try/except`` for defense-in-depth.  This test injects an
        exception from the helper and asserts ``gateway.create_pr`` is
        still invoked and its URL returned.
        """
        pipeline = _make_pipeline()
        spawner = MagicMock()
        spawner.gateway.create_pr.return_value = "https://github.com/owner/repo/pull/3"

        with (
            patch(
                "routes.pipelines._build_pr_body",
                return_value=("Fix auth", "Body text", False),
            ),
            patch("routes.pipelines.get_default_branch", return_value="main"),
            patch(
                "routes.pipelines._refresh_pipeline_branch_against_current_base",
                side_effect=RuntimeError("simulated helper bug"),
            ) as mock_refresh,
        ):
            result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result == "https://github.com/owner/repo/pull/3"
        mock_refresh.assert_called_once()
        spawner.gateway.create_pr.assert_called_once()

    def test_stub_fallback_forces_draft_in_public_mode(self):
        """Regression #1975: when _build_pr_body signals stub fallback, the
        PR is opened as a draft even in public mode so humans don't
        silently merge a planner-broken PR."""
        pipeline = _make_pipeline()
        spawner = MagicMock()
        spawner.gateway.create_pr.return_value = "https://github.com/owner/repo/pull/1"

        with (
            patch(
                "routes.pipelines._build_pr_body",
                return_value=("Issue #42", "stub body", True),
            ),
            patch("routes.pipelines.get_default_branch", return_value="main"),
        ):
            result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result == "https://github.com/owner/repo/pull/1"
        call_kwargs = spawner.gateway.create_pr.call_args[1]
        assert call_kwargs["mode"] == "public"
        assert call_kwargs["draft"] is True


class TestComputeGatewayMode:
    """Tests for _compute_gateway_mode helper."""

    def test_uses_explicit_network_mode(self):
        """Returns pipeline.network_mode when set, visibility is None."""
        pipeline = _make_pipeline()
        pipeline.network_mode = "private"
        mode, vis = _compute_gateway_mode(pipeline)
        assert mode == "private"
        assert vis is None

    def test_auto_detects_private_repo(self):
        """Auto-detects private mode from repo visibility."""
        pipeline = _make_pipeline()
        pipeline.network_mode = None
        mock_client = MagicMock()
        mock_client.get_repo_visibility.return_value = "private"
        with patch("routes.pipelines.get_gateway_client", return_value=mock_client):
            mode, vis = _compute_gateway_mode(pipeline)
        assert mode == "private"
        assert vis == "private"

    def test_auto_detects_internal_repo(self):
        """Treats internal repos as private."""
        pipeline = _make_pipeline()
        pipeline.network_mode = None
        mock_client = MagicMock()
        mock_client.get_repo_visibility.return_value = "internal"
        with patch("routes.pipelines.get_gateway_client", return_value=mock_client):
            mode, vis = _compute_gateway_mode(pipeline)
        assert mode == "private"
        assert vis == "internal"

    def test_defaults_to_public(self):
        """Defaults to public when no network_mode and no repo."""
        pipeline = _make_pipeline(repo=None)
        pipeline.network_mode = None
        mode, vis = _compute_gateway_mode(pipeline)
        assert mode == "public"
        assert vis is None

    def test_defaults_to_public_for_public_repo(self):
        """Returns public for public repos."""
        pipeline = _make_pipeline()
        pipeline.network_mode = None
        mock_client = MagicMock()
        mock_client.get_repo_visibility.return_value = "public"
        with patch("routes.pipelines.get_gateway_client", return_value=mock_client):
            mode, vis = _compute_gateway_mode(pipeline)
        assert mode == "public"
        assert vis == "public"


class TestDetectDefaultBranch:
    """Tests for _detect_default_branch."""

    def test_detects_via_symbolic_ref(self, tmp_path):
        """Detects default branch from origin/HEAD symbolic ref."""

        def fake_run(args, **kwargs):
            result = MagicMock()
            if "symbolic-ref" in args:
                result.returncode = 0
                result.stdout = "origin/master\n"
            else:
                result.returncode = 1
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            branch = _detect_default_branch(tmp_path)

        assert branch == "master"

    def test_detects_main_branch(self, tmp_path):
        """Falls back to origin/main when symbolic-ref fails."""

        def fake_run(args, **kwargs):
            result = MagicMock()
            if "symbolic-ref" in args:
                result.returncode = 1
                result.stdout = ""
            elif "rev-parse" in args and "origin/main" in args:
                result.returncode = 0
                result.stdout = "abc123\n"
            else:
                result.returncode = 1
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            branch = _detect_default_branch(tmp_path)

        assert branch == "main"

    def test_detects_master_branch(self, tmp_path):
        """Falls back to origin/master when origin/main doesn't exist."""

        def fake_run(args, **kwargs):
            result = MagicMock()
            if "symbolic-ref" in args:
                result.returncode = 1
                result.stdout = ""
            elif "rev-parse" in args and "origin/main" in args:
                result.returncode = 1
                result.stdout = ""
            elif "rev-parse" in args and "origin/master" in args:
                result.returncode = 0
                result.stdout = "abc123\n"
            else:
                result.returncode = 1
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            branch = _detect_default_branch(tmp_path)

        assert branch == "master"

    def test_falls_back_to_main(self, tmp_path):
        """Falls back to 'main' when nothing resolves."""

        def fake_run(args, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            branch = _detect_default_branch(tmp_path)

        assert branch == "main"


class TestAutoCreatePrPassesBaseBranch:
    """Tests that _auto_create_pr passes the detected base branch."""

    def test_passes_detected_base_branch(self):
        """Verify auto-detected base branch is passed to gateway.create_pr."""
        pipeline = _make_pipeline()
        spawner = MagicMock()
        spawner.gateway.create_pr.return_value = "https://github.com/owner/repo/pull/1"

        with (
            patch("routes.pipelines._build_pr_body", return_value=("Title", "Body", False)),
            patch("routes.pipelines.get_default_branch", return_value="master"),
        ):
            result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result == "https://github.com/owner/repo/pull/1"
        call_kwargs = spawner.gateway.create_pr.call_args
        assert call_kwargs[1]["base"] == "master"

    def test_passes_explicit_base_branch(self):
        """Verify explicit pipeline.base_branch is used for both PR base and body."""
        pipeline = _make_pipeline()
        pipeline.base_branch = "release/v2"
        spawner = MagicMock()
        spawner.gateway.create_pr.return_value = "https://github.com/owner/repo/pull/3"

        with (
            patch(
                "routes.pipelines._build_pr_body",
                return_value=("Title", "Body", False),
            ) as mock_build,
            patch("routes.pipelines.get_default_branch") as mock_detect,
        ):
            result = _auto_create_pr(pipeline, Path("/tmp/repo"), spawner)

        assert result == "https://github.com/owner/repo/pull/3"
        # Verify create_pr receives the explicit base branch
        call_kwargs = spawner.gateway.create_pr.call_args
        assert call_kwargs[1]["base"] == "release/v2"
        # Verify _build_pr_body is called (default_branch no longer passed)
        mock_build.assert_called_once_with(pipeline, Path("/tmp/repo"))
        # Verify get_default_branch is NOT called when explicit base is provided
        mock_detect.assert_not_called()


class TestHandlePrCreationFailure:
    """Tests for _handle_pr_creation_failure — the extracted helper that marks
    the pipeline FAILED when PR creation returns no URL.
    """

    def test_marks_pipeline_and_phase_failed_with_rescue_hint(self):
        """_handle_pr_creation_failure sets pipeline and phase to FAILED and
        attaches an actionable rescue hint containing the branch and repo."""
        pipeline = _make_pipeline()
        pipeline.status = PipelineStatus.RUNNING

        store = MagicMock()
        store.load_pipeline.return_value = pipeline

        with patch("routes.pipelines.get_pipeline_state_lock"):
            _handle_pr_creation_failure(
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                store=store,
            )

        assert pipeline.status == PipelineStatus.FAILED
        # Message starts with the canonical prefix
        assert pipeline.error.startswith("Auto PR creation failed: no PR URL returned")
        # Rescue hint present
        assert "origin/egg/issue-42" in pipeline.error
        assert "owner/repo" in pipeline.error
        assert "gh pr create" in pipeline.error
        assert "--head 'egg/issue-42'" in pipeline.error

        phase_execution = pipeline.get_phase_execution(PipelinePhase.PR)
        assert phase_execution.status == PipelineStatus.FAILED
        assert phase_execution.error == pipeline.error
        assert phase_execution.completed_at is not None

        store.save_pipeline.assert_called_once_with(pipeline)

    def test_reason_arg_surfaces_in_error_message(self):
        """When caller passes ``reason=...`` the message reflects that cause."""
        pipeline = _make_pipeline()
        pipeline.status = PipelineStatus.RUNNING

        store = MagicMock()
        store.load_pipeline.return_value = pipeline

        with patch("routes.pipelines.get_pipeline_state_lock"):
            _handle_pr_creation_failure(
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                store=store,
                reason="gateway push rejected and fetch+rebase reconcile failed",
            )

        assert "gateway push rejected" in pipeline.error
        assert "fetch+rebase reconcile failed" in pipeline.error

    def test_no_rescue_hint_when_branch_missing(self):
        """Without a branch we can't compose a rescue command — degrade gracefully."""
        pipeline = _make_pipeline(branch=None)
        pipeline.status = PipelineStatus.RUNNING

        store = MagicMock()
        store.load_pipeline.return_value = pipeline

        with patch("routes.pipelines.get_pipeline_state_lock"):
            _handle_pr_creation_failure(
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                store=store,
            )

        assert pipeline.error == "Auto PR creation failed: no PR URL returned"
        assert "gh pr create" not in pipeline.error
