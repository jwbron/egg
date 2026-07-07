"""Tests for the egg_restrictions shared package."""

from __future__ import annotations

from egg_restrictions import (
    AGENT_PATTERNS,
    AgentFilePattern,
    AgentRestrictionResult,
    AgentRole,
    check_agent_file_access,
    get_agent_pattern,
    validate_agent_push,
)
from egg_restrictions.patterns import (
    ARCHITECT_PATTERNS,
    AUTOFIXER_PATTERNS,
    CODER_PATTERNS,
    CONFLICT_RESOLVER_PATTERNS,
    DOCUMENTER_PATTERNS,
    EVIDENCE_GATHERER_PATTERNS,
    OVERSEER_PATTERNS,
    REFINER_PATTERNS,
    REVIEWER_AGENT_DESIGN_PATTERNS,
    REVIEWER_CODE_HOLISTIC_PATTERNS,
    REVIEWER_CODE_PATTERNS,
    REVIEWER_CONCURRENCY_PATTERNS,
    REVIEWER_CONTRACT_PATTERNS,
    REVIEWER_PLAN_PATTERNS,
    REVIEWER_REFINE_PATTERNS,
    REVIEWER_SECURITY_PATTERNS,
    RISK_ANALYST_PATTERNS,
    TASK_PLANNER_PATTERNS,
    TESTER_PATTERNS,
)

# ---------------------------------------------------------------------------
# AgentRole
# ---------------------------------------------------------------------------


class TestAgentRole:
    def test_all_19_roles_defined(self):
        roles = [
            AgentRole.CODER,
            AgentRole.TESTER,
            AgentRole.DOCUMENTER,
            AgentRole.ARCHITECT,
            AgentRole.TASK_PLANNER,
            AgentRole.RISK_ANALYST,
            AgentRole.REFINER,
            AgentRole.REVIEWER_CODE,
            AgentRole.REVIEWER_CODE_HOLISTIC,
            AgentRole.REVIEWER_CONTRACT,
            AgentRole.REVIEWER_AGENT_DESIGN,
            AgentRole.REVIEWER_REFINE,
            AgentRole.REVIEWER_PLAN,
            AgentRole.REVIEWER_SECURITY,
            AgentRole.REVIEWER_CONCURRENCY,
            AgentRole.AUTOFIXER,
            AgentRole.CONFLICT_RESOLVER,
            AgentRole.OVERSEER,
        ]
        assert len(roles) == 18
        # All unique
        assert len(set(roles)) == 18

    def test_role_values_are_lowercase(self):
        for attr in dir(AgentRole):
            if attr.isupper() and not attr.startswith("_"):
                assert getattr(AgentRole, attr) == getattr(AgentRole, attr).lower()


# ---------------------------------------------------------------------------
# AGENT_PATTERNS registry
# ---------------------------------------------------------------------------


class TestAgentPatterns:
    def test_registry_has_all_roles(self):
        # Issue #1557 — APPLIER joined the registry (Jira-epic SDLC
        # support); the count grew from 19 to 20. Issue #2925 — ORCHESTRATOR
        # was removed (the orchestrator is the control plane, not an agent
        # role; its gh pre-flights now run on launcher-authed control-plane
        # routes), dropping the count back to 19. SIMPLIFIER (human-focused
        # draft companions) brings it to 20. FIRST_PRINCIPLES_REVIEWER
        # (adversarial premise/direction reviewer, refine phase) brings it to 21.
        # EVIDENCE_GATHERER (read-only shared-evidence gatherer, #3523)
        # brings it to 22.
        assert len(AGENT_PATTERNS) == 22

    def test_registry_keys_match_role_constants(self):
        expected_roles = {
            AgentRole.CODER,
            AgentRole.TESTER,
            AgentRole.DOCUMENTER,
            # Issue #1557 — Jira-epic SDLC pipeline support.
            AgentRole.APPLIER,
            AgentRole.ARCHITECT,
            AgentRole.TASK_PLANNER,
            AgentRole.RISK_ANALYST,
            AgentRole.REFINER,
            # Human-focused draft companions (refine + plan).
            AgentRole.SIMPLIFIER,
            AgentRole.REVIEWER_CODE,
            AgentRole.REVIEWER_CODE_HOLISTIC,
            AgentRole.REVIEWER_CONTRACT,
            AgentRole.REVIEWER_AGENT_DESIGN,
            AgentRole.REVIEWER_REFINE,
            # Adversarial premise/direction reviewer (refine phase).
            AgentRole.FIRST_PRINCIPLES_REVIEWER,
            AgentRole.REVIEWER_PLAN,
            AgentRole.REVIEWER_SECURITY,
            AgentRole.REVIEWER_CONCURRENCY,
            AgentRole.AUTOFIXER,
            AgentRole.CONFLICT_RESOLVER,
            AgentRole.OVERSEER,
            # Read-only shared-evidence gatherer (#3523).
            AgentRole.EVIDENCE_GATHERER,
        }
        assert set(AGENT_PATTERNS.keys()) == expected_roles

    def test_named_constants_match_registry(self):
        assert AGENT_PATTERNS[AgentRole.CODER] is CODER_PATTERNS
        assert AGENT_PATTERNS[AgentRole.TESTER] is TESTER_PATTERNS
        assert AGENT_PATTERNS[AgentRole.DOCUMENTER] is DOCUMENTER_PATTERNS
        assert AGENT_PATTERNS[AgentRole.ARCHITECT] is ARCHITECT_PATTERNS
        assert AGENT_PATTERNS[AgentRole.TASK_PLANNER] is TASK_PLANNER_PATTERNS
        assert AGENT_PATTERNS[AgentRole.RISK_ANALYST] is RISK_ANALYST_PATTERNS
        assert AGENT_PATTERNS[AgentRole.REFINER] is REFINER_PATTERNS
        assert AGENT_PATTERNS[AgentRole.REVIEWER_CODE] is REVIEWER_CODE_PATTERNS
        assert AGENT_PATTERNS[AgentRole.REVIEWER_CODE_HOLISTIC] is REVIEWER_CODE_HOLISTIC_PATTERNS
        assert AGENT_PATTERNS[AgentRole.REVIEWER_CONTRACT] is REVIEWER_CONTRACT_PATTERNS
        assert AGENT_PATTERNS[AgentRole.REVIEWER_AGENT_DESIGN] is REVIEWER_AGENT_DESIGN_PATTERNS
        assert AGENT_PATTERNS[AgentRole.REVIEWER_REFINE] is REVIEWER_REFINE_PATTERNS
        assert AGENT_PATTERNS[AgentRole.REVIEWER_PLAN] is REVIEWER_PLAN_PATTERNS
        assert AGENT_PATTERNS[AgentRole.REVIEWER_SECURITY] is REVIEWER_SECURITY_PATTERNS
        assert AGENT_PATTERNS[AgentRole.REVIEWER_CONCURRENCY] is REVIEWER_CONCURRENCY_PATTERNS
        assert AGENT_PATTERNS[AgentRole.OVERSEER] is OVERSEER_PATTERNS
        assert AGENT_PATTERNS[AgentRole.EVIDENCE_GATHERER] is EVIDENCE_GATHERER_PATTERNS
        assert AGENT_PATTERNS[AgentRole.AUTOFIXER] is AUTOFIXER_PATTERNS
        assert AGENT_PATTERNS[AgentRole.CONFLICT_RESOLVER] is CONFLICT_RESOLVER_PATTERNS


# ---------------------------------------------------------------------------
# get_agent_pattern
# ---------------------------------------------------------------------------


class TestGetAgentPattern:
    def test_returns_pattern_for_known_role(self):
        assert get_agent_pattern("coder") is CODER_PATTERNS

    def test_case_insensitive(self):
        assert get_agent_pattern("CODER") is CODER_PATTERNS
        assert get_agent_pattern("Tester") is TESTER_PATTERNS

    def test_returns_none_for_unknown_role(self):
        assert get_agent_pattern("nonexistent") is None


# ---------------------------------------------------------------------------
# AgentFilePattern.can_write — per-role tests
# ---------------------------------------------------------------------------


class TestCoderPatterns:
    def test_allows_python_source(self):
        assert CODER_PATTERNS.can_write("gateway/server.py")

    def test_allows_typescript(self):
        assert CODER_PATTERNS.can_write("frontend/app.tsx")

    def test_allows_config(self):
        assert CODER_PATTERNS.can_write("config.yaml")
        assert CODER_PATTERNS.can_write("pyproject.toml")

    def test_allows_makefile(self):
        assert CODER_PATTERNS.can_write("Makefile")

    def test_allows_agent_outputs(self):
        assert CODER_PATTERNS.can_write(".egg-state/agent-outputs/coder.json")

    def test_blocks_docs(self):
        assert not CODER_PATTERNS.can_write("docs/guide.md")

    def test_blocks_readme(self):
        assert not CODER_PATTERNS.can_write("README.md")

    def test_allows_test_files(self):
        # Coder authors its own tests (intentional overlap with the tester);
        # the gateway no longer 403s a coder test push. See _build_coder_pattern.
        assert CODER_PATTERNS.can_write("tests/test_foo.py")
        assert CODER_PATTERNS.can_write("test/test_bar.py")
        assert CODER_PATTERNS.can_write("gateway/tests/test_x.py")

    def test_allows_conftest(self):
        assert CODER_PATTERNS.can_write("conftest.py")

    def test_blocks_contracts(self):
        assert not CODER_PATTERNS.can_write(".egg-state/contracts/spec.json")


class TestCoderBlocklistComplement:
    """TASK-5-1 (#1901): coder is a blocklist-complement.

    The historical coder allowlist enumerated extensions/filenames.  The
    new model uses ``allowed_patterns=["**"]`` paired with a blocklist
    that carves out the documenter scope (docs/markdown) and
    pipeline-state directories (.egg-state/, except agent-outputs/ which
    is carved back).

    The tester scope (test files) is **no longer carved out**: the coder
    authors its own tests, intentionally overlapping the tester (the
    #1901 strict-complement invariant is retired for the test scope
    only). See ``_build_coder_pattern``.

    These tests assert behavior, not pattern shape — they remain green
    if patterns.py is later refactored as long as the blocklist contract
    holds (e.g. extensionless scripts and arbitrary new top-level paths
    stay coder-writable).
    """

    # --- Allowed (TASK-5-1 True list) ---

    def test_allows_extensionless_bin_egg(self):
        """bin/egg has no extension — was blocked under the legacy allowlist."""
        assert CODER_PATTERNS.can_write("bin/egg")

    def test_allows_extensionless_bin_egg_deploy(self):
        assert CODER_PATTERNS.can_write("bin/egg-deploy")

    def test_allows_extensionless_bin_egg_status(self):
        assert CODER_PATTERNS.can_write("bin/egg-status")

    def test_allows_sandbox_egg_script(self):
        assert CODER_PATTERNS.can_write("sandbox/egg")

    def test_allows_sandbox_scripts_gh_shim(self):
        """sandbox/scripts/ is writable; the gateway is the sole egress
        chokepoint, so credential-shim modifications are reviewed by
        reviewer_security rather than blocked at the role-pattern layer."""
        assert CODER_PATTERNS.can_write("sandbox/scripts/gh")

    def test_allows_sandbox_scripts_git_credential_helper(self):
        """sandbox/scripts/ is writable; see test_allows_sandbox_scripts_gh_shim."""
        assert CODER_PATTERNS.can_write("sandbox/scripts/git-credential-github-token")

    def test_blocks_github_workflows(self):
        """.github/ is blocked (branch-protection invariant)."""
        assert not CODER_PATTERNS.can_write(".github/workflows/ci.yml")

    def test_blocks_github_codeowners(self):
        """.github/ is blocked (branch-protection invariant)."""
        assert not CODER_PATTERNS.can_write(".github/CODEOWNERS")

    def test_allows_license_file(self):
        """LICENSE — extensionless top-level metadata."""
        assert CODER_PATTERNS.can_write("LICENSE")

    def test_allows_dockerignore(self):
        assert CODER_PATTERNS.can_write(".dockerignore")

    def test_allows_arbitrary_new_path(self):
        """New top-level paths (future tools) should not require allowlist edits."""
        assert CODER_PATTERNS.can_write("path/to/new-thing")

    def test_allows_pyproject_toml(self):
        assert CODER_PATTERNS.can_write("pyproject.toml")

    def test_allows_makefile(self):
        assert CODER_PATTERNS.can_write("Makefile")

    def test_allows_agent_outputs_handoff(self):
        """Coder handoff lives in .egg-state/agent-outputs/ — carved back via exempt."""
        assert CODER_PATTERNS.can_write(".egg-state/agent-outputs/coder.json")

    def test_allows_agent_anchors(self):
        """Agent anchors live in .egg-state/agent-anchors/ — carved back via exempt."""
        assert CODER_PATTERNS.can_write(".egg-state/agent-anchors/coder.json")

    def test_allows_skills_skill_md(self):
        """skills/ is exempted from the **/*.md block (skill definitions)."""
        assert CODER_PATTERNS.can_write("skills/my-skill/SKILL.md")

    def test_allows_skills_handler_py(self):
        """Non-md files in skills/ are also coder-owned."""
        assert CODER_PATTERNS.can_write("skills/my-skill/handler.py")

    def test_allows_agent_config_rules_md(self):
        """sandbox/agent-config/rules/*.md exempted from **/*.md block."""
        assert CODER_PATTERNS.can_write("sandbox/agent-config/rules/foo.md")

    def test_allows_agent_config_commands_md(self):
        assert CODER_PATTERNS.can_write("sandbox/agent-config/commands/bar.md")

    # --- Blocked (TASK-5-1 False list) ---

    def test_blocks_docs_md(self):
        assert not CODER_PATTERNS.can_write("docs/foo.md")

    def test_blocks_root_readme(self):
        assert not CODER_PATTERNS.can_write("README.md")

    def test_allows_tests_dir_test_file(self):
        # Coder authors its own tests (overlap with tester) — no longer blocked.
        assert CODER_PATTERNS.can_write("tests/test_x.py")

    def test_allows_singular_test_dir(self):
        assert CODER_PATTERNS.can_write("test/test_y.py")

    def test_allows_nested_tests_init(self):
        """gateway/tests/__init__.py — coder-writable now that tests overlap."""
        assert CODER_PATTERNS.can_write("gateway/tests/__init__.py")

    def test_allows_root_conftest(self):
        # Root-level conftest is coder-writable now that the coder authors tests.
        assert CODER_PATTERNS.can_write("conftest.py")

    def test_blocks_egg_state_contracts(self):
        assert not CODER_PATTERNS.can_write(".egg-state/contracts/spec.json")

    def test_blocks_egg_state_drafts(self):
        assert not CODER_PATTERNS.can_write(".egg-state/drafts/1901-plan.md")

    def test_blocks_egg_state_reviews(self):
        assert not CODER_PATTERNS.can_write(".egg-state/reviews/verdict.json")

    def test_blocks_egg_state_future_subdir(self):
        """Hypothetical future .egg-state subdir — blocklist is catch-all."""
        assert not CODER_PATTERNS.can_write(".egg-state/secrets/key")

    def test_blocks_path_traversal_via_can_write(self):
        # Path-traversal regression — this assertion MUST go through
        # CODER_PATTERNS.can_write (NOT matches_pattern) because the
        # traversal guard lives in _normalize_path and is only invoked
        # by can_write.
        assert not CODER_PATTERNS.can_write("../../etc/passwd")


class TestFixtureTreeCarveOut:
    """#3396: test-fixture / testdata trees are test inputs, not docs.

    Fixtures deliberately imitate doc files (``AGENTS.md``, ``README.md``,
    ``.cursor/rules.md``, ...) because the tools under test scan doc
    files. Without a carve-out the ``**/*.md`` docs block misclassifies
    them as documenter-owned and 403s the coder/tester that ship them
    alongside the test. These are the exact paths from the #3396 denial.
    """

    FIXTURE_PATHS = [
        ".agents/tools/agents_tools/harness/fixtures/excluded-siblings/.cursor/rules.md",
        ".agents/tools/agents_tools/harness/fixtures/excluded-siblings/.github/instructions/copilot.md",
        ".agents/tools/agents_tools/harness/fixtures/excluded-siblings/.pi/prompts.md",
        ".agents/tools/agents_tools/harness/fixtures/staled/AGENTS.md",
        ".agents/tools/agents_tools/harness/fixtures/unresolvable-citation/AGENTS.md",
        ".agents/tools/testdata/README.md",
        ".agents/tools/testdata/mini_repo/AGENTS.md",
    ]

    def test_coder_can_write_fixture_markdown(self):
        for path in self.FIXTURE_PATHS:
            assert CODER_PATTERNS.can_write(path), path

    def test_tester_can_write_fixture_markdown(self):
        # The tester review-and-hardens fixtures alongside the coder.
        for path in self.FIXTURE_PATHS:
            assert TESTER_PATTERNS.can_write(path), path

    def test_carveout_covers_non_md_fixture_files(self):
        # The directory-form carve-out is extension-agnostic: any file
        # under a fixtures/testdata tree is a test input.
        assert CODER_PATTERNS.can_write("pkg/fixtures/data/sample.json")
        assert CODER_PATTERNS.can_write("pkg/testdata/golden.txt")

    def test_real_docs_stay_documenter_owned(self):
        # Negative control: genuine documentation must NOT be swept in.
        for path in ["docs/guides/testing.md", "README.md", "shared/README.md", "docs/index.md"]:
            assert not CODER_PATTERNS.can_write(path), path
            assert not TESTER_PATTERNS.can_write(path), path
            assert DOCUMENTER_PATTERNS.can_write(path), path

    def test_hard_blocks_unaffected(self):
        # The carve-out must not open a hole in the security blocks.
        assert not CODER_PATTERNS.can_write(".egg-state/contracts/spec.json")
        assert not CODER_PATTERNS.can_write(".github/workflows/ci.yml")
        assert not TESTER_PATTERNS.can_write(".egg-state/contracts/spec.json")

    def test_carveout_does_not_punch_through_github_block(self):
        # #3396 regression: the ``**/fixtures/`` / ``**/testdata/``
        # block-exempt carve-outs are evaluated against the union of all
        # blocked patterns, so a fixture/testdata segment under ``.github/``
        # must NOT become writable. ``.github/`` is a hard block that no
        # exemption can override.
        github_fixture_paths = [
            ".github/fixtures/x.yml",
            ".github/fixtures/x.md",
            ".github/actions/foo/testdata/case.yml",
            ".github/testdata/readme.md",
        ]
        for path in github_fixture_paths:
            assert not CODER_PATTERNS.can_write(path), path
            assert not TESTER_PATTERNS.can_write(path), path

    def test_carveout_does_not_punch_through_egg_state_blocks(self):
        # #3396 regression: a fixture/testdata segment under ANY ``.egg-state/``
        # subtree must stay blocked for BOTH roles. The whole ``.egg-state/``
        # tree is hard-blocked (only agent-outputs/agent-anchors carved back),
        # so no ``**/fixtures/`` / ``**/testdata/`` exemption can reach it.
        #
        # These include the subtrees the previous (enumeration-based) fix
        # missed — ``brc-history`` and ``checks`` are present and git-tracked
        # in the repo today — plus contracts/drafts/pipelines/reviews/oversight.
        egg_state_fixture_paths = [
            ".egg-state/contracts/fixtures/z.json",
            ".egg-state/drafts/fixtures/a.json",
            ".egg-state/pipelines/testdata/b.json",
            ".egg-state/reviews/testdata/c.json",
            ".egg-state/oversight/fixtures/d.json",
            ".egg-state/brc-history/fixtures/x.json",
            ".egg-state/checks/fixtures/x.json",
            ".egg-state/checkpoints/fixtures/y.json",
        ]
        for path in egg_state_fixture_paths:
            assert not CODER_PATTERNS.can_write(path), f"coder {path}"
            assert not TESTER_PATTERNS.can_write(path), f"tester {path}"

    def test_carveout_does_not_punch_through_future_egg_state_subdir(self):
        # The invariant must hold for subdirs that don't exist yet — the
        # whole-tree hard block is what makes this true without editing the
        # pattern list when a new ``.egg-state/`` subdir is added.
        for role_pattern in (CODER_PATTERNS, TESTER_PATTERNS):
            assert not role_pattern.can_write(".egg-state/some-new-dir/fixtures/x.json"), (
                role_pattern.role
            )
            assert not role_pattern.can_write(".egg-state/another-future-dir/testdata/y.json"), (
                role_pattern.role
            )

    def test_egg_state_carvebacks_still_writable(self):
        # The whole ``.egg-state/`` tree is hard-blocked, but the handoff
        # carve-backs (agent-outputs/, and for the coder agent-anchors/) are
        # restored via ``hard_block_exempt_patterns`` so they keep working.
        assert CODER_PATTERNS.can_write(".egg-state/agent-outputs/handoff.json")
        assert CODER_PATTERNS.can_write(".egg-state/agent-anchors/anchor.json")
        # Markdown handoffs (brc-memory.md) must clear the docs block too.
        assert CODER_PATTERNS.can_write(".egg-state/agent-outputs/coder/brc-memory.md")
        assert TESTER_PATTERNS.can_write(".egg-state/agent-outputs/tester.json")


class TestTesterPatterns:
    def test_allows_test_dirs(self):
        assert TESTER_PATTERNS.can_write("tests/test_foo.py")
        assert TESTER_PATTERNS.can_write("test/test_bar.py")

    def test_allows_nested_test_dirs(self):
        assert TESTER_PATTERNS.can_write("gateway/tests/test_server.py")

    def test_allows_test_file_patterns(self):
        assert TESTER_PATTERNS.can_write("some_test.py")
        assert TESTER_PATTERNS.can_write("app.test.ts")
        assert TESTER_PATTERNS.can_write("app.spec.js")

    def test_allows_conftest(self):
        assert TESTER_PATTERNS.can_write("conftest.py")
        assert TESTER_PATTERNS.can_write("tests/conftest.py")

    def test_allows_agent_outputs(self):
        assert TESTER_PATTERNS.can_write(".egg-state/agent-outputs/tester.json")

    def test_blocks_docs(self):
        assert not TESTER_PATTERNS.can_write("docs/guide.md")

    def test_blocks_contracts(self):
        assert not TESTER_PATTERNS.can_write(".egg-state/contracts/spec.json")

    def test_blocks_github_dir(self):
        # Issue #2521: parity with ``TESTER_ROLE.blocked_write``.
        # ``.github/test_actions.py`` is the load-bearing case — it matches
        # the tester's ``**/test_*.py`` allowlist, so only the new
        # `.github/` blocked entry stops it. The other two are blocked
        # by other rules even without the new entry.
        assert not TESTER_PATTERNS.can_write(".github/test_actions.py")
        assert not TESTER_PATTERNS.can_write(".github/CODEOWNERS")
        assert not TESTER_PATTERNS.can_write(".github/PULL_REQUEST_TEMPLATE.md")


class TestDocumenterPatterns:
    def test_allows_docs_dir(self):
        assert DOCUMENTER_PATTERNS.can_write("docs/guide.md")

    def test_allows_markdown_anywhere(self):
        assert DOCUMENTER_PATTERNS.can_write("README.md")
        assert DOCUMENTER_PATTERNS.can_write("CONTRIBUTING.md")

    def test_allows_agent_outputs(self):
        assert DOCUMENTER_PATTERNS.can_write(".egg-state/agent-outputs/doc.json")

    def test_blocks_source_code(self):
        assert not DOCUMENTER_PATTERNS.can_write("gateway/server.py")
        assert not DOCUMENTER_PATTERNS.can_write("app.ts")

    def test_blocks_test_dirs(self):
        assert not DOCUMENTER_PATTERNS.can_write("tests/test_foo.py")

    def test_blocks_contracts(self):
        assert not DOCUMENTER_PATTERNS.can_write(".egg-state/contracts/spec.json")


class TestArchitectPatterns:
    def test_allows_drafts(self):
        assert ARCHITECT_PATTERNS.can_write(".egg-state/drafts/arch.md")

    def test_allows_agent_outputs(self):
        assert ARCHITECT_PATTERNS.can_write(".egg-state/agent-outputs/arch.json")

    def test_blocks_source_code_dirs(self):
        assert not ARCHITECT_PATTERNS.can_write("src/main.py")
        assert not ARCHITECT_PATTERNS.can_write("gateway/server.py")
        assert not ARCHITECT_PATTERNS.can_write("shared/utils.py")

    def test_blocks_docs(self):
        assert not ARCHITECT_PATTERNS.can_write("docs/guide.md")

    def test_blocks_tests(self):
        assert not ARCHITECT_PATTERNS.can_write("tests/test_foo.py")

    def test_blocks_contracts(self):
        assert not ARCHITECT_PATTERNS.can_write(".egg-state/contracts/spec.json")

    def test_blocks_reviews(self):
        assert not ARCHITECT_PATTERNS.can_write(".egg-state/reviews/review.json")


class TestTaskPlannerPatterns:
    def test_allows_drafts(self):
        assert TASK_PLANNER_PATTERNS.can_write(".egg-state/drafts/plan.md")

    def test_allows_agent_outputs(self):
        assert TASK_PLANNER_PATTERNS.can_write(".egg-state/agent-outputs/plan.json")

    def test_blocks_source(self):
        assert not TASK_PLANNER_PATTERNS.can_write("src/main.py")

    def test_blocks_contracts(self):
        assert not TASK_PLANNER_PATTERNS.can_write(".egg-state/contracts/spec.json")


class TestRiskAnalystPatterns:
    def test_allows_drafts(self):
        assert RISK_ANALYST_PATTERNS.can_write(".egg-state/drafts/risk.md")

    def test_allows_agent_outputs(self):
        assert RISK_ANALYST_PATTERNS.can_write(".egg-state/agent-outputs/risk.json")

    def test_blocks_source(self):
        assert not RISK_ANALYST_PATTERNS.can_write("lib/utils.py")

    def test_blocks_contracts(self):
        assert not RISK_ANALYST_PATTERNS.can_write(".egg-state/contracts/spec.json")


class TestRefinerPatterns:
    def test_allows_drafts(self):
        assert REFINER_PATTERNS.can_write(".egg-state/drafts/refined.md")

    def test_allows_agent_outputs(self):
        assert REFINER_PATTERNS.can_write(".egg-state/agent-outputs/refiner.json")

    def test_blocks_source_code(self):
        assert not REFINER_PATTERNS.can_write("gateway/server.py")
        assert not REFINER_PATTERNS.can_write("app.ts")
        assert not REFINER_PATTERNS.can_write("main.go")

    def test_blocks_contracts(self):
        assert not REFINER_PATTERNS.can_write(".egg-state/contracts/spec.json")


class TestReviewerCodePatterns:
    def test_allows_reviews(self):
        assert REVIEWER_CODE_PATTERNS.can_write(".egg-state/reviews/code_review.json")

    def test_allows_agent_outputs(self):
        assert REVIEWER_CODE_PATTERNS.can_write(".egg-state/agent-outputs/review.json")

    def test_blocks_source(self):
        assert not REVIEWER_CODE_PATTERNS.can_write("src/main.py")
        assert not REVIEWER_CODE_PATTERNS.can_write("gateway/server.py")

    def test_blocks_docs(self):
        assert not REVIEWER_CODE_PATTERNS.can_write("docs/guide.md")

    def test_blocks_contracts(self):
        assert not REVIEWER_CODE_PATTERNS.can_write(".egg-state/contracts/spec.json")

    def test_blocks_drafts(self):
        assert not REVIEWER_CODE_PATTERNS.can_write(".egg-state/drafts/draft.md")


class TestReviewerContractPatterns:
    def test_allows_reviews(self):
        assert REVIEWER_CONTRACT_PATTERNS.can_write(".egg-state/reviews/contract_review.json")

    def test_allows_contracts(self):
        assert REVIEWER_CONTRACT_PATTERNS.can_write(".egg-state/contracts/spec.json")

    def test_allows_agent_outputs(self):
        assert REVIEWER_CONTRACT_PATTERNS.can_write(".egg-state/agent-outputs/review.json")

    def test_blocks_source(self):
        assert not REVIEWER_CONTRACT_PATTERNS.can_write("src/main.py")

    def test_blocks_drafts(self):
        assert not REVIEWER_CONTRACT_PATTERNS.can_write(".egg-state/drafts/draft.md")


class TestReviewerAgentDesignPatterns:
    def test_allows_reviews(self):
        assert REVIEWER_AGENT_DESIGN_PATTERNS.can_write(".egg-state/reviews/design_review.json")

    def test_allows_agent_outputs(self):
        assert REVIEWER_AGENT_DESIGN_PATTERNS.can_write(".egg-state/agent-outputs/review.json")

    def test_blocks_source(self):
        assert not REVIEWER_AGENT_DESIGN_PATTERNS.can_write("src/main.py")

    def test_blocks_contracts(self):
        assert not REVIEWER_AGENT_DESIGN_PATTERNS.can_write(".egg-state/contracts/spec.json")


class TestReviewerRefinePatterns:
    def test_allows_reviews(self):
        assert REVIEWER_REFINE_PATTERNS.can_write(".egg-state/reviews/refine_review.json")

    def test_allows_agent_outputs(self):
        assert REVIEWER_REFINE_PATTERNS.can_write(".egg-state/agent-outputs/review.json")

    def test_blocks_source(self):
        assert not REVIEWER_REFINE_PATTERNS.can_write("gateway/server.py")

    def test_blocks_drafts(self):
        assert not REVIEWER_REFINE_PATTERNS.can_write(".egg-state/drafts/draft.md")


class TestReviewerPlanPatterns:
    def test_allows_reviews(self):
        assert REVIEWER_PLAN_PATTERNS.can_write(".egg-state/reviews/plan_review.json")

    def test_allows_agent_outputs(self):
        assert REVIEWER_PLAN_PATTERNS.can_write(".egg-state/agent-outputs/review.json")

    def test_blocks_source(self):
        assert not REVIEWER_PLAN_PATTERNS.can_write("shared/utils.py")

    def test_blocks_contracts(self):
        assert not REVIEWER_PLAN_PATTERNS.can_write(".egg-state/contracts/spec.json")


class TestOverseerPatterns:
    def test_allows_oversight(self):
        assert OVERSEER_PATTERNS.can_write(".egg-state/oversight/log.json")

    def test_allows_agent_outputs(self):
        assert OVERSEER_PATTERNS.can_write(".egg-state/agent-outputs/overseer.json")

    def test_blocks_source(self):
        assert not OVERSEER_PATTERNS.can_write("src/main.py")
        assert not OVERSEER_PATTERNS.can_write("orchestrator/engine.py")

    def test_blocks_docs(self):
        assert not OVERSEER_PATTERNS.can_write("docs/guide.md")

    def test_blocks_contracts(self):
        assert not OVERSEER_PATTERNS.can_write(".egg-state/contracts/spec.json")

    def test_blocks_drafts(self):
        assert not OVERSEER_PATTERNS.can_write(".egg-state/drafts/draft.md")

    def test_blocks_reviews(self):
        assert not OVERSEER_PATTERNS.can_write(".egg-state/reviews/review.json")


class TestAutofixerPatterns:
    def test_allows_source_code(self):
        assert AUTOFIXER_PATTERNS.can_write("gateway/server.py")
        assert AUTOFIXER_PATTERNS.can_write("app.ts")
        assert AUTOFIXER_PATTERNS.can_write("script.sh")

    def test_allows_config(self):
        assert AUTOFIXER_PATTERNS.can_write("config.yaml")
        assert AUTOFIXER_PATTERNS.can_write("Makefile")

    def test_allows_agent_outputs(self):
        assert AUTOFIXER_PATTERNS.can_write(".egg-state/agent-outputs/fix.json")

    def test_blocks_docs(self):
        assert not AUTOFIXER_PATTERNS.can_write("docs/guide.md")
        assert not AUTOFIXER_PATTERNS.can_write("README.md")

    def test_blocks_contracts(self):
        assert not AUTOFIXER_PATTERNS.can_write(".egg-state/contracts/spec.json")


class TestConflictResolverPatterns:
    def test_allows_source(self):
        assert CONFLICT_RESOLVER_PATTERNS.can_write("gateway/server.py")

    def test_allows_tests(self):
        assert CONFLICT_RESOLVER_PATTERNS.can_write("tests/test_foo.py")

    def test_allows_docs(self):
        assert CONFLICT_RESOLVER_PATTERNS.can_write("docs/guide.md")
        assert CONFLICT_RESOLVER_PATTERNS.can_write("README.md")

    def test_allows_agent_outputs(self):
        assert CONFLICT_RESOLVER_PATTERNS.can_write(".egg-state/agent-outputs/resolve.json")

    def test_blocks_contracts(self):
        assert not CONFLICT_RESOLVER_PATTERNS.can_write(".egg-state/contracts/spec.json")

    def test_blocks_drafts(self):
        assert not CONFLICT_RESOLVER_PATTERNS.can_write(".egg-state/drafts/draft.md")

    def test_blocks_pipelines(self):
        assert not CONFLICT_RESOLVER_PATTERNS.can_write(".egg-state/pipelines/pipe.json")

    def test_blocks_reviews(self):
        assert not CONFLICT_RESOLVER_PATTERNS.can_write(".egg-state/reviews/review.json")

    def test_blocks_oversight(self):
        assert not CONFLICT_RESOLVER_PATTERNS.can_write(".egg-state/oversight/log.json")


# ---------------------------------------------------------------------------
# AgentFilePattern — edge cases
# ---------------------------------------------------------------------------


class TestAgentFilePatternEdgeCases:
    def test_path_traversal_rejected(self):
        assert not CODER_PATTERNS.can_write("../../etc/passwd")
        assert not CODER_PATTERNS.can_write("src/../../../etc/shadow")

    def test_dotdot_in_filename_ok(self):
        # A filename literally containing ".." but not as a path component
        # posixpath.normpath("foo/bar..py") => "foo/bar..py" — no traversal
        pattern = AgentFilePattern(role="test", allowed_patterns=["**/*.py"])
        assert pattern.can_write("foo/bar..py")

    def test_empty_allowed_patterns_blocks_all(self):
        pattern = AgentFilePattern(role="test", allowed_patterns=[], blocked_patterns=[])
        assert not pattern.can_write("anything.py")

    def test_leading_dot_slash_normalized(self):
        assert CODER_PATTERNS.can_write("./gateway/server.py")

    def test_leading_slash_stripped(self):
        assert CODER_PATTERNS.can_write("/gateway/server.py")

    def test_exact_match(self):
        pattern = AgentFilePattern(role="test", allowed_patterns=["Makefile"])
        assert pattern.can_write("Makefile")
        assert not pattern.can_write("sub/Makefile")

    def test_prefix_match(self):
        pattern = AgentFilePattern(role="test", allowed_patterns=["src/"])
        assert pattern.can_write("src/main.py")
        assert not pattern.can_write("lib/main.py")

    def test_blocked_takes_precedence_over_allowed(self):
        pattern = AgentFilePattern(
            role="test",
            allowed_patterns=["**/*.py"],
            blocked_patterns=["tests/"],
        )
        assert not pattern.can_write("tests/test_foo.py")
        assert pattern.can_write("gateway/server.py")


# ---------------------------------------------------------------------------
# check_agent_file_access
# ---------------------------------------------------------------------------


class TestCheckAgentFileAccess:
    def test_known_role_allowed(self):
        allowed, blocked, reason = check_agent_file_access("coder", ["gateway/server.py"])
        assert allowed is True
        assert blocked == []

    def test_known_role_blocked(self):
        allowed, blocked, reason = check_agent_file_access("coder", ["docs/guide.md"])
        assert allowed is False
        assert "docs/guide.md" in blocked
        assert "cannot modify" in reason

    def test_unknown_role_deny_all(self):
        files = ["any_file.py", "other.txt"]
        allowed, blocked, reason = check_agent_file_access("nonexistent_role", files)
        assert allowed is False
        assert blocked == files
        assert "deny-by-default" in reason

    def test_empty_file_list(self):
        allowed, blocked, reason = check_agent_file_access("coder", [])
        assert allowed is True
        assert blocked == []

    def test_mixed_allowed_and_blocked(self):
        # tests/ is now coder-writable (overlap with the tester); docs/ stays
        # blocked (documenter's scope). A coder-authored test lands in allowed.
        files = ["gateway/server.py", "docs/guide.md", "tests/test_foo.py"]
        allowed, blocked, reason = check_agent_file_access("coder", files)
        assert allowed is False
        assert "docs/guide.md" in blocked
        assert "tests/test_foo.py" not in blocked
        assert "gateway/server.py" not in blocked

    def test_truncation_message_for_many_blocked(self):
        files = [f"docs/file{i}.md" for i in range(10)]
        allowed, blocked, reason = check_agent_file_access("coder", files)
        assert allowed is False
        assert "and 5 more" in reason

    def test_path_traversal_blocked(self):
        allowed, blocked, reason = check_agent_file_access("coder", ["../../etc/passwd"])
        assert allowed is False
        assert "../../etc/passwd" in blocked


# ---------------------------------------------------------------------------
# AgentRestrictionResult
# ---------------------------------------------------------------------------


class TestAgentRestrictionResult:
    def test_allow_factory(self):
        result = AgentRestrictionResult.allow("coder", "All good")
        assert result.allowed is True
        assert result.role == "coder"
        assert result.message == "All good"
        assert result.blocked_files == []

    def test_block_factory(self):
        result = AgentRestrictionResult.block("coder", ["docs/guide.md"], "Cannot modify docs")
        assert result.allowed is False
        assert result.role == "coder"
        assert result.blocked_files == ["docs/guide.md"]
        assert result.message == "Cannot modify docs"

    def test_default_blocked_files_empty(self):
        result = AgentRestrictionResult(allowed=True, message="ok", role="coder")
        assert result.blocked_files == []


# ---------------------------------------------------------------------------
# validate_agent_push
# ---------------------------------------------------------------------------


class TestValidateAgentPush:
    def test_empty_role_allows(self):
        result = validate_agent_push("", ["any_file.py"])
        assert result.allowed is True
        assert result.role == ""

    def test_empty_files_allows(self):
        result = validate_agent_push("coder", [])
        assert result.allowed is True

    def test_allowed_push(self):
        result = validate_agent_push("coder", ["gateway/server.py", "config.yaml"])
        assert result.allowed is True
        assert result.role == "coder"

    def test_blocked_push(self):
        result = validate_agent_push("coder", ["docs/guide.md"])
        assert result.allowed is False
        assert result.role == "coder"
        assert "docs/guide.md" in result.blocked_files

    def test_unknown_role_denies(self):
        result = validate_agent_push("unknown_role", ["any_file.py"])
        assert result.allowed is False
        assert "any_file.py" in result.blocked_files

    def test_tester_push(self):
        result = validate_agent_push("tester", ["tests/test_foo.py", "conftest.py"])
        assert result.allowed is True

    def test_documenter_push(self):
        result = validate_agent_push("documenter", ["docs/guide.md", "README.md"])
        assert result.allowed is True

    def test_architect_push_drafts(self):
        result = validate_agent_push("architect", [".egg-state/drafts/arch.md"])
        assert result.allowed is True

    def test_architect_push_source_blocked(self):
        result = validate_agent_push("architect", ["src/main.py"])
        assert result.allowed is False

    def test_reviewer_code_push_reviews(self):
        result = validate_agent_push("reviewer_code", [".egg-state/reviews/review.json"])
        assert result.allowed is True

    def test_reviewer_contract_push_contracts(self):
        result = validate_agent_push("reviewer_contract", [".egg-state/contracts/spec.json"])
        assert result.allowed is True

    def test_overseer_push_oversight(self):
        result = validate_agent_push("overseer", [".egg-state/oversight/log.json"])
        assert result.allowed is True

    def test_autofixer_push_source(self):
        result = validate_agent_push("autofixer", ["gateway/server.py"])
        assert result.allowed is True

    def test_conflict_resolver_push_source(self):
        result = validate_agent_push("conflict_resolver", ["gateway/server.py"])
        assert result.allowed is True
