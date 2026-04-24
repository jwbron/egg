"""Tests for ``gateway.translate_to_host_path``.

The gateway returns host paths to the orchestrator to use as
``hostPath.path`` sources for agent-pod mounts. If the translation is
wrong, kubelet ``DirectoryOrCreate``s an empty root-owned dir and the
agent lands in an unwritable worktree (#1986). These tests cover the
two translation strategies: mountinfo-based auto-discovery (primary)
and the ``HOST_HOME`` env var (fallback).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import gateway as gateway_module  # noqa: E402


@pytest.fixture
def override_mounts():
    """Replace the module-level mount table with a test fixture."""
    original = gateway_module._MOUNT_MAPPING

    def _set(entries: list[tuple[str, str]]) -> None:
        # Sort longest-first to match _load_mount_mapping's contract.
        gateway_module._MOUNT_MAPPING = sorted(entries, key=lambda p: len(p[0]), reverse=True)

    yield _set
    gateway_module._MOUNT_MAPPING = original


@pytest.fixture
def override_host_home():
    """Temporarily set HOST_HOME at module scope."""
    original = gateway_module.HOST_HOME

    def _set(value: str) -> None:
        gateway_module.HOST_HOME = value

    yield _set
    gateway_module.HOST_HOME = original


class TestMountinfoTranslation:
    """Auto-discovered translation via /proc/self/mountinfo."""

    def test_longest_prefix_wins(self, override_mounts, override_host_home):
        # A nested hostPath mount must beat its parent emptyDir mount.
        override_mounts(
            [
                ("/home/egg", "/var/lib/kubelet/pods/abc/emptydir/home"),
                ("/home/egg/.egg-worktrees", "/home/user/.egg-worktrees"),
            ]
        )
        override_host_home("")

        result = gateway_module.translate_to_host_path("/home/egg/.egg-worktrees/pipeline-1/repo")

        assert result == "/home/user/.egg-worktrees/pipeline-1/repo"

    def test_exact_mount_point_match(self, override_mounts, override_host_home):
        override_mounts([("/home/egg/.egg-worktrees", "/home/user/.egg-worktrees")])
        override_host_home("")

        assert (
            gateway_module.translate_to_host_path("/home/egg/.egg-worktrees")
            == "/home/user/.egg-worktrees"
        )

    def test_sibling_paths_not_conflated(self, override_mounts, override_host_home):
        # /home/egg-other must not match the /home/egg mount_point.
        override_mounts([("/home/egg", "/host/egg")])
        override_host_home("")

        assert gateway_module.translate_to_host_path("/home/egg-other/x") == "/home/egg-other/x"

    def test_no_mountinfo_match_falls_through(self, override_mounts, override_host_home):
        override_mounts([("/home/egg", "/host/egg")])
        override_host_home("")

        assert gateway_module.translate_to_host_path("/other/path") == "/other/path"


class TestHostHomeFallback:
    """Explicit HOST_HOME env var, used when mountinfo lookup misses."""

    def test_host_home_used_when_mountinfo_empty(self, override_mounts, override_host_home):
        override_mounts([])
        override_host_home("/home/user")

        assert (
            gateway_module.translate_to_host_path("/home/egg/.egg-worktrees/x")
            == "/home/user/.egg-worktrees/x"
        )

    def test_mountinfo_takes_precedence_over_host_home(
        self, override_mounts, override_host_home
    ):
        # mountinfo disagrees with HOST_HOME — trust mountinfo because it
        # reflects what the kernel actually set up.
        override_mounts([("/home/egg/.egg-worktrees", "/real/host/path")])
        override_host_home("/stale/home")

        assert (
            gateway_module.translate_to_host_path("/home/egg/.egg-worktrees/x")
            == "/real/host/path/x"
        )

    def test_no_translation_when_both_unavailable(self, override_mounts, override_host_home):
        override_mounts([])
        override_host_home("")

        assert (
            gateway_module.translate_to_host_path("/home/egg/.egg-worktrees/x")
            == "/home/egg/.egg-worktrees/x"
        )

    def test_host_home_ignored_for_non_container_path(
        self, override_mounts, override_host_home
    ):
        override_mounts([])
        override_host_home("/home/user")

        assert gateway_module.translate_to_host_path("/other/path") == "/other/path"


class TestLoadMountMapping:
    """``_load_mount_mapping`` parses /proc/self/mountinfo."""

    def test_parses_mount_point_and_root(self, tmp_path):
        mountinfo = tmp_path / "mountinfo"
        mountinfo.write_text("1 0 0:1 /host/root /pod/mount rw,relatime - tmpfs tmpfs rw\n")
        with patch("gateway.open", lambda *a, **kw: mountinfo.open()):
            result = gateway_module._load_mount_mapping()
        assert ("/pod/mount", "/host/root") in result

    def test_sorts_longest_first(self, tmp_path):
        mountinfo = tmp_path / "mountinfo"
        mountinfo.write_text(
            "1 0 0:1 /r1 /a rw,relatime - tmpfs tmpfs rw\n"
            "2 0 0:2 /r2 /a/b rw,relatime - tmpfs tmpfs rw\n"
        )
        with patch("gateway.open", lambda *a, **kw: mountinfo.open()):
            result = gateway_module._load_mount_mapping()
        assert result[0] == ("/a/b", "/r2")
        assert result[1] == ("/a", "/r1")

    def test_skips_malformed_lines(self, tmp_path):
        mountinfo = tmp_path / "mountinfo"
        mountinfo.write_text("garbage\n1 0 0:1 /r /m rw,relatime - tmpfs tmpfs rw\n")
        with patch("gateway.open", lambda *a, **kw: mountinfo.open()):
            result = gateway_module._load_mount_mapping()
        assert result == [("/m", "/r")]

    def test_returns_empty_when_mountinfo_missing(self):
        def _raise(*_a, **_kw):
            raise OSError(2, "No such file or directory")

        with patch("gateway.open", _raise):
            assert gateway_module._load_mount_mapping() == []
