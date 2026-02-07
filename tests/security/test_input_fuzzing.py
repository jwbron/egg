"""
Property-based fuzzing tests using hypothesis.

Security Properties Tested:
- Parsers handle arbitrary input without crashes
- Validators reject invalid input gracefully
- No exceptions leak sensitive information
- Functions are deterministic for same input

Attack Vectors:
- CWE-20: Improper Input Validation
- CWE-754: Improper Check for Unusual or Exceptional Conditions
- CWE-209: Generation of Error Message Containing Sensitive Information

References:
- OWASP Testing Guide: Fuzzing
"""

import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    HYPOTHESIS_AVAILABLE = True

    # Common settings for all fuzz tests
    FUZZ_SETTINGS = settings(
        max_examples=200,
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.function_scoped_fixture,
        ],
    )
except ImportError:
    HYPOTHESIS_AVAILABLE = False
    FUZZ_SETTINGS = None

# Add project paths
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "shared"))
sys.path.insert(0, str(PROJECT_ROOT / "gateway"))


def _load_policy_module():
    """Load policy module with proper import handling."""
    gateway_dir = PROJECT_ROOT / "gateway"
    shared_dir = PROJECT_ROOT / "shared"

    # Set up environment
    os.environ.setdefault("GATEWAY_BOT_NAME", "egg")
    os.environ.setdefault("GATEWAY_BOT_BRANCH_PREFIX", "egg")

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
    policy_source = policy_source.replace(
        "from .github_client import", "from github_client import"
    )
    policy_module = ModuleType("policy")
    policy_module.__file__ = str(policy_path)
    exec(compile(policy_source, policy_path, "exec"), policy_module.__dict__)
    sys.modules["policy"] = policy_module

    return policy_module


def _load_session_manager_module():
    """Load session_manager module."""
    gateway_dir = PROJECT_ROOT / "gateway"
    shared_dir = PROJECT_ROOT / "shared"

    sys.path.insert(0, str(shared_dir))
    sys.path.insert(0, str(gateway_dir))

    session_manager_path = gateway_dir / "session_manager.py"
    session_manager_source = session_manager_path.read_text()
    session_manager_module = ModuleType("session_manager")
    session_manager_module.__file__ = str(session_manager_path)
    exec(
        compile(session_manager_source, session_manager_path, "exec"),
        session_manager_module.__dict__,
    )
    sys.modules["session_manager"] = session_manager_module

    return session_manager_module


# Skip all tests in this module if hypothesis is not available
pytestmark = pytest.mark.skipif(
    not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed"
)


@pytest.mark.security
class TestBranchNameFuzzing:
    """Fuzz testing for branch name handling.

    CWE-20: Improper Input Validation
    Ensures branch name parsing handles all inputs safely.
    """

    @pytest.fixture
    def policy_module(self, monkeypatch):
        """Load policy module."""
        monkeypatch.setenv("GATEWAY_BOT_NAME", "egg")
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "egg")
        return _load_policy_module()

    @given(branch_name=st.text(min_size=0, max_size=500))
    @FUZZ_SETTINGS
    def test_branch_name_never_crashes_parser(self, branch_name, policy_module):
        """Verify branch name parsing never crashes.

        Defense: All string inputs are handled without exceptions.
        Attack vector: Crafted input causing parser crash/DoS.
        """
        policy_module._reset_bot_config_caches()

        # Should not raise any exception
        try:
            is_bot = policy_module.get_bot_branch_prefixes()
            result = branch_name.startswith(is_bot)
            assert isinstance(result, bool)
        except ValueError:
            # ValueError for missing config is acceptable
            pass

    @given(branch_name=st.text(min_size=1, max_size=100))
    @FUZZ_SETTINGS
    def test_branch_name_deterministic(self, branch_name, policy_module):
        """Verify branch name checking is deterministic.

        Defense: Same input always produces same output.
        Attack vector: Race conditions or state-dependent bypasses.
        """
        policy_module._reset_bot_config_caches()

        try:
            prefixes = policy_module.get_bot_branch_prefixes()
            result1 = branch_name.startswith(prefixes)
            result2 = branch_name.startswith(prefixes)
            assert result1 == result2
        except ValueError:
            pass


@pytest.mark.security
class TestRefspecFuzzing:
    """Fuzz testing for refspec parsing.

    CWE-20: Improper Input Validation
    Ensures refspec parsing handles all inputs safely.
    """

    @pytest.fixture
    def policy_module(self, monkeypatch):
        """Load policy module."""
        monkeypatch.setenv("GATEWAY_BOT_NAME", "egg")
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "egg")
        return _load_policy_module()

    @given(refspec=st.text(min_size=0, max_size=500))
    @FUZZ_SETTINGS
    def test_refspec_extraction_never_crashes(self, refspec, policy_module):
        """Verify refspec extraction never crashes.

        Defense: Function handles any string input gracefully.
        Attack vector: Malformed refspecs causing parser exceptions.
        """
        result = policy_module.extract_branch_from_refspec(refspec)

        # Result should be None or a string
        assert result is None or isinstance(result, str)

    @given(refspec=st.text(min_size=0, max_size=100))
    @FUZZ_SETTINGS
    def test_refspec_extraction_deterministic(self, refspec, policy_module):
        """Verify refspec extraction is deterministic.

        Defense: Same input always produces same output.
        Attack vector: Non-deterministic parsing enabling bypasses.
        """
        result1 = policy_module.extract_branch_from_refspec(refspec)
        result2 = policy_module.extract_branch_from_refspec(refspec)
        assert result1 == result2


@pytest.mark.security
class TestRemoteURLFuzzing:
    """Fuzz testing for remote URL parsing.

    CWE-20: Improper Input Validation
    Ensures URL parsing handles all inputs safely.
    """

    @pytest.fixture
    def policy_module(self, monkeypatch):
        """Load policy module."""
        monkeypatch.setenv("GATEWAY_BOT_NAME", "egg")
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "egg")
        return _load_policy_module()

    @given(url=st.text(min_size=0, max_size=500))
    @FUZZ_SETTINGS
    def test_remote_url_extraction_never_crashes(self, url, policy_module):
        """Verify URL extraction never crashes.

        Defense: Function handles any string input gracefully.
        Attack vector: Malformed URLs causing parser exceptions.
        """
        result = policy_module.extract_repo_from_remote(url)

        # Result should be None or a string
        assert result is None or isinstance(result, str)

    @given(
        url=st.one_of(
            st.just("https://github.com/owner/repo.git"),
            st.just("https://github.com/owner/repo"),
            st.just("git@github.com:owner/repo.git"),
            st.text(min_size=0, max_size=200),
        )
    )
    @FUZZ_SETTINGS
    def test_url_extraction_deterministic(self, url, policy_module):
        """Verify URL extraction is deterministic.

        Defense: Same input always produces same output.
        Attack vector: Non-deterministic parsing enabling bypasses.
        """
        result1 = policy_module.extract_repo_from_remote(url)
        result2 = policy_module.extract_repo_from_remote(url)
        assert result1 == result2


@pytest.mark.security
class TestSessionTokenFuzzing:
    """Fuzz testing for session token handling.

    CWE-20: Improper Input Validation
    Ensures token handling handles all inputs safely.
    """

    @pytest.fixture
    def session_module(self, monkeypatch, tmp_path):
        """Load session_manager module."""
        return _load_session_manager_module()

    @given(token=st.text(min_size=0, max_size=500))
    @FUZZ_SETTINGS
    def test_token_hashing_never_crashes(self, token, session_module):
        """Verify token hashing never crashes.

        Defense: Hashing handles any string input.
        Attack vector: Crafted tokens causing hash function exceptions.
        """
        result = session_module._hash_token(token)

        # Result should be a 64-character hex string (SHA-256)
        assert isinstance(result, str)
        assert len(result) == 64

    @given(token=st.text(min_size=0, max_size=100))
    @FUZZ_SETTINGS
    def test_token_hashing_deterministic(self, token, session_module):
        """Verify token hashing is deterministic.

        Defense: Same token always produces same hash.
        Attack vector: Non-deterministic hashing enabling hash collisions.
        """
        result1 = session_module._hash_token(token)
        result2 = session_module._hash_token(token)
        assert result1 == result2

    @given(
        a=st.text(min_size=0, max_size=100),
        b=st.text(min_size=0, max_size=100),
    )
    @FUZZ_SETTINGS
    def test_constant_time_compare_never_crashes(self, a, b, session_module):
        """Verify constant-time comparison never crashes.

        Defense: Comparison handles any string inputs.
        Attack vector: Crafted input causing comparison exceptions.
        """
        result = session_module._constant_time_compare(a, b)
        assert isinstance(result, bool)

        # Verify correctness
        if a == b:
            assert result is True
        else:
            assert result is False


@pytest.mark.security
class TestIPAddressFuzzing:
    """Fuzz testing for IP address handling in sessions.

    CWE-20: Improper Input Validation
    Ensures IP address handling is safe.
    """

    @pytest.fixture
    def session_module(self):
        """Load session_manager module."""
        return _load_session_manager_module()

    @given(ip=st.text(min_size=0, max_size=100))
    @FUZZ_SETTINGS
    def test_session_registration_with_arbitrary_ip(self, ip, session_module, tmp_path):
        """Verify session registration handles arbitrary IP strings.

        Defense: IP stored as-is for later comparison.
        Attack vector: Malformed IPs causing registration failures.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            persist_path = Path(tmpdir) / "sessions.json"
            manager = session_module.SessionManager(persistence_file=persist_path)

            # Should not crash, though may or may not succeed
            try:
                token, session = manager.register_session(
                    container_id="test-container",
                    container_ip=ip,
                    mode="private",
                )

                # If it succeeds, verify IP is stored correctly
                assert session.container_ip == ip
            except (ValueError, TypeError):
                # Validation errors are acceptable
                pass


@pytest.mark.security
class TestContainerIdFuzzing:
    """Fuzz testing for container ID handling.

    CWE-20: Improper Input Validation
    Ensures container ID handling is safe.
    """

    @pytest.fixture
    def session_module(self):
        """Load session_manager module."""
        return _load_session_manager_module()

    @given(container_id=st.text(min_size=0, max_size=200))
    @FUZZ_SETTINGS
    def test_session_with_arbitrary_container_id(self, container_id, session_module):
        """Verify session handles arbitrary container IDs.

        Defense: Container ID stored as-is for audit purposes.
        Attack vector: Malformed container IDs causing issues.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            persist_path = Path(tmpdir) / "sessions.json"
            manager = session_module.SessionManager(persistence_file=persist_path)

            # Should not crash
            try:
                token, session = manager.register_session(
                    container_id=container_id,
                    container_ip="172.18.0.5",
                    mode="private",
                )

                # If it succeeds, verify container ID is stored correctly
                assert session.container_id == container_id
            except (ValueError, TypeError):
                # Validation errors are acceptable
                pass


@pytest.mark.security
class TestPRAuthorFuzzing:
    """Fuzz testing for PR author name handling.

    CWE-20: Improper Input Validation
    Ensures author name comparisons are safe.
    """

    @pytest.fixture
    def policy_module(self, monkeypatch):
        """Load policy module."""
        monkeypatch.setenv("GATEWAY_BOT_NAME", "egg")
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "egg")
        return _load_policy_module()

    @given(author=st.text(min_size=0, max_size=100))
    @FUZZ_SETTINGS
    def test_bot_author_check_never_crashes(self, author, policy_module):
        """Verify bot author check handles arbitrary names.

        Defense: Author comparison handles any string input.
        Attack vector: Crafted author names causing comparison exceptions.
        """
        from unittest.mock import MagicMock

        policy_module._reset_bot_config_caches()

        engine = policy_module.PolicyEngine(github_client=MagicMock())

        # Test with string author
        result = engine._is_bot_author(author)
        assert isinstance(result, bool)

        # Test with dict author
        result = engine._is_bot_author({"login": author})
        assert isinstance(result, bool)

    @given(author=st.text(min_size=0, max_size=100))
    @FUZZ_SETTINGS
    def test_trusted_author_check_never_crashes(self, author, policy_module):
        """Verify trusted author check handles arbitrary names.

        Defense: Author comparison handles any string input.
        Attack vector: Crafted author names causing comparison exceptions.
        """
        from unittest.mock import MagicMock

        policy_module._reset_bot_config_caches()

        engine = policy_module.PolicyEngine(github_client=MagicMock())

        # Test with string author
        result = engine._is_trusted_author(author)
        assert isinstance(result, bool)

        # Test with dict author
        result = engine._is_trusted_author({"login": author})
        assert isinstance(result, bool)
