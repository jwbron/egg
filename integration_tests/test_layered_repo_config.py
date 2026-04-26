"""Layered repo-config integration test (issue #2073, TASK-6-3).

Builds a fixture repo with ``.egg/repositories.yaml`` plus a user file
that overrides one field, drives
``shared.egg_config.repos.load_merged_repo_config``, and asserts:

* user-file overrides win at the leaf level;
* list-valued fields replace outright;
* the host-side classifier produces the legacy two-list manifest shape
  (``persist_dirs`` + ``persist_system_dirs``) from the unified
  ``persist:``;
* ``sandbox.docker-setup.persist_build_dirs`` accepts the produced
  manifest unchanged (architect Component C3 — the sandbox image is
  cross-version-stable).

This test does NOT require Docker — it stops short of the actual image
build but covers the full host-side manifest production pipeline.
"""

from __future__ import annotations

import sys
import textwrap
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest
import yaml

# Make sure shared/ + sandbox/ are importable when run outside the
# repo's pytest harness.
_REPO_ROOT = Path(__file__).parent.parent
for _p in (_REPO_ROOT / "shared", _REPO_ROOT / "sandbox", _REPO_ROOT / "config"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from egg_config.repos import (  # noqa: E402
    _classify_persist_for_manifest,
    load_merged_repo_config,
    reload_config,
)

_DOCKER_SETUP_PATH = _REPO_ROOT / "sandbox" / "docker-setup.py"
_loader = SourceFileLoader("docker_setup_integration", str(_DOCKER_SETUP_PATH))
_docker_setup = _loader.load_module()
persist_build_dirs = _docker_setup.persist_build_dirs


@pytest.fixture(autouse=True)
def _drop_loader_cache():
    reload_config()
    yield
    reload_config()


def _make_checkout(tmp_path: Path, name: str = "foo") -> Path:
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
    return checkout


# ---------------------------------------------------------------------------
# End-to-end: layered config → manifest → docker-setup persistence
# ---------------------------------------------------------------------------


class TestLayeredConfigEndToEnd:
    def test_full_host_side_round_trip(self, tmp_path):
        """Full flow: repo file + user override → merged → manifest → persist_build_dirs."""
        checkout = _make_checkout(tmp_path)

        # Repo-level defaults: collapse-style persist (mix of repo-relative
        # and system absolute), Makefile-derived checks.
        (checkout / ".egg" / "repositories.yaml").parent.mkdir(parents=True, exist_ok=True)
        (checkout / ".egg" / "repositories.yaml").write_text(
            yaml.safe_dump(
                {
                    "schemaVersion": "1.0",
                    "build_commands": {"commands": ["true"]},
                    "watch_files": ["Makefile", "pyproject.toml"],
                    "persist": ["/usr/local/bin", ".venv"],
                }
            )
        )

        # User-side override: replaces persist with a single repo-relative
        # entry. Must REPLACE rather than APPEND.
        user_path = tmp_path / "user.yaml"
        user_path.write_text(
            yaml.safe_dump(
                {
                    "schemaVersion": "1.0",
                    "github_username": "alice",
                    "writable_repos": ["alice/foo"],
                    "repo_settings": {
                        "alice/foo": {"persist": [".venv-alt"]},
                    },
                }
            )
        )

        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)

        # User override won at the leaf level.
        block = merged.get_repo("alice/foo")
        assert block["persist"] == [".venv-alt"]
        # Replace, not append.
        assert "/usr/local/bin" not in block["persist"]
        # build_commands and watch_files came from the repo file.
        assert block["build_commands"]["commands"] == ["true"]
        assert "Makefile" in block["watch_files"]

        # Operator-scoped fields come from the user file unchanged.
        assert merged.user_file["github_username"] == "alice"
        assert merged.user_file["writable_repos"] == ["alice/foo"]

        # Host-side classifier emits the legacy two-list manifest shape.
        repo_dirs, system_dirs = _classify_persist_for_manifest(block["persist"])
        assert repo_dirs == [".venv-alt"]
        assert system_dirs == []

    def test_repo_file_persist_unchanged_when_no_override(self, tmp_path):
        """User file without per-repo override → repo-defaults persist preserved."""
        checkout = _make_checkout(tmp_path)
        (checkout / ".egg" / "repositories.yaml").parent.mkdir(parents=True, exist_ok=True)
        (checkout / ".egg" / "repositories.yaml").write_text(
            yaml.safe_dump(
                {
                    "schemaVersion": "1.0",
                    "persist": ["/usr/local/bin", ".venv"],
                }
            )
        )

        user_path = tmp_path / "user.yaml"
        user_path.write_text(yaml.safe_dump({"schemaVersion": "1.0", "github_username": "alice"}))

        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)

        # No matching user repo_settings entry → repo block surfaces
        # under synthetic __checkout__ key.
        assert "__checkout__" in merged.repo_blocks
        assert merged.repo_blocks["__checkout__"]["persist"] == [
            "/usr/local/bin",
            ".venv",
        ]

        # Classifier separates absolute vs repo-relative.
        repo_dirs, system_dirs = _classify_persist_for_manifest(
            merged.repo_blocks["__checkout__"]["persist"]
        )
        assert repo_dirs == [".venv"]
        assert system_dirs == ["/usr/local/bin"]

    def test_manifest_shape_consumed_by_docker_setup(self, tmp_path):
        """Manifest produced by the host classifier is consumable by docker-setup.py."""
        # Synthesize a manifest entry exactly as the host-side flow
        # produces it (matching ``sandbox/egg_lib/docker.py``).
        repo_dirs, system_dirs = _classify_persist_for_manifest(["/usr/local/bin", ".venv"])

        manifest_entry = {
            "repo": "alice/foo",
            "watch_files": ["pyproject.toml"],
            "commands": ["true"],
            "persist_dirs": repo_dirs,
            "persist_system_dirs": system_dirs,
        }

        # Drive docker-setup's persist_build_dirs with this manifest;
        # set up the build context with dirs that DO exist so the
        # fail-loud invariant doesn't trip.
        repo_deps = tmp_path / "repo-deps"
        prebuilt = tmp_path / "prebuilt-deps"
        work = repo_deps / "alice--foo"
        venv = work / ".venv"
        venv.mkdir(parents=True)
        (venv / "marker").write_text("ok")

        # /usr/local/bin must exist (in the test runner's environment).
        # The `persist_build_dirs` function uses /usr/local/bin's actual
        # state, so we verify only that the call doesn't blow up on the
        # manifest *shape*. We skip the system-dir entry by removing it
        # from the manifest if /usr/local/bin doesn't exist on this host.
        if not Path("/usr/local/bin").is_dir():
            manifest_entry["persist_system_dirs"] = []

        persist_build_dirs(
            [manifest_entry],
            repo_deps_base=repo_deps,
            prebuilt_base=prebuilt,
        )

        # Repo-relative .venv was persisted.
        assert (prebuilt / "alice--foo" / ".venv" / "marker").read_text() == "ok"


class TestLayeredConfigOperatorPolicies:
    """Operator-scoped policies stay user-side."""

    def test_user_file_restrict_to_configured_users_honored(self, tmp_path):
        checkout = _make_checkout(tmp_path)
        (checkout / ".egg" / "repositories.yaml").parent.mkdir(parents=True, exist_ok=True)
        (checkout / ".egg" / "repositories.yaml").write_text(
            yaml.safe_dump({"schemaVersion": "1.0", "auth_mode": "bot"})
        )
        user_path = tmp_path / "user.yaml"
        user_path.write_text(
            yaml.safe_dump(
                {
                    "schemaVersion": "1.0",
                    "repo_settings": {
                        "alice/foo": {"restrict_to_configured_users": True},
                    },
                }
            )
        )
        merged = load_merged_repo_config(checkout=checkout, user_path=user_path)
        block = merged.get_repo("alice/foo")
        # auth_mode comes from the repo defaults; the operator policy
        # is layered on top from the user file.
        assert block["auth_mode"] == "bot"
        assert block["restrict_to_configured_users"] is True
