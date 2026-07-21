"""Regression tests for ``bin/egg-init`` dependency enumeration.

Guards against the ``set -e`` abort bug where ``need_cmd`` returned
non-zero for a missing binary and, being invoked as a bare statement
under ``set -euo pipefail``, tripped ``set -e`` and aborted the script
at the *first* missing tool — leaving the enumerate-all install hints
and the failure summary unreachable (PR #3590 review).

Both the preflight stage and ``--check`` mode share the same
``need_cmd`` helper, so each has a test that runs it against a curated
PATH from which all seven onboarding dependencies are deliberately
absent, and asserts that:

  * every missing dependency is enumerated (not just the first), and
  * the run reaches the trailing failure summary (proving no early
    abort).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

EGG_INIT = Path(__file__).resolve().parents[2] / "bin" / "egg-init"

# Dependencies egg-init probes via need_cmd. The test masks every one of
# these off PATH so the enumerate-all path is exercised end to end.
MASKED_DEPS = ("docker", "git", "gh", "kubectl", "envsubst", "claude")


def _isolated_path(bin_dir: Path) -> str:
    """Mirror the real PATH into ``bin_dir`` as symlinks, minus the masked
    onboarding dependencies, and return it as a PATH string.

    Coreutils (bash, grep, sed, dirname, uname, …) stay available so the
    script runs; ``docker``/``git``/``gh``/``kubectl``/``envsubst``/``claude``
    are guaranteed absent so ``command -v`` reports them missing. A shim
    cannot be used to fake a *missing* command — ``command -v`` would find
    it — so genuine removal from PATH is required.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    for src_dir in ("/usr/local/bin", "/usr/bin", "/bin"):
        d = Path(src_dir)
        if not d.is_dir():
            continue
        for entry in d.iterdir():
            dst = bin_dir / entry.name
            if not dst.exists():
                try:
                    dst.symlink_to(entry)
                except OSError:
                    pass
    for dep in MASKED_DEPS:
        (bin_dir / dep).unlink(missing_ok=True)
    return str(bin_dir)


def _base_env(tmp_path: Path) -> dict[str, str]:
    """Env with an isolated PATH, HOME, and egg config/skills dirs so the
    run touches nothing real and finds no pre-existing config."""
    bin_dir = tmp_path / "bin"
    env = {
        "PATH": _isolated_path(bin_dir),
        "HOME": str(tmp_path / "home"),
        "EGG_CONFIG_DIR": str(tmp_path / "cfg"),
        "EGG_SKILLS_DIR": str(tmp_path / "skills"),
    }
    for key in ("HOME", "EGG_CONFIG_DIR", "EGG_SKILLS_DIR"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    return env


@pytest.fixture(scope="module")
def bash() -> str:
    exe = shutil.which("bash")
    if not exe:
        pytest.skip("bash not available")
    return exe


class TestCheckEnumeratesAllMissingDeps:
    """``bin/egg-init --check`` must list every missing dependency and
    reach the trailing sections, not abort at the first."""

    def test_check_enumerates_all_and_reaches_summary(
        self, bash: str, tmp_path: Path
    ) -> None:
        env = _base_env(tmp_path)
        proc = subprocess.run(
            [bash, str(EGG_INIT), "--check"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = proc.stdout + proc.stderr

        # Every masked dependency is enumerated — not just the first.
        for dep in MASKED_DEPS:
            assert f"{dep} not found" in out, (
                f"{dep!r} missing from --check output; the run likely "
                f"aborted early.\n{out}"
            )

        # The run reached sections that come *after* the dependency block,
        # proving no early abort.
        assert "Configuration files" in out
        assert "check(s) failed" in out
        # A reported-failures exit is 1; a set -e abort would surface a
        # different (often 127/nonzero-from-die) path without the summary.
        assert proc.returncode == 1


class TestPreflightEnumeratesAllMissingDeps:
    """The ``stage_preflight`` function shares ``need_cmd``; drive it
    directly (main() stripped) to confirm it enumerates all deps and
    reaches its summary rather than aborting on the first."""

    def test_preflight_enumerates_all_and_reaches_summary(
        self, bash: str, tmp_path: Path
    ) -> None:
        env = _base_env(tmp_path)
        # Source the script with its final `main "$@"` line removed, set
        # ASSUME_YES *after* sourcing (the script resets it to 0), then
        # invoke stage_preflight in isolation.
        script = (
            'source <(sed "$ d" "$1"); '
            "ASSUME_YES=1; "
            "stage_preflight"
        )
        proc = subprocess.run(
            [bash, "-c", script, "_", str(EGG_INIT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = proc.stdout + proc.stderr

        for dep in MASKED_DEPS:
            assert f"{dep} not found" in out, (
                f"{dep!r} missing from preflight output; the run likely "
                f"aborted early.\n{out}"
            )

        # The per-OS install hints for tools *after* docker must appear —
        # these are exactly what an early abort would suppress.
        assert "kubernetes.io/docs/tasks/tools" in out  # kubectl hint
        assert "claude.ai/install.sh" in out  # claude hint
        # Reached the enumerate-all summary line.
        assert "preflight check(s) failed" in out
