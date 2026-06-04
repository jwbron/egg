"""Tests for egg_agent.tool_output_cap predictive PreToolUse caps (#2876)."""

import os
from unittest.mock import patch

import pytest
from egg_agent import tool_output_cap
from egg_agent.tool_output_cap import (
    check_builtin_tool_output_risk,
    check_grep_output_risk,
    check_read_output_risk,
    is_output_cap_disabled,
)


@pytest.fixture(autouse=True)
def _reset_cap_warning_cache():
    # The invalid-cap warning is now once-per-value (module-level cache), so
    # clear it between tests to keep the warn/no-warn assertions order-independent.
    tool_output_cap._warned_cap_values.clear()
    yield
    tool_output_cap._warned_cap_values.clear()


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
        # Per #2884, the cap is reframed as model-context/cost discipline
        # (not #2804 crash prevention — the SDK reader buffer handles that).
        assert "context budget" in reason

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

    @patch.dict(os.environ, {"EGG_READ_CAP_BYTES": "0"})
    @patch("egg_agent.tool_output_cap.logger")
    def test_zero_env_warns_and_falls_back_to_default(self, mock_logger, tmp_path):
        # 0 is non-positive → invalid; fall back to the 256 KiB default and warn.
        big = _write(tmp_path, "big.py", 300 * 1024)
        assert check_read_output_risk({"file_path": str(big)}, str(tmp_path)) is not None
        small = _write(tmp_path, "small.py", 2048)
        assert check_read_output_risk({"file_path": str(small)}, str(tmp_path)) is None
        mock_logger.warning.assert_called()

    @patch.dict(os.environ, {"EGG_READ_CAP_BYTES": "-5"})
    @patch("egg_agent.tool_output_cap.logger")
    def test_negative_env_warns_and_falls_back_to_default(self, mock_logger, tmp_path):
        small = _write(tmp_path, "small.py", 2048)
        assert check_read_output_risk({"file_path": str(small)}, str(tmp_path)) is None
        mock_logger.warning.assert_called()

    @patch.dict(os.environ, {"EGG_READ_CAP_BYTES": "2mb"})
    @patch("egg_agent.tool_output_cap.logger")
    def test_unparseable_env_warns(self, mock_logger, tmp_path):
        # set-but-unparseable must be loud, not silently swallowed (#2876 review).
        small = _write(tmp_path, "small.py", 2048)
        check_read_output_risk({"file_path": str(small)}, str(tmp_path))
        mock_logger.warning.assert_called()

    @patch.dict(os.environ, {"EGG_READ_CAP_BYTES": "0"})
    @patch("egg_agent.tool_output_cap.logger")
    def test_invalid_env_warns_only_once_across_reads(self, mock_logger, tmp_path):
        # A steady misconfiguration must not spam one warning per Read (#2876
        # re-review): resolve the cap several times, expect a single warning.
        small = _write(tmp_path, "small.py", 1024)
        for _ in range(5):
            check_read_output_risk({"file_path": str(small)}, str(tmp_path))
        assert mock_logger.warning.call_count == 1

    @patch("egg_agent.tool_output_cap.logger")
    def test_unset_env_does_not_warn(self, mock_logger, tmp_path):
        # The unset case uses the expected default — it must stay silent.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EGG_READ_CAP_BYTES", None)
            small = _write(tmp_path, "small.py", 1024)
            check_read_output_risk({"file_path": str(small)}, str(tmp_path))
        mock_logger.warning.assert_not_called()

    def test_denies_oversized_limit_on_large_file(self, tmp_path):
        # A huge limit still reads (nearly) the whole file → must be denied, not
        # waved through on the mere presence of a limit (#2876 review item 3).
        big = _write(tmp_path, "big.py", 300 * 1024)
        reason = check_read_output_risk({"file_path": str(big), "limit": 10_000_000}, str(tmp_path))
        assert reason is not None

    def test_allows_modest_limit_on_large_file(self, tmp_path):
        big = _write(tmp_path, "big.py", 300 * 1024)
        assert check_read_output_risk({"file_path": str(big), "limit": 100}, str(tmp_path)) is None

    def test_non_positive_limit_treated_as_unbounded(self, tmp_path):
        big = _write(tmp_path, "big.py", 300 * 1024)
        assert (
            check_read_output_risk({"file_path": str(big), "limit": 0}, str(tmp_path)) is not None
        )

    def test_pdf_deny_points_at_pages(self, tmp_path):
        # offset/limit are line-based; for a PDF the remedy is the 'pages' param.
        pdf = _write(tmp_path, "big.pdf", 300 * 1024)
        reason = check_read_output_risk({"file_path": str(pdf)}, str(tmp_path))
        assert reason is not None
        assert "pages" in reason

    def test_pdf_with_pages_is_allowed(self, tmp_path):
        # The deny remedy tells the agent to use 'pages'; a pages-scoped read
        # must then be honored, not denied again (#2876 re-review). The Read
        # tool caps a pages request at 20 pages, so it's inherently bounded.
        pdf = _write(tmp_path, "big.pdf", 300 * 1024)
        assert (
            check_read_output_risk({"file_path": str(pdf), "pages": "1-5"}, str(tmp_path)) is None
        )

    def test_pdf_with_empty_pages_still_denied(self, tmp_path):
        # An empty/whitespace 'pages' is not a real page range → still unbounded.
        pdf = _write(tmp_path, "big.pdf", 300 * 1024)
        assert (
            check_read_output_risk({"file_path": str(pdf), "pages": "  "}, str(tmp_path))
            is not None
        )

    def test_notebook_deny_suggests_jq(self, tmp_path):
        # Read returns a notebook whole; offset/limit/pages don't help, so the
        # remedy should point at jq cell inspection rather than file/stat.
        nb = _write(tmp_path, "big.ipynb", 300 * 1024)
        reason = check_read_output_risk({"file_path": str(nb)}, str(tmp_path))
        assert reason is not None
        assert "jq" in reason
        assert "offset" not in reason and "limit" not in reason

    def test_image_deny_does_not_suggest_line_paging(self, tmp_path):
        png = _write(tmp_path, "big.png", 300 * 1024)
        reason = check_read_output_risk({"file_path": str(png)}, str(tmp_path))
        assert reason is not None
        # offset/limit are meaningless for a binary read — must not be suggested.
        assert "offset" not in reason and "limit" not in reason
        assert "binary" in reason

    def test_binary_limit_does_not_bypass_cap(self, tmp_path):
        # Read returns a binary file whole, so a limit never bounds it.
        png = _write(tmp_path, "big.png", 300 * 1024)
        assert (
            check_read_output_risk({"file_path": str(png), "limit": 10}, str(tmp_path)) is not None
        )


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
    # Pin EGG_TOOL_OUTPUT_CAP for the cap-on dispatch cases so an ambient
    # env that disables the cap can't make them fail spuriously (#2876 review).
    @patch.dict(os.environ, {"EGG_TOOL_OUTPUT_CAP": ""}, clear=False)
    def test_dispatch_read(self, tmp_path):
        big = _write(tmp_path, "big.py", 300 * 1024)
        assert (
            check_builtin_tool_output_risk("Read", {"file_path": str(big)}, str(tmp_path))
            is not None
        )

    @patch.dict(os.environ, {"EGG_TOOL_OUTPUT_CAP": ""}, clear=False)
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
