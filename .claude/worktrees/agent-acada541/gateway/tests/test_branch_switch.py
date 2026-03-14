"""Tests for is_branch_switch() in git_client.py.

Validates the heuristic that distinguishes branch-switching checkout/switch
commands from file-level checkouts.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from git_client import is_branch_switch


class TestIsBranchSwitchNonCheckout:
    """Non-checkout/switch operations always return False."""

    @pytest.mark.parametrize(
        "operation",
        ["commit", "push", "pull", "fetch", "status", "log", "diff", "add", "reset"],
    )
    def test_non_checkout_operations_return_false(self, operation):
        assert is_branch_switch(operation, ["some-arg"]) is False

    def test_empty_operation(self):
        assert is_branch_switch("", []) is False


class TestIsBranchSwitchSwitch:
    """git switch always targets branches."""

    def test_switch_with_branch_name(self):
        assert is_branch_switch("switch", ["main"]) is True

    def test_switch_with_create_flag(self):
        assert is_branch_switch("switch", ["-c", "new-branch"]) is True

    def test_switch_with_no_args(self):
        """Bare switch is still a branch operation."""
        assert is_branch_switch("switch", []) is True

    def test_switch_with_detach(self):
        assert is_branch_switch("switch", ["--detach", "HEAD"]) is True


class TestIsBranchSwitchCheckoutBranchCreate:
    """checkout with -b, -B, --orphan are branch switches."""

    def test_checkout_dash_b(self):
        assert is_branch_switch("checkout", ["-b", "new-feature"]) is True

    def test_checkout_dash_B(self):
        assert is_branch_switch("checkout", ["-B", "existing-branch"]) is True

    def test_checkout_orphan(self):
        assert is_branch_switch("checkout", ["--orphan", "fresh"]) is True

    def test_checkout_b_with_start_point(self):
        assert is_branch_switch("checkout", ["-b", "new", "origin/main"]) is True


class TestIsBranchSwitchCheckoutBranch:
    """checkout with a positional arg (branch name) is a branch switch."""

    def test_checkout_branch_name(self):
        assert is_branch_switch("checkout", ["main"]) is True

    def test_checkout_remote_branch(self):
        assert is_branch_switch("checkout", ["origin/main"]) is True

    def test_checkout_branch_with_double_dash_and_files(self):
        """checkout branch -- file is a branch switch (positional before --)."""
        assert is_branch_switch("checkout", ["main", "--", "file.txt"]) is True


class TestIsBranchSwitchCheckoutFiles:
    """File-targeting checkouts are NOT branch switches."""

    def test_checkout_file_with_double_dash(self):
        """checkout -- file.txt is a file checkout."""
        assert is_branch_switch("checkout", ["--", "file.txt"]) is False

    def test_checkout_multiple_files_with_double_dash(self):
        assert is_branch_switch("checkout", ["--", "a.py", "b.py"]) is False

    def test_checkout_with_patch_flag(self):
        """checkout -p is interactive patching, not a branch switch."""
        assert is_branch_switch("checkout", ["-p"]) is False

    def test_checkout_with_patch_long_flag(self):
        assert is_branch_switch("checkout", ["--patch"]) is False

    def test_checkout_no_args(self):
        """Bare checkout with no arguments."""
        assert is_branch_switch("checkout", []) is False

    def test_checkout_only_double_dash(self):
        """checkout -- with nothing after is a file checkout (empty pathspec)."""
        assert is_branch_switch("checkout", ["--"]) is False


class TestIsBranchSwitchEdgeCases:
    """Edge cases for the branch switch detection heuristic."""

    def test_checkout_head_double_dash_file(self):
        """checkout HEAD -- file.txt: HEAD is a positional before -- so it's a branch switch."""
        assert is_branch_switch("checkout", ["HEAD", "--", "file.txt"]) is True

    def test_checkout_unknown_flags_ignored(self):
        """Unknown flags are skipped, positional still detected."""
        assert is_branch_switch("checkout", ["--quiet", "main"]) is True

    def test_empty_args_list(self):
        assert is_branch_switch("checkout", []) is False
        assert is_branch_switch("switch", []) is True

    def test_patch_prevents_branch_detection(self):
        """Even with a positional-looking arg, -p means file operation."""
        assert is_branch_switch("checkout", ["-p", "main"]) is False
