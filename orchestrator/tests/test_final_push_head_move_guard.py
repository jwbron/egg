"""Orchestrator-side unit tests for ``_verify_pr_head_unchanged``.

These tests complement the integration-level coverage in
``integration_tests/test_babysit_pr/test_escalation.py`` (class
``TestFinalPushHeadMoveGuard``) by focusing on fine-grained edge cases:
exact subprocess invocation shape, exception swallowing, attribute-absence
behaviour, and whitespace-stripping in the rev-parse output.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from routes.pipelines import _verify_pr_head_unchanged  # noqa: E402


def _pipe(**overrides):
    """Build a lightweight pipeline stand-in with sensible defaults."""
    defaults = {
        "id": "pr-42",
        "pr_head_sha": "abc1234deadbeef",
        "branch": "feature-x",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestHeadUnchangedAllowsPush:
    """When the remote head still matches the stored SHA, push is allowed."""

    @patch("routes.pipelines.subprocess.run")
    def test_short_sha_matches_identically(self, mock_run):
        pipeline = _pipe(pr_head_sha="abc1234deadbeef", branch="feature-x")

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="abc1234deadbeef", stderr=""),
        ]

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual == "abc1234deadbeef"

    @patch("routes.pipelines.subprocess.run")
    def test_full_length_sha_matches(self, mock_run):
        full_sha = "a" * 40
        pipeline = _pipe(pr_head_sha=full_sha, branch="feature-x")

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout=full_sha, stderr=""),
        ]

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual == full_sha


class TestHeadMovedSignalsAbort:
    """When the remote has advanced the helper returns (False, actual_sha)."""

    @patch("routes.pipelines.subprocess.run")
    def test_completely_different_sha(self, mock_run):
        pipeline = _pipe(pr_head_sha="abc1234deadbeef")

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="def5678cafebabe\n", stderr=""),
        ]

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is False
        assert actual == "def5678cafebabe"

    @patch("routes.pipelines.subprocess.run")
    def test_one_character_difference_aborts(self, mock_run):
        # Last char differs: ...ef vs ...ee
        pipeline = _pipe(pr_head_sha="abc1234deadbeef")

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="abc1234deadbeee\n", stderr=""),
        ]

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is False
        assert actual == "abc1234deadbeee"


class TestStoredShaMissing:
    """Missing stored SHA short-circuits — subprocess is never invoked."""

    @patch("routes.pipelines.subprocess.run")
    def test_pr_head_sha_is_none(self, mock_run):
        pipeline = _pipe(pr_head_sha=None, branch="feature-x")

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual is None
        mock_run.assert_not_called()

    @patch("routes.pipelines.subprocess.run")
    def test_pr_head_sha_is_empty_string(self, mock_run):
        pipeline = _pipe(pr_head_sha="", branch="feature-x")

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual is None
        mock_run.assert_not_called()

    @patch("routes.pipelines.subprocess.run")
    def test_pr_head_sha_attribute_absent(self, mock_run):
        # SimpleNamespace without pr_head_sha at all -> getattr default None
        pipeline = SimpleNamespace(id="pr-42", branch="feature-x")

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual is None
        mock_run.assert_not_called()


class TestBranchMissing:
    """Missing branch short-circuits — subprocess is never invoked."""

    @patch("routes.pipelines.subprocess.run")
    def test_branch_is_none(self, mock_run):
        pipeline = _pipe(pr_head_sha="abc1234", branch=None)

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual is None
        mock_run.assert_not_called()

    @patch("routes.pipelines.subprocess.run")
    def test_branch_is_empty_string(self, mock_run):
        pipeline = _pipe(pr_head_sha="abc1234", branch="")

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual is None
        mock_run.assert_not_called()

    @patch("routes.pipelines.subprocess.run")
    def test_branch_attribute_absent(self, mock_run):
        pipeline = SimpleNamespace(id="pr-42", pr_head_sha="abc1234")

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual is None
        mock_run.assert_not_called()


class TestRevParseFailure:
    """A non-zero rev-parse or empty stdout degrades to (True, None)."""

    @patch("routes.pipelines.subprocess.run")
    def test_rev_parse_returncode_128(self, mock_run):
        pipeline = _pipe()

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=128, stdout="", stderr="fatal: bad revision"),
        ]

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual is None

    @patch("routes.pipelines.subprocess.run")
    def test_rev_parse_returncode_1_with_stderr(self, mock_run):
        pipeline = _pipe()

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="some stderr content"),
        ]

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual is None

    @patch("routes.pipelines.subprocess.run")
    def test_rev_parse_empty_stdout(self, mock_run):
        pipeline = _pipe()

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual is None

    @patch("routes.pipelines.subprocess.run")
    def test_rev_parse_whitespace_only_stdout(self, mock_run):
        pipeline = _pipe()

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="   \n", stderr=""),
        ]

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual is None


class TestGitRaisesException:
    """subprocess.run raising any exception is swallowed -> (True, None)."""

    @patch("routes.pipelines.subprocess.run")
    def test_timeout_expired_is_swallowed(self, mock_run):
        pipeline = _pipe()
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual is None

    @patch("routes.pipelines.subprocess.run")
    def test_os_error_is_swallowed(self, mock_run):
        pipeline = _pipe()
        mock_run.side_effect = OSError("git not found")

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual is None

    @patch("routes.pipelines.subprocess.run")
    def test_generic_exception_is_swallowed(self, mock_run):
        pipeline = _pipe()
        mock_run.side_effect = Exception("boom")

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual is None


class TestFetchAndRevParseInvocations:
    """The subprocess command shape is contractually important."""

    @patch("routes.pipelines.subprocess.run")
    def test_fetch_call_shape(self, mock_run):
        pipeline = _pipe(branch="feature-x", pr_head_sha="abc1234")
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="abc1234", stderr=""),
        ]

        _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))

        fetch_call = mock_run.call_args_list[0]
        args, kwargs = fetch_call
        assert args[0] == ["git", "-C", "/tmp/repo", "fetch", "origin", "feature-x"]
        assert kwargs["timeout"] == 30
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False

    @patch("routes.pipelines.subprocess.run")
    def test_rev_parse_call_shape(self, mock_run):
        pipeline = _pipe(branch="feature-x", pr_head_sha="abc1234")
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="abc1234", stderr=""),
        ]

        _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))

        rev_call = mock_run.call_args_list[1]
        args, kwargs = rev_call
        assert args[0] == ["git", "-C", "/tmp/repo", "rev-parse", "origin/feature-x"]
        assert kwargs["timeout"] == 10
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False

    @patch("routes.pipelines.subprocess.run")
    def test_worktree_path_stringified(self, mock_run):
        """A pathlib.Path must be converted to str in the argv."""
        pipeline = _pipe(branch="feature-x", pr_head_sha="abc1234")
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="abc1234", stderr=""),
        ]

        path = Path("/some/weird/worktree/path")
        _verify_pr_head_unchanged(pipeline, path)

        for call in mock_run.call_args_list:
            argv = call.args[0]
            # Third element is the argument to -C
            assert argv[2] == str(path)
            assert isinstance(argv[2], str)

    @patch("routes.pipelines.subprocess.run")
    def test_both_calls_made_in_happy_path(self, mock_run):
        pipeline = _pipe(branch="b", pr_head_sha="s")
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="s", stderr=""),
        ]

        _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert mock_run.call_count == 2


class TestStrippedShaComparison:
    """Stored SHA is raw; remote output must be stripped before comparing."""

    @patch("routes.pipelines.subprocess.run")
    def test_trailing_newline_is_stripped(self, mock_run):
        pipeline = _pipe(pr_head_sha="abc1234deadbeef")
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="abc1234deadbeef\n", stderr=""),
        ]

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual == "abc1234deadbeef"

    @patch("routes.pipelines.subprocess.run")
    def test_surrounding_whitespace_is_stripped(self, mock_run):
        pipeline = _pipe(pr_head_sha="abc1234deadbeef")
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="  abc1234deadbeef  \n", stderr=""),
        ]

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual == "abc1234deadbeef"
