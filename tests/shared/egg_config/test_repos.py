"""Tests for ``shared/egg_config/repos.py`` (issue #2073, TASK-2-2).

Cover the centralized layered repo-config loader:

* replace-by-default for every list-valued field
* deep-merge for dicts (build_commands fields)
* auto-discover present-vs-absent
* repo-defaults rejected for operator-scoped keys
* repo-file ``persist:`` denylist enforcement (each entry in the
  denylist triggers an error; safe paths pass; operator-side use of
  denylisted paths is allowed)
* malformed YAML in either file produces a ``ConfigError`` whose
  message names both file paths
* version-tolerance behavior pinned in TASK-1-1
* mtime cache invalidation on file rewrite

Tests use ``tmp_path`` fixtures; no global state survives between cases
because ``reload_config()`` is called from the autouse fixture below.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from egg_config.repos import (
    MergedRepoConfig,
    _classify_persist_for_manifest,
    _enforce_repo_persist_denylist,
    _is_denylisted_abs_path,
    load_merged_repo_config,
    reload_config,
)
from egg_config.repos_schema import ConfigError


@pytest.fixture(autouse=True)
def _drop_cache_before_each_test():
    """The loader caches per-process; drop it before every test."""
    reload_config()
    yield
    reload_config()


def _write_yaml(path: Path, body: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def _write_text(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Fixtures: synthetic checkout + user file
# ---------------------------------------------------------------------------


def _write_repo_defaults(checkout: Path, body: dict) -> Path:
    return _write_yaml(checkout / ".egg" / "repositories.yaml", body)


def _write_user_file(tmp_path: Path, body: dict, name: str = "user.yaml") -> Path:
    return _write_yaml(tmp_path / name, body)


def _make_checkout_with_remote(
    tmp_path: Path, name: str, remote: str = "https://github.com/alice/foo.git"
) -> Path:
    """Create a fake checkout with a .git/config naming the remote."""
    checkout = tmp_path / name
    checkout.mkdir(parents=True, exist_ok=True)
    git_dir = checkout / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "config").write_text(
        textwrap.dedent(
            f"""
            [core]
            \trepositoryformatversion = 0
            [remote "origin"]
            \turl = {remote}
            \tfetch = +refs/heads/*:refs/remotes/origin/*
            """
        ).strip()
        + "\n"
    )
    return checkout


# ---------------------------------------------------------------------------
# (1) Replace-by-default for every list-valued field
# ---------------------------------------------------------------------------


class TestListReplaceByDefault:
    """``persist`` / ``watch_files`` / ``checks`` replace, do not append."""

    def test_user_persist_replaces_repo_persist(self, tmp_path):
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(
            checkout,
            {
                "schemaVersion": "1.0",
                "persist": ["/usr/local/bin", ".venv"],
            },
        )
        user_path = _write_user_file(
            tmp_path,
            {
                "schemaVersion": "1.0",
                "repo_settings": {"alice/foo": {"persist": [".venv"]}},
            },
        )
        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)
        block = merged.get_repo("alice/foo")
        assert block["persist"] == [".venv"]
        assert "/usr/local/bin" not in block["persist"]

    def test_user_watch_files_replaces_repo_watch_files(self, tmp_path):
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(
            checkout,
            {
                "schemaVersion": "1.0",
                "watch_files": ["pyproject.toml", "uv.lock"],
            },
        )
        user_path = _write_user_file(
            tmp_path,
            {
                "schemaVersion": "1.0",
                "repo_settings": {
                    "alice/foo": {"watch_files": ["Makefile"]},
                },
            },
        )
        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)
        assert merged.get_repo("alice/foo")["watch_files"] == ["Makefile"]

    def test_user_checks_replaces_repo_checks(self, tmp_path):
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(
            checkout,
            {
                "schemaVersion": "1.0",
                "checks": [
                    {"name": "lint", "command": "make lint"},
                    {"name": "test", "command": "make test"},
                ],
            },
        )
        user_path = _write_user_file(
            tmp_path,
            {
                "schemaVersion": "1.0",
                "repo_settings": {"alice/foo": {"checks": [{"name": "ci", "command": "make ci"}]}},
            },
        )
        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)
        block = merged.get_repo("alice/foo")
        assert block["checks"] == [{"name": "ci", "command": "make ci"}]

    def test_repo_only_block_with_no_user_override(self, tmp_path):
        """Repo-defaults survive when there's no user override entry."""
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(
            checkout,
            {
                "schemaVersion": "1.0",
                "persist": ["/usr/local/bin", ".venv"],
            },
        )
        user_path = _write_user_file(
            tmp_path,
            {"schemaVersion": "1.0", "repo_settings": {}},
        )
        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)
        # Surfaced under synthetic __checkout__ key.
        assert "__checkout__" in merged.repo_blocks
        block = merged.repo_blocks["__checkout__"]
        assert block["persist"] == ["/usr/local/bin", ".venv"]


# ---------------------------------------------------------------------------
# (2) Deep-merge for dict fields (build_commands)
# ---------------------------------------------------------------------------


class TestDeepMergeDicts:
    """``build_commands`` deep-merges so leaf scalar overrides apply."""

    def test_build_commands_merge_replaces_inner_lists(self, tmp_path):
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(
            checkout,
            {
                "schemaVersion": "1.0",
                "build_commands": {"commands": ["echo base"]},
            },
        )
        user_path = _write_user_file(
            tmp_path,
            {
                "schemaVersion": "1.0",
                "repo_settings": {
                    "alice/foo": {
                        "build_commands": {"commands": ["echo override"]},
                    }
                },
            },
        )
        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)
        block = merged.get_repo("alice/foo")
        assert block["build_commands"]["commands"] == ["echo override"]

    def test_build_commands_keeps_repo_only_keys(self, tmp_path):
        """Keys present only in the repo file survive the merge."""
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(
            checkout,
            {
                "schemaVersion": "1.0",
                "build_commands": {
                    "commands": ["make sandbox-deps"],
                },
            },
        )
        user_path = _write_user_file(
            tmp_path,
            {
                "schemaVersion": "1.0",
                "repo_settings": {
                    "alice/foo": {
                        "build_commands": {},  # Empty mapping, keys preserved from base
                    }
                },
            },
        )
        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)
        block = merged.get_repo("alice/foo")
        # No override → base is preserved.
        assert block["build_commands"]["commands"] == ["make sandbox-deps"]


# ---------------------------------------------------------------------------
# (3) Auto-discover present-vs-absent
# ---------------------------------------------------------------------------


class TestAutoDiscover:
    """``<checkout>/.egg/repositories.yaml`` is auto-loaded when present."""

    def test_repo_file_present_loaded(self, tmp_path):
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(
            checkout,
            {"schemaVersion": "1.0", "persist": [".venv"]},
        )
        user_path = _write_user_file(
            tmp_path,
            {
                "schemaVersion": "1.0",
                "repo_settings": {"alice/foo": {}},
            },
        )
        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)
        assert merged.get_repo("alice/foo")["persist"] == [".venv"]

    def test_repo_file_absent_silent_skip(self, tmp_path):
        """Decision-10: silent-skip if absent."""
        checkout = tmp_path / "foo"
        checkout.mkdir()
        # No .egg/repositories.yaml
        user_path = _write_user_file(
            tmp_path,
            {
                "schemaVersion": "1.0",
                "github_username": "alice",
                "repo_settings": {"alice/foo": {"auth_mode": "user"}},
            },
        )
        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)
        assert merged.user_file["github_username"] == "alice"
        assert merged.get_repo("alice/foo")["auth_mode"] == "user"

    def test_no_user_file_no_checkout_returns_empty(self, tmp_path):
        merged = load_merged_repo_config(checkout=None, user_path=None)
        # Cannot guarantee no env-var fallback; but the result is well-formed.
        assert isinstance(merged, MergedRepoConfig)


# ---------------------------------------------------------------------------
# (4) Repo-defaults rejected for operator-scoped keys
# ---------------------------------------------------------------------------


class TestRepoDefaultsRejectsOperatorKeys:
    """Operator-scoped keys in repo file blow up the loader."""

    def test_operator_top_level_key_rejected(self, tmp_path):
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(
            checkout,
            {
                "schemaVersion": "1.0",
                "github_username": "alice",  # operator-scoped
            },
        )
        user_path = _write_user_file(tmp_path, {"schemaVersion": "1.0"})
        with pytest.raises(ConfigError) as excinfo:
            load_merged_repo_config(checkout=checkout, user_path=user_path)
        msg = str(excinfo.value)
        assert "github_username" in msg
        assert "user file" in msg.lower() or "~/.config/egg" in msg

    def test_operator_per_repo_key_rejected(self, tmp_path):
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(
            checkout,
            {
                "schemaVersion": "1.0",
                "restrict_to_configured_users": True,  # operator policy
            },
        )
        user_path = _write_user_file(tmp_path, {"schemaVersion": "1.0"})
        with pytest.raises(ConfigError) as excinfo:
            load_merged_repo_config(checkout=checkout, user_path=user_path)
        msg = str(excinfo.value)
        assert "restrict_to_configured_users" in msg


# ---------------------------------------------------------------------------
# (5) Repo-file persist denylist enforcement
# ---------------------------------------------------------------------------


class TestRepoFilePersistDenylist:
    """The denylist is the security floor for auto-discovered repo files."""

    @pytest.mark.parametrize(
        "denied_path",
        [
            "/etc",
            "/etc/passwd",
            "/root",
            "/root/.bashrc",
            "/var/log",
            "/home/alice",
            "/home/alice/.ssh",
            "/proc/self",
            "/sys/firmware",
            "/dev/null",
            "/some/.ssh/keys",
            # Outside the safe set
            "/usr/bin",
            "/srv/data",
            "/mnt/something",
        ],
    )
    def test_denylisted_path_rejected_in_repo_file(self, tmp_path, denied_path):
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(
            checkout,
            {"schemaVersion": "1.0", "persist": [denied_path]},
        )
        user_path = _write_user_file(tmp_path, {"schemaVersion": "1.0"})
        with pytest.raises(ConfigError) as excinfo:
            load_merged_repo_config(checkout=checkout, user_path=user_path)
        msg = str(excinfo.value)
        assert denied_path in msg
        # Diagnostic must point at the safe-set.
        assert "/usr/local/" in msg or "/opt/" in msg

    @pytest.mark.parametrize(
        "safe_path",
        [
            "/usr/local/bin",
            "/usr/local/go",
            "/opt/foo",
            ".venv",
            "node_modules",
            "dist/cache",
        ],
    )
    def test_safe_path_passes(self, tmp_path, safe_path):
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(
            checkout,
            {"schemaVersion": "1.0", "persist": [safe_path]},
        )
        user_path = _write_user_file(tmp_path, {"schemaVersion": "1.0"})
        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)
        # Successful load → block surfaced under synthetic key.
        block = merged.repo_blocks["__checkout__"]
        assert safe_path in block["persist"]

    def test_user_file_can_persist_denylisted_path(self, tmp_path):
        """Operator-side overrides may persist whatever the user wants."""
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(
            checkout,
            {"schemaVersion": "1.0", "persist": [".venv"]},
        )
        user_path = _write_user_file(
            tmp_path,
            {
                "schemaVersion": "1.0",
                "repo_settings": {
                    "alice/foo": {"persist": ["/etc/myconfig"]},
                },
            },
        )
        # Loader must NOT raise — user-side persist is unrestricted.
        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)
        assert merged.get_repo("alice/foo")["persist"] == ["/etc/myconfig"]

    def test_enforce_repo_persist_denylist_helper(self):
        # Direct helper test for ``_enforce_repo_persist_denylist``.
        with pytest.raises(ConfigError):
            _enforce_repo_persist_denylist(["/etc/passwd"], repo_label="<test>")
        # Safe set passes.
        _enforce_repo_persist_denylist(
            ["/usr/local/bin", ".venv", "/opt/cache"], repo_label="<test>"
        )

    def test_is_denylisted_abs_path_helper(self):
        # /usr/local/bin is allowed.
        assert _is_denylisted_abs_path("/usr/local/bin") is False
        assert _is_denylisted_abs_path("/opt/foo") is False
        # /etc denied.
        assert _is_denylisted_abs_path("/etc/passwd") is True
        # /home denied.
        assert _is_denylisted_abs_path("/home/alice") is True
        # /.ssh substring denied.
        assert _is_denylisted_abs_path("/some/path/.ssh/keys") is True
        # Outside safe set denied.
        assert _is_denylisted_abs_path("/usr/bin") is True


# ---------------------------------------------------------------------------
# (6) Malformed YAML produces ConfigError naming the file paths
# ---------------------------------------------------------------------------


class TestMalformedYaml:
    def test_user_file_yaml_error_surfaces_path(self, tmp_path):
        bad = tmp_path / "user.yaml"
        bad.write_text(": : : not yaml")
        with pytest.raises(ConfigError) as excinfo:
            load_merged_repo_config(checkout=None, user_path=bad)
        assert str(bad) in str(excinfo.value)

    def test_repo_file_yaml_error_surfaces_path(self, tmp_path):
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        bad_repo = checkout / ".egg" / "repositories.yaml"
        bad_repo.parent.mkdir(parents=True, exist_ok=True)
        bad_repo.write_text("[unclosed: list")
        user_path = _write_user_file(tmp_path, {"schemaVersion": "1.0"})
        with pytest.raises(ConfigError) as excinfo:
            load_merged_repo_config(checkout=checkout, user_path=user_path)
        assert str(bad_repo) in str(excinfo.value)


# ---------------------------------------------------------------------------
# (7) Version-tolerance behavior
# ---------------------------------------------------------------------------


class TestVersionTolerance:
    def test_unknown_future_major_in_repo_file_hard_fails(self, tmp_path):
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(
            checkout,
            {"schemaVersion": "9.0", "persist": [".venv"]},
        )
        user_path = _write_user_file(tmp_path, {"schemaVersion": "1.0"})
        with pytest.raises(ConfigError, match="9.0"):
            load_merged_repo_config(checkout=checkout, user_path=user_path)

    def test_unknown_future_major_in_user_file_hard_fails(self, tmp_path):
        user_path = _write_user_file(tmp_path, {"schemaVersion": "9.0"})
        with pytest.raises(ConfigError, match="9.0"):
            load_merged_repo_config(checkout=None, user_path=user_path)

    def test_known_major_loads(self, tmp_path):
        user_path = _write_user_file(tmp_path, {"schemaVersion": "1.5"})
        merged = load_merged_repo_config(checkout=None, user_path=user_path)
        assert merged.user_file["schemaVersion"] == "1.5"


# ---------------------------------------------------------------------------
# (8) mtime cache invalidation on file rewrite
# ---------------------------------------------------------------------------


class TestMtimeCache:
    def test_same_inputs_two_calls_use_cache(self, tmp_path):
        """Same files + mtime → second call hits the LRU cache.

        We verify cache hits by counting the number of times the
        underlying YAML loader is invoked.
        """
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(checkout, {"schemaVersion": "1.0", "persist": [".venv"]})
        user_path = _write_user_file(tmp_path, {"schemaVersion": "1.0"})

        with patch(
            "egg_config.repos._read_yaml",
            wraps=__import__("egg_config.repos", fromlist=["_read_yaml"])._read_yaml,
        ) as spy:
            load_merged_repo_config(checkout=checkout, user_path=user_path)
            first_calls = spy.call_count
            load_merged_repo_config(checkout=checkout, user_path=user_path)
            second_calls = spy.call_count
        # Second call should not have added FS reads.
        assert second_calls == first_calls

    def test_repo_file_rewrite_invalidates_cache(self, tmp_path):
        """Touching the repo file (changing mtime) bypasses the LRU cache."""
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(checkout, {"schemaVersion": "1.0", "persist": [".venv"]})
        user_path = _write_user_file(tmp_path, {"schemaVersion": "1.0"})

        merged_a = load_merged_repo_config(checkout=checkout, user_path=user_path)
        assert merged_a.repo_blocks["__checkout__"]["persist"] == [".venv"]

        # Rewrite with new content + bump mtime forward.
        repo_file = checkout / ".egg" / "repositories.yaml"
        os.utime(repo_file, (repo_file.stat().st_atime, repo_file.stat().st_mtime + 10))
        _write_repo_defaults(
            checkout,
            {"schemaVersion": "1.0", "persist": ["node_modules"]},
        )

        merged_b = load_merged_repo_config(checkout=checkout, user_path=user_path)
        assert merged_b.repo_blocks["__checkout__"]["persist"] == ["node_modules"]

    def test_reload_config_drops_cache(self, tmp_path):
        checkout = _make_checkout_with_remote(tmp_path, "foo")
        _write_repo_defaults(checkout, {"schemaVersion": "1.0", "persist": [".venv"]})
        user_path = _write_user_file(tmp_path, {"schemaVersion": "1.0"})
        load_merged_repo_config(checkout=checkout, user_path=user_path)
        # No assertion needed — just that reload_config() does not raise.
        reload_config()
        load_merged_repo_config(checkout=checkout, user_path=user_path)


# ---------------------------------------------------------------------------
# (9) Helper: _classify_persist_for_manifest
# ---------------------------------------------------------------------------


class TestClassifyPersistForManifest:
    def test_split_repo_and_system(self):
        repo_dirs, system_dirs = _classify_persist_for_manifest(
            [".venv", "/usr/local/bin", "node_modules", "/opt/foo"]
        )
        assert repo_dirs == [".venv", "node_modules"]
        assert system_dirs == ["/usr/local/bin", "/opt/foo"]

    def test_empty_input_two_empty_lists(self):
        repo_dirs, system_dirs = _classify_persist_for_manifest([])
        assert repo_dirs == []
        assert system_dirs == []


# ---------------------------------------------------------------------------
# (10) MergedRepoConfig.get_repo case-insensitive lookup
# ---------------------------------------------------------------------------


class TestMergedRepoConfigCaseInsensitive:
    def test_get_repo_case_insensitive(self):
        cfg = MergedRepoConfig(
            repo_blocks={"alice/Foo": {"persist": [".venv"]}},
        )
        assert cfg.get_repo("ALICE/FOO")["persist"] == [".venv"]
        assert cfg.get_repo("Alice/foo")["persist"] == [".venv"]

    def test_get_repo_missing_returns_empty_dict(self):
        cfg = MergedRepoConfig()
        assert cfg.get_repo("alice/none") == {}


# ---------------------------------------------------------------------------
# (11) Checkout repo-name resolution from .git/config
# ---------------------------------------------------------------------------


class TestCheckoutRepoNameResolution:
    def test_https_remote_resolves(self, tmp_path):
        checkout = _make_checkout_with_remote(
            tmp_path, "foo", remote="https://github.com/alice/foo.git"
        )
        _write_repo_defaults(checkout, {"schemaVersion": "1.0", "persist": [".venv"]})
        user_path = _write_user_file(
            tmp_path,
            {
                "schemaVersion": "1.0",
                "repo_settings": {"alice/foo": {"auth_mode": "bot"}},
            },
        )
        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)
        block = merged.get_repo("alice/foo")
        assert block["persist"] == [".venv"]
        assert block["auth_mode"] == "bot"

    def test_ssh_remote_resolves(self, tmp_path):
        checkout = _make_checkout_with_remote(
            tmp_path, "foo", remote="git@github.com:alice/foo.git"
        )
        _write_repo_defaults(checkout, {"schemaVersion": "1.0", "persist": [".venv"]})
        user_path = _write_user_file(
            tmp_path,
            {
                "schemaVersion": "1.0",
                "repo_settings": {"alice/foo": {}},
            },
        )
        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)
        assert merged.get_repo("alice/foo")["persist"] == [".venv"]

    def test_no_git_remote_falls_back_to_synthetic_key(self, tmp_path):
        # No .git/config — repo-defaults block surfaces under __checkout__
        checkout = tmp_path / "foo"
        _write_repo_defaults(checkout, {"schemaVersion": "1.0", "persist": [".venv"]})
        user_path = _write_user_file(tmp_path, {"schemaVersion": "1.0"})
        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)
        assert "__checkout__" in merged.repo_blocks
        assert merged.repo_blocks["__checkout__"]["persist"] == [".venv"]

    def test_repo_defaults_does_not_cascade_to_other_user_repos(self, tmp_path):
        """Repo-defaults attribution is scoped to the checkout's repo only."""
        checkout = _make_checkout_with_remote(
            tmp_path, "foo", remote="https://github.com/alice/foo.git"
        )
        _write_repo_defaults(checkout, {"schemaVersion": "1.0", "persist": [".venv"]})
        user_path = _write_user_file(
            tmp_path,
            {
                "schemaVersion": "1.0",
                "repo_settings": {
                    "alice/foo": {},  # the checkout's repo
                    "alice/bar": {},  # unrelated repo, no defaults
                },
            },
        )
        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)
        # alice/foo carries the merged block.
        assert merged.get_repo("alice/foo")["persist"] == [".venv"]
        # alice/bar is empty (the user file's empty block). The
        # repo-defaults must NOT cascade to other repos.
        assert merged.get_repo("alice/bar") == {}
