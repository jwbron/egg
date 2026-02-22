"""Tests for checkpoint CLI HTTP mode (gateway direct)."""

import argparse
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from egg_contracts.checkpoint_cli import (
    _add_checkpoint_resolution_params,
    _build_list_params,
    _get_gateway_url,
    _get_source_repo,
    _http_get,
    cmd_cost,
    cmd_list,
    cmd_show,
)


class TestGetGatewayUrl:
    """Tests for _get_gateway_url() detection logic."""

    def test_returns_url_when_both_set(self):
        with patch.dict(
            "os.environ", {"GATEWAY_URL": "http://gw:9848", "EGG_SESSION_TOKEN": "tok"}
        ):
            assert _get_gateway_url() == "http://gw:9848"

    def test_returns_none_without_token(self):
        with patch.dict("os.environ", {"GATEWAY_URL": "http://gw:9848"}, clear=True):
            assert _get_gateway_url() is None

    def test_returns_none_without_url(self):
        with patch.dict("os.environ", {"EGG_SESSION_TOKEN": "tok"}, clear=True):
            assert _get_gateway_url() is None

    def test_returns_none_when_neither_set(self):
        with patch.dict("os.environ", {}, clear=True):
            assert _get_gateway_url() is None


class TestHttpGet:
    """Tests for _http_get() request construction."""

    @patch("urllib.request.urlopen")
    def test_sends_auth_header(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        with patch.dict("os.environ", {"EGG_SESSION_TOKEN": "my-token"}):
            result = _http_get("http://gw:9848", "/api/v1/checkpoints")

        assert result == {"success": True}
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.get_header("Authorization") == "Bearer my-token"
        assert req.get_header("Accept") == "application/json"

    @patch("urllib.request.urlopen")
    def test_appends_query_params(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        with patch.dict("os.environ", {"EGG_SESSION_TOKEN": "tok"}):
            _http_get("http://gw:9848", "/api/v1/checkpoints", {"issue": 738})

        req = mock_urlopen.call_args[0][0]
        assert "issue=738" in req.full_url

    @patch("urllib.request.urlopen")
    def test_filters_none_params(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        with patch.dict("os.environ", {"EGG_SESSION_TOKEN": "tok"}):
            _http_get("http://gw:9848", "/ep", {"a": 1, "b": None})

        req = mock_urlopen.call_args[0][0]
        assert "a=1" in req.full_url
        assert "b=" not in req.full_url

    @patch("urllib.request.urlopen")
    def test_http_error_raises_runtime(self, mock_urlopen):
        from urllib.error import HTTPError

        err = HTTPError("http://gw:9848/ep", 500, "err", {}, None)
        mock_urlopen.side_effect = err

        with patch.dict("os.environ", {"EGG_SESSION_TOKEN": "tok"}):
            with pytest.raises(RuntimeError, match="Gateway request failed"):
                _http_get("http://gw:9848", "/ep")

    @patch("urllib.request.urlopen")
    def test_url_error_raises_runtime(self, mock_urlopen):
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("Connection refused")

        with patch.dict("os.environ", {"EGG_SESSION_TOKEN": "tok"}):
            with pytest.raises(RuntimeError, match="Cannot connect to gateway"):
                _http_get("http://gw:9848", "/ep")


class TestBuildListParams:
    """Tests for _build_list_params() parameter extraction."""

    def test_basic_params(self):
        args = argparse.Namespace(
            limit=50,
            issue=738,
            pr=None,
            branch=None,
            session=None,
            trigger=None,
            status=None,
            agent_type=None,
            phase=None,
            pipeline=None,
            repo=None,
            repo_path="/repo",
        )
        params = _build_list_params(args)
        assert params["limit"] == 50
        assert params["issue"] == 738
        assert params["repo_path"] == "/repo"
        assert "pr" not in params

    def test_all_filters(self):
        args = argparse.Namespace(
            limit=10,
            issue=1,
            pr=2,
            branch="main",
            session="s1",
            trigger="COMMIT",
            status="completed",
            agent_type="coder",
            phase="implement",
            pipeline="p1",
            repo="org/repo",
            repo_path="/repo",
        )
        params = _build_list_params(args)
        assert params["pr"] == 2
        assert params["branch"] == "main"
        assert params["agent_type"] == "coder"
        assert params["pipeline"] == "p1"


class TestBuildListParamsCheckpointRepo:
    """Tests that _build_list_params passes checkpoint_repo to HTTP."""

    @patch("egg_contracts.checkpoint_cli._get_checkpoint_repo_from_args")
    def test_includes_checkpoint_repo_when_set(self, mock_get_ckpt):
        mock_get_ckpt.return_value = "org/checkpoints"
        args = argparse.Namespace(
            limit=50,
            issue=835,
            pr=None,
            branch=None,
            session=None,
            trigger=None,
            status=None,
            agent_type=None,
            phase=None,
            pipeline=None,
            repo=None,
            repo_path="/repo",
            checkpoint_repo="org/checkpoints",
        )
        params = _build_list_params(args)
        assert params["checkpoint_repo"] == "org/checkpoints"

    @patch("egg_contracts.checkpoint_cli._get_checkpoint_repo_from_args")
    def test_omits_checkpoint_repo_when_none(self, mock_get_ckpt):
        mock_get_ckpt.return_value = None
        args = argparse.Namespace(
            limit=50,
            issue=835,
            pr=None,
            branch=None,
            session=None,
            trigger=None,
            status=None,
            agent_type=None,
            phase=None,
            pipeline=None,
            repo=None,
            repo_path="/repo",
            checkpoint_repo=None,
        )
        params = _build_list_params(args)
        assert "checkpoint_repo" not in params


class TestCmdListHttpFallback:
    """Tests that cmd_list falls back to git on RuntimeError."""

    @patch("egg_contracts.checkpoint_cli._get_gateway_url")
    @patch("egg_contracts.checkpoint_cli._cmd_list_http")
    def test_uses_http_when_available(self, mock_http, mock_url):
        mock_url.return_value = "http://gw:9848"
        mock_http.return_value = 0

        args = argparse.Namespace(
            limit=50,
            issue=None,
            pr=None,
            branch=None,
            session=None,
            trigger=None,
            status=None,
            agent_type=None,
            phase=None,
            pipeline=None,
            repo=None,
            repo_path="/repo",
            json=False,
            checkpoint_repo=None,
        )
        result = cmd_list(args)
        assert result == 0
        mock_http.assert_called_once()

    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    @patch("egg_contracts.checkpoint_cli._get_gateway_url")
    @patch("egg_contracts.checkpoint_cli._cmd_list_http")
    def test_falls_back_on_runtime_error(self, mock_http, mock_url, mock_ref):
        mock_url.return_value = "http://gw:9848"
        mock_http.side_effect = RuntimeError("Connection refused")
        mock_ref.return_value = None

        args = argparse.Namespace(
            limit=50,
            issue=None,
            pr=None,
            branch=None,
            session=None,
            trigger=None,
            status=None,
            agent_type=None,
            phase=None,
            pipeline=None,
            repo=None,
            repo_path="/repo",
            json=False,
            checkpoint_repo=None,
        )
        result = cmd_list(args)
        # Should fall through to git path (which returns 0 for no ref)
        assert result == 0
        mock_ref.assert_called_once()

    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    @patch("egg_contracts.checkpoint_cli._get_gateway_url")
    def test_skips_http_when_no_gateway(self, mock_url, mock_ref):
        mock_url.return_value = None
        mock_ref.return_value = None

        args = argparse.Namespace(
            limit=50,
            issue=None,
            pr=None,
            branch=None,
            session=None,
            trigger=None,
            status=None,
            agent_type=None,
            phase=None,
            pipeline=None,
            repo=None,
            repo_path="/repo",
            json=False,
            checkpoint_repo=None,
        )
        result = cmd_list(args)
        assert result == 0
        # Should go straight to git path
        mock_ref.assert_called_once()


class TestCmdShowHttpFallback:
    """Tests that cmd_show falls back to git on RuntimeError."""

    @patch("egg_contracts.checkpoint_cli._get_gateway_url")
    @patch("egg_contracts.checkpoint_cli._cmd_show_http")
    def test_uses_http_when_available(self, mock_http, mock_url):
        mock_url.return_value = "http://gw:9848"
        mock_http.return_value = 0

        args = argparse.Namespace(
            identifier="ckpt-abc12345",
            repo_path="/repo",
            json=False,
            checkpoint_repo=None,
        )
        result = cmd_show(args)
        assert result == 0
        mock_http.assert_called_once()


class TestCmdCostHttpFallback:
    """Tests that cmd_cost falls back to git on RuntimeError."""

    @patch("egg_contracts.checkpoint_cli._get_gateway_url")
    @patch("egg_contracts.checkpoint_cli._cmd_cost_http")
    def test_uses_http_when_available(self, mock_http, mock_url):
        mock_url.return_value = "http://gw:9848"
        mock_http.return_value = 0

        args = argparse.Namespace(
            limit=50,
            issue=None,
            pr=None,
            pipeline=None,
            repo=None,
            repo_path="/repo",
            json=False,
            checkpoint_repo=None,
        )
        result = cmd_cost(args)
        assert result == 0
        mock_http.assert_called_once()


class TestGetSourceRepo:
    """Tests for _get_source_repo() helper."""

    @patch("egg_contracts.checkpoint_cli.run_git")
    def test_extracts_from_https_remote(self, mock_git):
        mock_git.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/jwbron/egg.git\n", stderr=""
        )
        assert _get_source_repo("/repo") == "jwbron/egg"

    @patch("egg_contracts.checkpoint_cli.run_git")
    def test_extracts_from_ssh_remote(self, mock_git):
        mock_git.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="git@github.com:jwbron/egg.git\n", stderr=""
        )
        assert _get_source_repo("/repo") == "jwbron/egg"

    @patch("egg_contracts.checkpoint_cli.run_git")
    def test_returns_none_when_git_fails(self, mock_git):
        mock_git.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="fatal: not a git repo"
        )
        assert _get_source_repo("/repo") is None

    @patch("egg_contracts.checkpoint_cli.run_git")
    def test_returns_none_for_non_github_url(self, mock_git):
        mock_git.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://gitlab.com/org/repo.git\n", stderr=""
        )
        assert _get_source_repo("/repo") is None

    @patch("egg_contracts.checkpoint_cli.run_git", side_effect=Exception("timeout"))
    def test_returns_none_on_exception(self, mock_git):
        assert _get_source_repo("/repo") is None


class TestAddCheckpointResolutionParams:
    """Tests for _add_checkpoint_resolution_params() source_repo fallback."""

    @patch("egg_contracts.checkpoint_cli._get_checkpoint_repo_from_args")
    def test_passes_checkpoint_repo_when_available(self, mock_get_ckpt):
        mock_get_ckpt.return_value = "org/checkpoints"
        args = argparse.Namespace(repo_path="/repo", checkpoint_repo="org/checkpoints")
        params: dict = {"repo_path": "/repo"}
        _add_checkpoint_resolution_params(params, args)
        assert params["checkpoint_repo"] == "org/checkpoints"
        assert "source_repo" not in params

    @patch("egg_contracts.checkpoint_cli._get_source_repo")
    @patch("egg_contracts.checkpoint_cli._get_checkpoint_repo_from_args")
    def test_passes_source_repo_when_checkpoint_repo_unavailable(
        self, mock_get_ckpt, mock_get_src
    ):
        mock_get_ckpt.return_value = None
        mock_get_src.return_value = "jwbron/egg"
        args = argparse.Namespace(repo_path="/repo", checkpoint_repo=None)
        params: dict = {"repo_path": "/repo"}
        _add_checkpoint_resolution_params(params, args)
        assert "checkpoint_repo" not in params
        assert params["source_repo"] == "jwbron/egg"

    @patch("egg_contracts.checkpoint_cli._get_source_repo")
    @patch("egg_contracts.checkpoint_cli._get_checkpoint_repo_from_args")
    def test_passes_neither_when_both_unavailable(self, mock_get_ckpt, mock_get_src):
        mock_get_ckpt.return_value = None
        mock_get_src.return_value = None
        args = argparse.Namespace(repo_path="/repo", checkpoint_repo=None)
        params: dict = {"repo_path": "/repo"}
        _add_checkpoint_resolution_params(params, args)
        assert "checkpoint_repo" not in params
        assert "source_repo" not in params


class TestBuildListParamsSourceRepo:
    """Tests that _build_list_params includes source_repo for gateway resolution."""

    @patch("egg_contracts.checkpoint_cli._get_source_repo")
    @patch("egg_contracts.checkpoint_cli._get_checkpoint_repo_from_args")
    def test_includes_source_repo_when_checkpoint_repo_unavailable(
        self, mock_get_ckpt, mock_get_src
    ):
        mock_get_ckpt.return_value = None
        mock_get_src.return_value = "jwbron/egg"
        args = argparse.Namespace(
            limit=50,
            issue=None,
            pr=None,
            branch=None,
            session=None,
            trigger=None,
            status=None,
            agent_type=None,
            phase=None,
            pipeline=None,
            repo=None,
            repo_path="/repo",
            checkpoint_repo=None,
        )
        params = _build_list_params(args)
        assert "checkpoint_repo" not in params
        assert params["source_repo"] == "jwbron/egg"
