"""Tests for ``partition_files_by_role`` in gateway.agent_restrictions.

Covers TASK-5-3 acceptance criteria for issue #1882:

- all-allowed, all-blocked, and mixed inputs
- empty input
- three-way precedence (blocked vs block-exempt vs allowed)
- unknown role fail-closed deny-by-default with WARNING log

Reference:
- gateway/agent_restrictions.py::partition_files_by_role
- shared/egg_restrictions/checker.py:42-44 (deny-by-default semantics)
- shared/egg_restrictions/patterns.py (AgentFilePattern precedence)
"""

from __future__ import annotations

import logging

from agent_restrictions import (
    AgentFilePattern,
    AgentRole,
    get_agent_pattern,
    partition_files_by_role,
)

# ---------------------------------------------------------------------------
# Basic partition behavior
# ---------------------------------------------------------------------------


class TestPartitionBasicBehavior:
    """Basic partition cases: all-allowed, all-blocked, mixed, empty."""

    def test_all_allowed(self):
        """Coder role with only source files yields allowed == files, blocked == []."""
        files = [
            "orchestrator/main.py",
            "gateway/gateway.py",
            "shared/utils.py",
        ]
        allowed, blocked = partition_files_by_role(AgentRole.CODER, files)
        assert allowed == files
        assert blocked == []

    def test_all_blocked(self):
        """Coder role with only markdown/docs files yields blocked == files, allowed == []."""
        files = [
            "docs/guide.md",
            "docs/index.md",
            "README.md",
        ]
        allowed, blocked = partition_files_by_role(AgentRole.CODER, files)
        assert allowed == []
        assert blocked == files

    def test_mixed(self):
        """Coder role: .py files allowed, .md files blocked."""
        files = [
            "orchestrator/main.py",
            "docs/guide.md",
            "gateway/gateway.py",
            "README.md",
        ]
        allowed, blocked = partition_files_by_role(AgentRole.CODER, files)
        assert allowed == ["orchestrator/main.py", "gateway/gateway.py"]
        assert blocked == ["docs/guide.md", "README.md"]

    def test_empty_input(self):
        """Empty input always returns ([], []), regardless of role."""
        allowed, blocked = partition_files_by_role(AgentRole.CODER, [])
        assert allowed == []
        assert blocked == []


# ---------------------------------------------------------------------------
# Unknown role - deny-by-default, fail-closed
# ---------------------------------------------------------------------------


class TestPartitionUnknownRole:
    """Unknown roles fail-closed (deny-by-default) and log a WARNING."""

    def test_unknown_role_is_deny_by_default(self, caplog):
        """Unknown role returns ([], list(files)) with WARNING logged.

        Asserts fail-closed deny-by-default per
        shared/egg_restrictions/checker.py:42-44.
        """
        files = ["orchestrator/main.py", "docs/guide.md", "README.md"]

        # The gateway logger has propagate=False in the gateway's logging
        # config, so caplog (which attaches to the root logger) does not
        # see the record by default. Temporarily re-enable propagation so
        # the test can observe the structured WARNING.
        gateway_logger = logging.getLogger("gateway")
        prior_propagate = gateway_logger.propagate
        gateway_logger.propagate = True
        try:
            with caplog.at_level(logging.WARNING, logger="gateway.agent_restrictions"):
                allowed, blocked = partition_files_by_role("not_a_real_role", files)
        finally:
            gateway_logger.propagate = prior_propagate

        assert allowed == []
        # All files must be blocked (deny-by-default); ordering preserved
        assert blocked == files
        # blocked must be a new list, not the input list
        assert blocked is not files

        # Assert WARNING log was emitted with the expected structured key
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and r.name == "gateway.agent_restrictions"
            and r.getMessage() == "partition_files_by_role_unknown_role"
        ]
        assert len(warning_records) == 1, (
            f"Expected 1 WARNING log with key partition_files_by_role_unknown_role, "
            f"found: {[(r.name, r.levelname, r.getMessage()) for r in caplog.records]}"
        )
        # Structured extras should include the role and file_count
        record = warning_records[0]
        assert getattr(record, "role", None) == "not_a_real_role"
        assert getattr(record, "file_count", None) == len(files)

    def test_unknown_role_empty_input_still_returns_empty_tuples(self):
        """Empty input short-circuits before role lookup and returns ([], [])."""
        allowed, blocked = partition_files_by_role("not_a_real_role", [])
        assert allowed == []
        assert blocked == []


# ---------------------------------------------------------------------------
# Three-way precedence: blocked vs block-exempt vs allowed
# ---------------------------------------------------------------------------


class TestPartitionPrecedence:
    """Three-way precedence tests.

    Per AgentFilePattern.can_write():
    1. blocked_patterns are checked first (security precedence).
    2. block_exempt_patterns carve narrow exceptions out of blocked.
    3. allowed_patterns are the final gate — path must match at least one.
    """

    def test_precedence_blocked_wins_over_allowed(self):
        """A path matching both allowed and blocked is blocked.

        Coder has allowed_patterns=['**'] (catch-all) but
        blocked_patterns includes 'docs/'. A .py file under docs/
        matches the catch-all allow AND the directory block — the block
        must win.
        """
        files = ["docs/foo.py"]
        allowed, blocked = partition_files_by_role(AgentRole.CODER, files)
        assert allowed == []
        assert blocked == ["docs/foo.py"]

    def test_precedence_block_exempt_wins_over_blocked(self):
        """A block-exempt path is treated as allowed even though it matches a blocked pattern.

        Coder has '**/*.md' blocked but 'sandbox/agent-config/rules/*.md'
        is block-exempt. Such a .md file must end up in ``allowed``.
        """
        files = ["sandbox/agent-config/rules/my-rule.md"]
        allowed, blocked = partition_files_by_role(AgentRole.CODER, files)
        assert allowed == ["sandbox/agent-config/rules/my-rule.md"]
        assert blocked == []

    def test_precedence_three_way_split_in_one_call(self):
        """A single call correctly partitions blocked, block-exempt, and plain-allowed paths."""
        files = [
            "orchestrator/main.py",  # plain allowed
            "docs/guide.md",  # blocked (docs/ + **/*.md)
            "sandbox/agent-config/rules/rule.md",  # block-exempt (allowed)
            "README.md",  # blocked (**/*.md)
            "gateway/gateway.py",  # plain allowed
        ]
        allowed, blocked = partition_files_by_role(AgentRole.CODER, files)
        assert allowed == [
            "orchestrator/main.py",
            "sandbox/agent-config/rules/rule.md",
            "gateway/gateway.py",
        ]
        assert blocked == ["docs/guide.md", "README.md"]


# ---------------------------------------------------------------------------
# Role-specific sanity checks
# ---------------------------------------------------------------------------


class TestPartitionRoleSanity:
    """Spot-checks: tester, documenter, overseer roles behave as expected."""

    def test_tester_allowed_patterns(self):
        """Tester role: tests/, conftest.py, and test_*.py files are allowed."""
        files = [
            "tests/test_foo.py",
            "orchestrator/tests/test_bar.py",
            "orchestrator/conftest.py",
            "gateway/tests/test_baz.py",
        ]
        allowed, blocked = partition_files_by_role(AgentRole.TESTER, files)
        assert allowed == files
        assert blocked == []

    def test_tester_blocks_source_and_docs(self):
        """Tester role: source .py outside test patterns and docs/*.md are blocked."""
        files = [
            "orchestrator/main.py",  # not a test file
            "docs/guide.md",  # docs blocked
            "tests/test_foo.py",  # allowed
        ]
        allowed, blocked = partition_files_by_role(AgentRole.TESTER, files)
        assert allowed == ["tests/test_foo.py"]
        # orchestrator/main.py is not in tester's allow list
        assert "orchestrator/main.py" in blocked
        assert "docs/guide.md" in blocked

    def test_documenter_can_write_md_files(self):
        """Documenter role: .md files and docs/ are allowed; source code is blocked."""
        files = [
            "docs/guide.md",
            "README.md",
            "docs/architecture/overview.md",
        ]
        allowed, blocked = partition_files_by_role(AgentRole.DOCUMENTER, files)
        assert allowed == files
        assert blocked == []

    def test_documenter_blocks_source_code(self):
        """Documenter role blocks .py / .ts / source-code files."""
        files = [
            "orchestrator/main.py",
            "src/app.ts",
            "docs/guide.md",
        ]
        allowed, blocked = partition_files_by_role(AgentRole.DOCUMENTER, files)
        assert "orchestrator/main.py" in blocked
        assert "src/app.ts" in blocked
        assert "docs/guide.md" in allowed

    def test_overseer_restricted_to_oversight_dir(self):
        """Overseer role allows .egg-state/oversight/ and blocks source/docs/tests."""
        files = [
            ".egg-state/oversight/report.json",
            ".egg-state/agent-outputs/overseer.json",
            "orchestrator/main.py",
            "docs/guide.md",
            "tests/test_foo.py",
        ]
        allowed, blocked = partition_files_by_role(AgentRole.OVERSEER, files)
        assert ".egg-state/oversight/report.json" in allowed
        assert ".egg-state/agent-outputs/overseer.json" in allowed
        assert "orchestrator/main.py" in blocked
        assert "docs/guide.md" in blocked
        assert "tests/test_foo.py" in blocked


# ---------------------------------------------------------------------------
# Structural invariants: ordering, duplicates
# ---------------------------------------------------------------------------


class TestPartitionStructuralInvariants:
    """Verify the partition preserves input order and duplicates."""

    def test_order_preserved(self):
        """Allowed and blocked lists preserve the order of the input list."""
        files = [
            "docs/a.md",  # blocked (pos 0)
            "orchestrator/x.py",  # allowed (pos 1)
            "docs/b.md",  # blocked (pos 2)
            "gateway/y.py",  # allowed (pos 3)
            "docs/c.md",  # blocked (pos 4)
            "shared/z.py",  # allowed (pos 5)
        ]
        allowed, blocked = partition_files_by_role(AgentRole.CODER, files)
        assert allowed == [
            "orchestrator/x.py",
            "gateway/y.py",
            "shared/z.py",
        ]
        assert blocked == [
            "docs/a.md",
            "docs/b.md",
            "docs/c.md",
        ]

    def test_duplicates_preserved(self):
        """Duplicate entries are preserved in the corresponding list."""
        files = ["a.py", "a.py", "b.md"]
        allowed, blocked = partition_files_by_role(AgentRole.CODER, files)
        # Both copies of a.py should land in allowed
        assert allowed == ["a.py", "a.py"]
        # b.md is blocked by **/*.md
        assert blocked == ["b.md"]

    def test_partition_returns_new_lists(self):
        """The returned lists must not alias the input list."""
        files = ["orchestrator/main.py", "docs/guide.md"]
        allowed, blocked = partition_files_by_role(AgentRole.CODER, files)
        assert allowed is not files
        assert blocked is not files
        # Mutating the input after the call must not affect results
        files.append("README.md")
        assert "README.md" not in allowed
        assert "README.md" not in blocked

    def test_partition_uses_get_agent_pattern(self):
        """Sanity check: partition's per-file outcome matches pattern.can_write()."""
        pattern: AgentFilePattern | None = get_agent_pattern(AgentRole.CODER)
        assert pattern is not None
        files = [
            "orchestrator/main.py",
            "docs/guide.md",
            "sandbox/agent-config/rules/rule.md",
            "README.md",
        ]
        allowed, blocked = partition_files_by_role(AgentRole.CODER, files)
        for f in allowed:
            assert pattern.can_write(f) is True, f"{f} should be writable by coder"
        for f in blocked:
            assert pattern.can_write(f) is False, f"{f} should be blocked for coder"
