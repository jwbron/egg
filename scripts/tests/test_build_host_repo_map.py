"""Tests for ``scripts/build-host-repo-map.py``.

The orchestrator consumes ``EGG_HOST_REPO_MAP`` (owner/repo → host_path)
to build hostPath mounts for spawned agent pods. Before this script
existed that map was hand-maintained in the k8s overlay, hardcoding
specific owner/repo pairs to one developer's filesystem (#1986). These
tests cover parsing every remote-URL shape that can appear in
``~/.config/egg/repositories.yaml`` and the edge cases the builder has
to survive (missing config, missing dirs, broken remotes).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_BUILDER_PATH = Path(__file__).resolve().parent.parent / "build-host-repo-map.py"
_spec = importlib.util.spec_from_file_location("build_host_repo_map", _BUILDER_PATH)
assert _spec and _spec.loader
build_host_repo_map = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_host_repo_map)


class TestParseOwnerRepo:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("git@github.com:Khan/webapp.git", "Khan/webapp"),
            ("git@github.com:Khan/webapp", "Khan/webapp"),
            ("git@gitlab.internal:team/repo.git", "team/repo"),
            ("ssh://git@github.com/Khan/webapp.git", "Khan/webapp"),
            ("ssh://git@github.com:22/Khan/webapp.git", "Khan/webapp"),
            ("https://github.com/Khan/webapp.git", "Khan/webapp"),
            ("https://github.com/Khan/webapp", "Khan/webapp"),
            ("http://example.com/o/r.git", "o/r"),
            ("https://github.com/Khan/webapp/", "Khan/webapp"),
            ("  git@github.com:Khan/webapp.git\n", "Khan/webapp"),
        ],
    )
    def test_parses_common_forms(self, url, expected):
        assert build_host_repo_map.parse_owner_repo(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "not-a-url",
            "file:///local/path.git",
            "https://github.com/only-one-segment",
        ],
    )
    def test_unparseable_returns_none(self, url):
        assert build_host_repo_map.parse_owner_repo(url) is None


def _init_repo_with_remote(path: Path, remote_url: str) -> None:
    """Initialize a bare-bones git repo with an origin remote for testing."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote_url], check=True)


class TestBuildMap:
    def test_builds_map_from_configured_paths(self, tmp_path):
        repo_a = tmp_path / "webapp"
        repo_b = tmp_path / "egg"
        _init_repo_with_remote(repo_a, "git@github.com:Khan/webapp.git")
        _init_repo_with_remote(repo_b, "ssh://git@github.com/owner/egg.git")

        config_path = tmp_path / "repositories.yaml"
        config_path.write_text(f"local_repos:\n  paths:\n  - {repo_a}\n  - {repo_b}\n")

        result = build_host_repo_map.build_map(config_path)

        assert result == {
            "Khan/webapp": str(repo_a),
            "owner/egg": str(repo_b),
        }

    def test_missing_config_returns_empty(self, tmp_path):
        assert build_host_repo_map.build_map(tmp_path / "does-not-exist.yaml") == {}

    def test_skips_nonexistent_paths(self, tmp_path):
        config_path = tmp_path / "repositories.yaml"
        config_path.write_text("local_repos:\n  paths:\n  - /definitely/not/real/path\n")

        assert build_host_repo_map.build_map(config_path) == {}

    def test_skips_paths_without_git_remote(self, tmp_path):
        repo = tmp_path / "no-remote"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        # no `git remote add` — origin is absent

        config_path = tmp_path / "repositories.yaml"
        config_path.write_text(f"local_repos:\n  paths:\n  - {repo}\n")

        assert build_host_repo_map.build_map(config_path) == {}

    def test_corrupted_yaml_returns_empty(self, tmp_path):
        config_path = tmp_path / "repositories.yaml"
        config_path.write_text("local_repos:\n  paths:\n  - valid\n  bad: [unterminated\n")

        assert build_host_repo_map.build_map(config_path) == {}

    def test_handles_missing_local_repos_section(self, tmp_path):
        config_path = tmp_path / "repositories.yaml"
        config_path.write_text("github_username: someone\n")

        assert build_host_repo_map.build_map(config_path) == {}

    def test_empty_local_repos_returns_empty(self, tmp_path):
        config_path = tmp_path / "repositories.yaml"
        config_path.write_text("local_repos:\n  paths: []\n")

        assert build_host_repo_map.build_map(config_path) == {}


class TestScriptEntryPoint:
    def test_cli_emits_sorted_json(self, tmp_path):
        repo_a = tmp_path / "zeta"
        repo_b = tmp_path / "alpha"
        _init_repo_with_remote(repo_a, "git@github.com:owner/zeta.git")
        _init_repo_with_remote(repo_b, "git@github.com:owner/alpha.git")

        config_path = tmp_path / "repositories.yaml"
        config_path.write_text(f"local_repos:\n  paths:\n  - {repo_a}\n  - {repo_b}\n")

        proc = subprocess.run(
            [str(_BUILDER_PATH), str(config_path)],
            capture_output=True,
            text=True,
            check=True,
        )

        payload = json.loads(proc.stdout)
        assert list(payload.keys()) == ["owner/alpha", "owner/zeta"]
        assert payload["owner/alpha"] == str(repo_b)
        assert payload["owner/zeta"] == str(repo_a)
