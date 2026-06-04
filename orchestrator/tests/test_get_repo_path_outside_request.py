"""Tests for routes.get_repo_path outside Flask request context (#2903).

The context-PR opener runs from the ``_run_pipeline`` driver thread,
not an HTTP handler.  Before #2903 it failed with
``RuntimeError: Working outside of request context`` because
``get_repo_path`` unconditionally accessed Flask's ``request`` proxy.

The fix: detect request context availability up front and fall
through to EGG_REPO_PATH / CWD when no request is active.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

# Mock docker before importing routes (which transitively pulls in modules
# that touch docker at import time).
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from routes import get_repo_path  # noqa: E402


def _make_git_repo(path: Path) -> None:
    (path / ".git").mkdir(parents=True, exist_ok=True)


class TestGetRepoPathOutsideRequestContext:
    """get_repo_path must work outside a Flask HTTP request (#2903)."""

    def test_returns_env_path_when_no_request_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """EGG_REPO_PATH is used when no request is active."""
        _make_git_repo(tmp_path)
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))

        result = get_repo_path()
        assert result == tmp_path

    def test_returns_cwd_when_no_request_context_and_no_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falls back to CWD when neither request nor EGG_REPO_PATH is set."""
        monkeypatch.delenv("EGG_REPO_PATH", raising=False)
        monkeypatch.chdir(tmp_path)

        result = get_repo_path()
        assert result == tmp_path

    def test_returns_multi_repo_base_when_no_request_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multi-repo base (parent dir, not a git repo itself) is returned
        without error when no request context is available.  The caller
        (get_state_store_for_pipeline) handles repo discovery."""
        # tmp_path is NOT a git repo — simulates multi-repo base
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))

        # Must not raise RuntimeError about missing request context
        result = get_repo_path()
        assert result == tmp_path


class TestGetRepoPathInsideRequestContext:
    """Sanity check that request-based resolution still works."""

    def test_uses_request_repo_path_arg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """repo_path query arg takes precedence over EGG_REPO_PATH."""
        from flask import Flask

        app = Flask(__name__)

        with app.test_request_context(f"/?repo_path={tmp_path}"):
            result = get_repo_path()
            assert result == tmp_path

    def test_uses_request_json_body(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """repo_path in JSON body is used when no query arg."""
        from flask import Flask

        app = Flask(__name__)

        with app.test_request_context(
            "/",
            json={"repo_path": str(tmp_path)},
        ):
            result = get_repo_path()
            assert result == tmp_path

    def test_falls_back_to_env_when_request_has_no_repo_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty request → EGG_REPO_PATH fallback still fires."""
        _make_git_repo(tmp_path)
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))

        from flask import Flask

        app = Flask(__name__)

        with app.test_request_context("/"):
            result = get_repo_path()
            assert result == tmp_path

    def test_multi_repo_resolves_via_request_repo_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inside a request, when EGG_REPO_PATH points at a parent dir
        (not itself a git repo), the ``repo`` field from the JSON body
        selects the correct subdirectory.  Guards the
        ``has_request_context``-gated multi-repo branch at
        ``routes/__init__.py:80``."""
        # tmp_path is the multi-repo parent — NOT a git repo itself.
        repo_dir = tmp_path / "egg"
        _make_git_repo(repo_dir)
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))

        from flask import Flask

        app = Flask(__name__)

        with app.test_request_context(
            "/",
            json={"repo": "owner/egg"},
        ):
            result = get_repo_path()
            assert result == repo_dir
