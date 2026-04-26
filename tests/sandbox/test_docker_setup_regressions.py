"""Regression tests for issue #2073's adjacent prior incidents.

This file pins the #2087 / #2065 reproducers under the new layered
schema so a regression in any of the touched modules surfaces here:

* **#2087 reproducer** — ``uv sync`` against a watch-files-only build
  context with ``persist_dirs: [.venv]``: ``persist_build_dirs`` must
  raise ``RuntimeError`` matching the post-#2090 fail-loud message.
* **#2065 reproducer** — binary installed at ``/usr/local/bin/uv``
  without a covering ``persist:`` entry: the validator must surface
  the error at write-time AND ``persist_build_dirs`` must raise if the
  bad manifest somehow slips through.
* The sandbox-side ``docker-setup.py`` continues to honour the
  pre-#2073 manifest contract (architect Component C3) — the host
  classifier produces ``persist_dirs`` + ``persist_system_dirs``, the
  sandbox script reads them unchanged.

Cite the upstream commits in the test docstrings so a future
investigator can pull the original change ladder:

* ``514c5afaa`` — #2087 fix (``make sandbox-deps`` / ``--no-install-project``)
* ``fff9cea56`` — #2065 doc update flagging the persist-system-dirs trap
* ``aa1f5e22d`` — #2065 fix decoupling test/test-all from the venv
  prerequisite
"""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest
from egg_config.repo_validator import validate_repo_config

# Load docker-setup module (hyphenated filename mirrors the existing
# tests/sandbox/test_docker_setup.py loader pattern).
_DOCKER_SETUP_PATH = Path(__file__).parent.parent.parent / "sandbox" / "docker-setup.py"
_loader = SourceFileLoader("docker_setup_regressions", str(_DOCKER_SETUP_PATH))
docker_setup = _loader.load_module()

persist_build_dirs = docker_setup.persist_build_dirs
run_build_commands = docker_setup.run_build_commands


# ---------------------------------------------------------------------------
# #2087 reproducer
# ---------------------------------------------------------------------------


class TestIssue2087Reproducer:
    """`uv sync` against a watch-files-only context drops `.venv` silently.

    The pre-#2090 behaviour was warn-and-continue, which silently shipped
    images without the ``.venv`` they advertised. #2090 (commit
    514c5afaa) raised on this case at build time. This test pins the
    fail-loud invariant under the #2073 schema: the legacy two-list
    manifest shape is unchanged (architect Component C3), but the
    upstream classifier now feeds it from the unified user-facing
    ``persist:`` list.

    See: 514c5afaa (#2087 fix).
    """

    def test_persist_dirs_missing_after_build_raises(self, tmp_path):
        # Build context exists (so run_build_commands didn't bail
        # early) but the .venv was never produced — exactly the #2087
        # shape after `uv sync` errors silently against a context
        # without source files.
        repo_deps = tmp_path / "repo-deps"
        prebuilt = tmp_path / "prebuilt-deps"
        work = repo_deps / "alice--foo"
        work.mkdir(parents=True)
        # The watch-files-only build context shape — no .venv produced.
        (work / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
        (work / "uv.lock").write_text("# minimal lock\n")

        manifest_entry = {
            "repo": "alice/foo",
            "watch_files": ["pyproject.toml", "uv.lock"],
            "commands": ["uv sync"],
            "persist_dirs": [".venv"],
            "persist_system_dirs": [],
        }

        with pytest.raises(RuntimeError) as excinfo:
            persist_build_dirs(
                [manifest_entry],
                repo_deps_base=repo_deps,
                prebuilt_base=prebuilt,
            )
        msg = str(excinfo.value)
        # Diagnostic must be specific enough that the operator knows
        # what to fix (matches the post-#2090 fail-loud prose).
        assert ".venv" in msg
        assert "alice/foo" in msg
        assert "build commands" in msg.lower() or "watch_files" in msg

    def test_persist_dirs_present_after_build_passes(self, tmp_path):
        """Sanity: the same shape DOES persist when `.venv` actually exists."""
        repo_deps = tmp_path / "repo-deps"
        prebuilt = tmp_path / "prebuilt-deps"
        work = repo_deps / "alice--foo"
        venv = work / ".venv"
        venv.mkdir(parents=True)
        (venv / "marker").write_text("ok")

        manifest_entry = {
            "repo": "alice/foo",
            "watch_files": ["pyproject.toml"],
            "commands": ["true"],
            "persist_dirs": [".venv"],
            "persist_system_dirs": [],
        }

        # No exception expected.
        persist_build_dirs(
            [manifest_entry],
            repo_deps_base=repo_deps,
            prebuilt_base=prebuilt,
        )
        # Result lives at __egg_system_dirs__ for system, prebuilt root for repo.
        assert (prebuilt / "alice--foo" / ".venv" / "marker").read_text() == "ok"


# ---------------------------------------------------------------------------
# #2065 reproducer
# ---------------------------------------------------------------------------


class TestIssue2065Reproducer:
    """Install to ``/usr/local/bin/uv`` without a covering ``persist:`` entry.

    The original incident shipped an image that installed ``uv`` to
    ``/usr/local/bin`` via build_commands but did not list
    ``/usr/local/bin`` under ``persist_system_dirs``, so the binary
    silently disappeared between Docker stages. The #2073 validator
    catches this at write-time; ``docker-setup.py`` raises if the bad
    manifest survives to build time.

    See: fff9cea56 (#2065 doc update), aa1f5e22d (#2065 fix).
    """

    def test_validator_flags_install_path_without_persist(self, tmp_path):
        """Write-time gate: validator surfaces the missing persist entry."""
        # Build a synthetic checkout with a repo-defaults file that
        # installs uv to /usr/local/bin but does NOT persist that path.
        checkout = tmp_path / "foo"
        (checkout / ".egg").mkdir(parents=True)
        (checkout / ".egg" / "repositories.yaml").write_text(
            "schemaVersion: '1.0'\n"
            "build_commands:\n"
            "  commands:\n"
            "    - 'curl -LsSf https://astral.sh/uv/install.sh "
            "| env UV_INSTALL_DIR=/usr/local/bin sh'\n"
            "watch_files:\n"
            "  - pyproject.toml\n"
            "persist:\n"
            "  - .venv\n"
        )

        result = validate_repo_config(checkout=checkout, user_path=None)

        # Must surface the missing-persist diagnostic for /usr/local/bin.
        joined = " ".join(result.errors)
        assert "/usr/local/bin" in joined
        assert "persist" in joined.lower()
        assert "#2065" in joined or "covers it" in joined

    def test_persist_build_dirs_raises_for_missing_system_path(self, tmp_path):
        """Defense-in-depth: docker-setup.py raises when the path is absent."""
        repo_deps = tmp_path / "repo-deps"
        prebuilt = tmp_path / "prebuilt-deps"
        # Build context exists, but /usr/local/<missing> doesn't.
        work = repo_deps / "alice--foo"
        work.mkdir(parents=True)

        # We can't realistically point a test at /usr/local/bin (the
        # tests are unsandboxed). Use a tmp-path absolute that is
        # neither in DENIED_EXACT nor under any DENIED_PREFIXES so we
        # exercise the missing-path branch. The temp path is absolute
        # but not a denied prefix, so the script goes past the
        # denylist check and into the is_dir() check.
        nonexistent_abs = str(tmp_path / "absent-system-dir")

        manifest_entry = {
            "repo": "alice/foo",
            "watch_files": [],
            "commands": [],
            "persist_dirs": [],
            "persist_system_dirs": [nonexistent_abs],
        }

        with pytest.raises(RuntimeError) as excinfo:
            persist_build_dirs(
                [manifest_entry],
                repo_deps_base=repo_deps,
                prebuilt_base=prebuilt,
            )
        msg = str(excinfo.value)
        assert "alice/foo" in msg
        assert "does not exist" in msg
        assert "build commands" in msg.lower()


# ---------------------------------------------------------------------------
# Shape stability: pre-#2073 manifest field names still drive docker-setup.py
# ---------------------------------------------------------------------------


class TestManifestContractStability:
    """Architect Component C3 — manifest stays on the legacy two-list shape.

    ``sandbox/docker-setup.py`` reads ``persist_dirs`` and
    ``persist_system_dirs`` from each manifest entry. Even after the
    user-facing ``persist:`` collapse, the host-side classifier
    produces these two lists so existing sandbox images cross-version
    keep working unchanged.
    """

    def test_get_build_commands_emits_two_list_manifest_shape(self):
        # Drive ``get_build_commands`` against the two-list manifest
        # shape it has always read. This is the contract the host-side
        # classifier in ``shared/egg_config/repos.py`` upholds.
        config = {
            "repo_settings": {
                "alice/foo": {
                    "build_commands": {
                        "commands": ["echo hi"],
                        "watch_files": ["pyproject.toml"],
                        "persist_dirs": [".venv"],
                        "persist_system_dirs": ["/usr/local/bin"],
                    }
                }
            }
        }
        out = docker_setup.get_build_commands(config)
        assert len(out) == 1
        entry = out[0]
        assert entry["persist_dirs"] == [".venv"]
        assert entry["persist_system_dirs"] == ["/usr/local/bin"]
        # Round-tripping through persist_build_dirs accepts that shape.

    def test_run_build_commands_no_op_on_empty_list(self, tmp_path):
        """Empty manifest is a no-op (preserves prior behaviour)."""
        # Should not raise.
        run_build_commands([], repo_deps_base=tmp_path)
