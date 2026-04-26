"""Gateway-side smoke test for the merged repo-config view (issue #2073).

NACK non-blocking from plan TASK-6-3: assert that
``config/repo_config.py`` (which the gateway and checkpoint handler
import) sees the merged view after a ``<repo>/.egg/repositories.yaml``
is dropped into a checkout.

This is a lightweight smoke test — it does not start the gateway
sidecar, just exercises the import-and-merge path that
``gateway/gateway.py`` and ``gateway/checkpoint_handler.py`` rely on.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).parent.parent
for _p in (_REPO_ROOT / "shared", _REPO_ROOT / "config"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@pytest.fixture(autouse=True)
def _drop_caches():
    from egg_config.repos import reload_config

    reload_config()
    yield
    reload_config()


def _make_checkout_with_repo_file(
    tmp_path: Path, name: str = "foo", repo_block: dict | None = None
) -> Path:
    checkout = tmp_path / name
    checkout.mkdir(parents=True, exist_ok=True)
    git = checkout / ".git"
    git.mkdir()
    (git / "config").write_text(
        textwrap.dedent(
            """
            [remote "origin"]
            \turl = https://github.com/alice/foo.git
            """
        ).strip()
        + "\n"
    )
    body = repo_block or {"schemaVersion": "1.0"}
    egg_file = checkout / ".egg" / "repositories.yaml"
    egg_file.parent.mkdir(parents=True, exist_ok=True)
    egg_file.write_text(yaml.safe_dump(body))
    return checkout


class TestGatewayMergedView:
    """Gateway-side consumers see the merged view via config/repo_config.py."""

    def test_get_repo_build_commands_reads_layered_view(self, tmp_path, monkeypatch):
        """`get_repo_build_commands` returns the layered (merged) block."""
        # Start in the checkout dir so config/repo_config.py's
        # _checkout_path heuristic finds the repo-defaults file.
        checkout = _make_checkout_with_repo_file(
            tmp_path,
            repo_block={
                "schemaVersion": "1.0",
                "build_commands": {"commands": ["echo from-repo-file"]},
                "watch_files": ["pyproject.toml"],
                "persist": ["/usr/local/bin", ".venv"],
            },
        )

        user_path = tmp_path / "user.yaml"
        user_path.write_text(
            yaml.safe_dump(
                {
                    "schemaVersion": "1.0",
                    "github_username": "alice",
                    "writable_repos": ["alice/foo"],
                    "repo_settings": {"alice/foo": {}},
                }
            )
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(user_path))
        monkeypatch.chdir(checkout)

        from config.repo_config import get_repo_build_commands

        block = get_repo_build_commands("alice/foo")
        assert block["commands"] == ["echo from-repo-file"]
        # Unified persist list surfaces.
        assert ".venv" in block["persist"]
        assert "/usr/local/bin" in block["persist"]
        # Classifier-derived two-list shape ALSO surfaces (manifest
        # writer in sandbox/egg_lib/docker.py consumes this).
        assert ".venv" in block["persist_dirs"]
        assert "/usr/local/bin" in block["persist_system_dirs"]

    def test_writable_repos_unchanged_after_layered_load(self, tmp_path, monkeypatch):
        """Operator-scoped fields keep coming from the user file."""
        checkout = _make_checkout_with_repo_file(tmp_path)
        user_path = tmp_path / "user.yaml"
        user_path.write_text(
            yaml.safe_dump(
                {
                    "schemaVersion": "1.0",
                    "github_username": "alice",
                    "writable_repos": ["alice/foo", "alice/bar"],
                    "readable_repos": ["alice/baz"],
                    "default_reviewer": "alice",
                }
            )
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(user_path))
        monkeypatch.chdir(checkout)

        from config.repo_config import (
            get_default_reviewer,
            get_github_username,
            get_readable_repos,
            get_writable_repos,
        )

        assert get_writable_repos() == ["alice/foo", "alice/bar"]
        assert get_readable_repos() == ["alice/baz"]
        assert get_github_username() == "alice"
        assert get_default_reviewer() == "alice"

    def test_checkpoint_repo_seen_through_layered_view(self, tmp_path, monkeypatch):
        """`get_all_checkpoint_repos` picks up the layered block's checkpoint_repo."""
        checkout = _make_checkout_with_repo_file(
            tmp_path,
            repo_block={
                "schemaVersion": "1.0",
                "checkpoint_repo": "alice/checkpoints",
            },
        )
        user_path = tmp_path / "user.yaml"
        user_path.write_text(
            yaml.safe_dump(
                {
                    "schemaVersion": "1.0",
                    "github_username": "alice",
                    "repo_settings": {"alice/foo": {}},
                }
            )
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(user_path))
        monkeypatch.chdir(checkout)

        from config.repo_config import get_all_checkpoint_repos, reload_config

        # Drop any pre-existing cache from a prior test.
        reload_config()
        repos = get_all_checkpoint_repos()
        assert "alice/checkpoints" in repos

    def test_repo_file_absent_falls_back_to_user_only(self, tmp_path, monkeypatch):
        """If no repo-defaults file is found, behaviour matches pre-#2073."""
        # Plain checkout without a .egg/repositories.yaml.
        checkout = tmp_path / "plain"
        checkout.mkdir()
        user_path = tmp_path / "user.yaml"
        user_path.write_text(
            yaml.safe_dump(
                {
                    "schemaVersion": "1.0",
                    "github_username": "alice",
                    "writable_repos": ["alice/foo"],
                    "repo_settings": {
                        "alice/foo": {
                            "build_commands": {
                                "commands": ["echo legacy"],
                                "watch_files": ["go.mod"],
                            },
                            "persist": ["node_modules"],
                        }
                    },
                }
            )
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(user_path))
        monkeypatch.chdir(checkout)

        from config.repo_config import get_repo_build_commands, reload_config

        reload_config()
        block = get_repo_build_commands("alice/foo")
        assert block["commands"] == ["echo legacy"]
        # User-only persist surfaces under the new key.
        assert "node_modules" in block["persist"]
        assert "node_modules" in block["persist_dirs"]
