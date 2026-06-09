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
    bindir: Path, images_file: Path, removed_file: Path, fail_ref: str = ""
) -> None:
    """Write a `docker` shim covering the subcommands the script invokes.

    - `image inspect <ref>`        -> exit 0 iff <ref> is in images_file
    - `image ls --format .. <repo>`-> print images_file lines for <repo>
    - `image rm <ref>`             -> record <ref>; exit 1 iff <ref>==fail_ref
    - `image prune -f`             -> no-op success
    - `builder prune ...`          -> no-op success
    """
    docker = bindir / "docker"
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
        '  "builder prune") exit 0 ;;\n'
        '  *) echo "unexpected docker args: $*" >&2; exit 99 ;;\n'
        "esac\n"
    )
    docker.chmod(0o755)


def _run_script(
    tmp_path: Path, present: list[str], keep: str, fail_ref: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run reclaim-docker-disk.sh with `docker` shimmed to `present`.

    Returns (completed_process, list_of_refs_the_script_asked_to_remove).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    images_file = tmp_path / "images.txt"
    images_file.write_text("\n".join(present) + "\n")
    removed_file = tmp_path / "removed.txt"
    removed_file.write_text("")
    _write_docker_shim(bindir, images_file, removed_file, fail_ref=fail_ref)

    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(
        [BASH, str(SCRIPT), keep],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    removed = [line for line in removed_file.read_text().splitlines() if line]
    return proc, removed


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
        proc, removed = _run_script(tmp_path, present, keep="v2")
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
        proc, removed = _run_script(tmp_path, present, keep="v2")
        assert proc.returncode == 0, proc.stderr
        assert sorted(removed) == sorted(
            ["egg-gateway:v1", "egg-sandbox:v1", "egg-orchestrator:v0"]
        )
        # Neither the kept tag nor :latest may ever be handed to `image rm`.
        for ref in removed:
            assert not ref.endswith(":v2")
            assert not ref.endswith(":latest")

    def test_no_stale_tags_is_a_noop(self, tmp_path: Path) -> None:
        proc, removed = _run_script(tmp_path, _KEEP_V2, keep="v2")
        assert proc.returncode == 0, proc.stderr
        assert removed == []
        assert "removed 0 stale egg image tag(s)" in proc.stdout

    def test_rm_failure_is_tolerated_and_does_not_abort(self, tmp_path: Path) -> None:
        present = _KEEP_V2 + ["egg-gateway:v1", "egg-sandbox:v1"]
        proc, removed = _run_script(tmp_path, present, keep="v2", fail_ref="egg-gateway:v1")
        # Best-effort: a failed rm must not fail the script, and the OTHER
        # stale tag must still be attempted.
        assert proc.returncode == 0, proc.stderr
        assert "egg-gateway:v1" in removed  # attempted
        assert "egg-sandbox:v1" in removed  # not blocked by the failure above
        assert "rm failed for egg-gateway:v1" in proc.stderr

    def test_build_cache_is_capped(self, tmp_path: Path) -> None:
        proc, _ = _run_script(tmp_path, _KEEP_V2, keep="v2")
        assert proc.returncode == 0, proc.stderr
        assert "build cache capped at" in proc.stdout


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
