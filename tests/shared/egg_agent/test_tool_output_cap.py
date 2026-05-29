"""Tests for egg_agent.tool_output_cap predictive PreToolUse caps (#2876)."""

import os
from unittest.mock import patch

from egg_agent.tool_output_cap import (
    check_builtin_tool_output_risk,
    check_grep_output_risk,
    check_read_output_risk,
    is_output_cap_disabled,
)


def _write(tmp_path, name, size):
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return p


class TestReadCap:
    def test_denies_unbounded_read_of_large_file(self, tmp_path):
        big = _write(tmp_path, "big.py", 300 * 1024)
        reason = check_read_output_risk({"file_path": str(big)}, str(tmp_path))
        assert reason is not None
        # Reason must tell the agent how to narrow the call.
        assert "offset" in reason and "limit" in reason
        assert "#2804" in reason

    def test_allows_large_file_when_limit_given(self, tmp_path):
        big = _write(tmp_path, "big.py", 300 * 1024)
        reason = check_read_output_risk({"file_path": str(big), "limit": 2000}, str(tmp_path))
        assert reason is None

    def test_allows_small_file(self, tmp_path):
        small = _write(tmp_path, "small.py", 1024)
        assert check_read_output_risk({"file_path": str(small)}, str(tmp_path)) is None

    def test_allows_missing_file(self, tmp_path):
        # Let the real Read tool surface the "file not found" error.
        assert check_read_output_risk({"file_path": "does-not-exist.py"}, str(tmp_path)) is None

    def test_resolves_relative_path_against_cwd(self, tmp_path):
        _write(tmp_path, "rel.py", 300 * 1024)
        reason = check_read_output_risk({"file_path": "rel.py"}, str(tmp_path))
        assert reason is not None

    def test_no_file_path_allowed(self, tmp_path):
        assert check_read_output_risk({}, str(tmp_path)) is None

    @patch.dict(os.environ, {"EGG_READ_CAP_BYTES": "1024"})
    def test_threshold_configurable_via_env(self, tmp_path):
        mid = _write(tmp_path, "mid.py", 2048)
        assert check_read_output_risk({"file_path": str(mid)}, str(tmp_path)) is not None

    @patch.dict(os.environ, {"EGG_READ_CAP_BYTES": "not-a-number"})
    def test_invalid_env_falls_back_to_default(self, tmp_path):
        # 2 KB is under the 256 KiB default, so it is allowed despite bad env.
        mid = _write(tmp_path, "mid.py", 2048)
        assert check_read_output_risk({"file_path": str(mid)}, str(tmp_path)) is None


class TestGrepCap:
    def test_denies_unbounded_content_grep(self):
        reason = check_grep_output_risk({"pattern": "foo", "output_mode": "content"})
        assert reason is not None
        assert "head_limit" in reason
        assert "files_with_matches" in reason

    def test_allows_content_grep_with_head_limit(self):
        assert (
            check_grep_output_risk({"pattern": "foo", "output_mode": "content", "head_limit": 50})
            is None
        )

    def test_allows_content_grep_scoped_by_path(self):
        assert (
            check_grep_output_risk(
                {"pattern": "foo", "output_mode": "content", "path": "orchestrator/"}
            )
            is None
        )

    def test_allows_content_grep_scoped_by_glob(self):
        assert (
            check_grep_output_risk({"pattern": "foo", "output_mode": "content", "glob": "*.py"})
            is None
        )

    def test_allows_files_with_matches_mode(self):
        assert (
            check_grep_output_risk({"pattern": "foo", "output_mode": "files_with_matches"}) is None
        )

    def test_allows_default_mode(self):
        # No output_mode → files_with_matches default → bounded.
        assert check_grep_output_risk({"pattern": "foo"}) is None


class TestDispatchAndKillSwitch:
    def test_dispatch_read(self, tmp_path):
        big = _write(tmp_path, "big.py", 300 * 1024)
        assert (
            check_builtin_tool_output_risk("Read", {"file_path": str(big)}, str(tmp_path))
            is not None
        )

    def test_dispatch_grep(self):
        assert (
            check_builtin_tool_output_risk("Grep", {"pattern": "x", "output_mode": "content"}, None)
            is not None
        )

    def test_dispatch_other_tool_allowed(self, tmp_path):
        assert check_builtin_tool_output_risk("Edit", {"file_path": "x"}, str(tmp_path)) is None

    @patch.dict(os.environ, {"EGG_TOOL_OUTPUT_CAP": "false"})
    def test_kill_switch_disables_dispatch(self, tmp_path):
        big = _write(tmp_path, "big.py", 300 * 1024)
        assert is_output_cap_disabled() is True
        assert (
            check_builtin_tool_output_risk("Read", {"file_path": str(big)}, str(tmp_path)) is None
        )
