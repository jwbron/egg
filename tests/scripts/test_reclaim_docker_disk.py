"""Tests for scripts/reclaim-docker-disk.sh.

This script bounds the *docker* image + BuildKit cache store before a
`make k3s-import`, so the import's transient `docker save` + `ctr import`
spike does not push the shared root fs over kubelet's image-GC high-water
mark and get the freshly-imported egg images evicted mid-run.

Two invariants are load-bearing and tested end-to-end via a PATH-shimmed
`docker`:

1. The four-image safety gate: stale tags are reaped ONLY when every
   egg-*:KEEP_TAG image is present. A half-built tag must strand nothing.
2. Tag-scoped reap: stale egg tags (tag != KEEP_TAG, != latest) are removed
   by `docker image rm <repo:tag>`, while KEEP_TAG and :latest are kept.
   Unlike the containerd reap there is deliberately NO digest guard --
   `docker image rm` is name-scoped, so a shared image ID is untagged, not
   deleted.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "reclaim-docker-disk.sh"
# Exec bash by absolute path so the test's PATH governs only what the *script*
# sees (notably whether `docker` is resolvable), not whether bash itself is.
BASH = shutil.which("bash") or "bash"


def _write_docker_shim(
    bindir: Path,
    images_file: Path,
    removed_file: Path,
    builder_args_file: Path,
    fail_ref: str = "",
    fail_max_used_space: bool = False,
) -> None:
    """Write a `docker` shim covering the subcommands the script invokes.

    - `image inspect <ref>`        -> exit 0 iff <ref> is in images_file
    - `image ls --format .. <repo>`-> print images_file lines for <repo>
    - `image rm <ref>`             -> record <ref>; exit 1 iff <ref>==fail_ref
    - `image prune -f`             -> no-op success
    - `builder prune ...`          -> record args; exit 1 iff fail_max_used_space
                                       AND `--max-used-space` is in the args
                                       (simulates an old docker that lacks the
                                       flag). Otherwise success.
    """
    docker = bindir / "docker"
    fail_mus = "1" if fail_max_used_space else "0"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        'case "$1 $2" in\n'
        '  "image inspect")\n'
        f'    grep -qxF "$3" "{images_file}" && exit 0 || exit 1 ;;\n'
        '  "image ls")\n'
        # The repo filter is the last positional arg.
        '    repo="${@: -1}"\n'
        f'    grep -E "^${{repo}}:" "{images_file}" || true ; exit 0 ;;\n'
        '  "image rm")\n'
        f'    echo "$3" >> "{removed_file}"\n'
        f'    if [ -n "{fail_ref}" ] && [ "$3" = "{fail_ref}" ]; then exit 1; fi\n'
        "    exit 0 ;;\n"
        '  "image prune") exit 0 ;;\n'
        '  "builder prune")\n'
        # Record every builder-prune invocation's args, one per line, so the
        # test can assert what flags (and what value of --max-used-space) the
        # script actually passed.
        f'    echo "$*" >> "{builder_args_file}"\n'
        # Simulate an old docker that does not understand --max-used-space.
        f'    if [ "{fail_mus}" = "1" ]; then\n'
        '      for arg in "$@"; do\n'
        '        if [ "$arg" = "--max-used-space" ]; then exit 1; fi\n'
        "      done\n"
        "    fi\n"
        "    exit 0 ;;\n"
        '  *) echo "unexpected docker args: $*" >&2; exit 99 ;;\n'
        "esac\n"
    )
    docker.chmod(0o755)


def _run_script(
    tmp_path: Path,
    present: list[str],
    keep: str,
    fail_ref: str = "",
    fail_max_used_space: bool = False,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    """Run reclaim-docker-disk.sh with `docker` shimmed to `present`.

    Returns (completed_process, refs_asked_to_remove, builder_prune_invocations)
    where the third element is the list of arg-strings the script passed to
    `docker builder prune ...`, one per invocation in call order.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    images_file = tmp_path / "images.txt"
    images_file.write_text("\n".join(present) + "\n")
    removed_file = tmp_path / "removed.txt"
    removed_file.write_text("")
    builder_args_file = tmp_path / "builder_args.txt"
    builder_args_file.write_text("")
    _write_docker_shim(
        bindir,
        images_file,
        removed_file,
        builder_args_file,
        fail_ref=fail_ref,
        fail_max_used_space=fail_max_used_space,
    )

    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [BASH, str(SCRIPT), keep],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    removed = [line for line in removed_file.read_text().splitlines() if line]
    builder_invocations = [line for line in builder_args_file.read_text().splitlines() if line]
    return proc, removed, builder_invocations


# A complete, healthy image set for KEEP_TAG=v2 plus :latest for every repo.
_KEEP_V2 = [
    "egg-gateway:v2",
    "egg-orchestrator:v2",
    "egg-sandbox:v2",
    "egg-litellm:v2",
    "egg-gateway:latest",
    "egg-orchestrator:latest",
    "egg-sandbox:latest",
    "egg-litellm:latest",
]


class TestSafetyGate:
    def test_skips_reap_when_a_kept_image_is_missing(self, tmp_path: Path) -> None:
        # egg-sandbox:v2 absent -> gate must trip and reap nothing, even though
        # a stale egg-sandbox:v1 (and others) are present.
        present = [r for r in _KEEP_V2 if r != "egg-sandbox:v2"] + [
            "egg-gateway:v1",
            "egg-sandbox:v1",
        ]
        proc, removed, _ = _run_script(tmp_path, present, keep="v2")
        assert proc.returncode == 0, proc.stderr
        assert "skipping stale-tag reap" in proc.stdout
        assert "egg-sandbox:v2" in proc.stdout
        assert removed == []

    def test_missing_docker_is_a_clean_noop(self, tmp_path: Path) -> None:
        # No docker on PATH at all -> exit 0, reclaim nothing.
        bindir = tmp_path / "bin"
        bindir.mkdir()
        # PATH is the empty bindir only -> the script's `command -v docker`
        # resolves nothing. bash is exec'd by absolute path, so it still runs.
        proc = subprocess.run(
            [BASH, str(SCRIPT), "v2"],
            capture_output=True,
            text=True,
            env={"PATH": str(bindir), "HOME": str(tmp_path)},
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "docker not found" in proc.stdout


class TestStaleTagReap:
    def test_reaps_stale_tags_keeps_keep_and_latest(self, tmp_path: Path) -> None:
        present = _KEEP_V2 + [
            "egg-gateway:v1",
            "egg-sandbox:v1",
            "egg-orchestrator:v0",
        ]
        proc, removed, _ = _run_script(tmp_path, present, keep="v2")
        assert proc.returncode == 0, proc.stderr
        assert sorted(removed) == sorted(
            ["egg-gateway:v1", "egg-sandbox:v1", "egg-orchestrator:v0"]
        )
        # Neither the kept tag nor :latest may ever be handed to `image rm`.
        for ref in removed:
            assert not ref.endswith(":v2")
            assert not ref.endswith(":latest")

    def test_no_stale_tags_is_a_noop(self, tmp_path: Path) -> None:
        proc, removed, _ = _run_script(tmp_path, _KEEP_V2, keep="v2")
        assert proc.returncode == 0, proc.stderr
        assert removed == []
        assert "removed 0 stale egg image tag(s)" in proc.stdout

    def test_rm_failure_is_tolerated_and_does_not_abort(self, tmp_path: Path) -> None:
        present = _KEEP_V2 + ["egg-gateway:v1", "egg-sandbox:v1"]
        proc, removed, _ = _run_script(tmp_path, present, keep="v2", fail_ref="egg-gateway:v1")
        # Best-effort: a failed rm must not fail the script, and the OTHER
        # stale tag must still be attempted.
        assert proc.returncode == 0, proc.stderr
        assert "egg-gateway:v1" in removed  # attempted
        assert "egg-sandbox:v1" in removed  # not blocked by the failure above
        assert "rm failed for egg-gateway:v1" in proc.stderr

    def test_build_cache_is_capped(self, tmp_path: Path) -> None:
        proc, _, _ = _run_script(tmp_path, _KEEP_V2, keep="v2")
        assert proc.returncode == 0, proc.stderr
        assert "build cache capped at" in proc.stdout


class TestBuildKitCacheCap:
    """Regression coverage for the BuildKit-cache cap and its fallback path.

    Two invariants are load-bearing:
      1. `--max-used-space` carries the CACHE_MAX value -- defaulting to "20GB"
         and overridable via EGG_DOCKER_CACHE_MAX. Without coverage, a typo or
         dropped variable substitution would silently uncap the cache.
      2. On a docker old enough to lack `--max-used-space`, the script falls
         back to a plain `docker builder prune -f` rather than failing or
         skipping the cache prune entirely. Without coverage, deleting the
         fallback (or inverting the if/elif) would go unnoticed.
    """

    def test_default_cache_max_is_passed_to_builder_prune(self, tmp_path: Path) -> None:
        proc, _, builder_invocations = _run_script(tmp_path, _KEEP_V2, keep="v2")
        assert proc.returncode == 0, proc.stderr
        # Exactly one invocation expected (the --max-used-space path succeeds).
        assert len(builder_invocations) == 1, builder_invocations
        args = builder_invocations[0].split()
        assert "--max-used-space" in args
        idx = args.index("--max-used-space")
        # Default cap is 20GB; the env override path is tested below.
        assert args[idx + 1] == "20GB"
        assert "build cache capped at 20GB" in proc.stdout

    def test_env_override_substitutes_into_builder_prune(self, tmp_path: Path) -> None:
        proc, _, builder_invocations = _run_script(
            tmp_path,
            _KEEP_V2,
            keep="v2",
            extra_env={"EGG_DOCKER_CACHE_MAX": "30GB"},
        )
        assert proc.returncode == 0, proc.stderr
        assert len(builder_invocations) == 1, builder_invocations
        args = builder_invocations[0].split()
        idx = args.index("--max-used-space")
        assert args[idx + 1] == "30GB"
        assert "build cache capped at 30GB" in proc.stdout

    def test_fallback_to_plain_prune_when_max_used_space_unsupported(self, tmp_path: Path) -> None:
        # Simulate a docker old enough to lack --max-used-space: that invocation
        # exits 1, the script must then re-invoke `docker builder prune -f`
        # WITHOUT the flag and report it as a dangling-only prune.
        proc, _, builder_invocations = _run_script(
            tmp_path, _KEEP_V2, keep="v2", fail_max_used_space=True
        )
        assert proc.returncode == 0, proc.stderr
        # Two invocations: the failed --max-used-space attempt + the fallback.
        assert len(builder_invocations) == 2, builder_invocations
        assert "--max-used-space" in builder_invocations[0].split()
        assert "--max-used-space" not in builder_invocations[1].split()
        assert "build cache pruned (dangling; --max-used-space unsupported)" in proc.stdout
        # And the "capped at" line must NOT be printed -- otherwise we'd be
        # claiming a cap that the daemon did not enforce.
        assert "build cache capped at" not in proc.stdout


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
