"""Tests for scripts/reap-stale-egg-images.sh.

The script's awk block is the load-bearing logic: it walks a
`sudo k3s ctr images list` listing and decides which egg-* refs are stale
(not the just-deployed tag, not :latest) AND whose manifest digest does
NOT match a kept ref's digest. The digest guard exists because a commit
that does not change an image's build inputs yields a stale tag whose
digest (and image ID) is identical to the current one -- `crictl rmi`
removes by image ID, so removing such a stale tag by name would take the
current image with it. This test extracts the awk program from the script
and runs it directly against synthetic listings to lock the digest-guard
invariant against future "simplifications" that drop it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "reap-stale-egg-images.sh"


def _extract_awk_program() -> str:
    """Pull the awk program literal out of the script.

    Reading the program from the script (rather than hardcoding it here)
    means any future edit to the awk block flows into the test on the next
    run instead of letting a stale hardcoded copy mask a regression.
    """
    src = SCRIPT.read_text()
    m = re.search(r"awk -v keep=\"\$KEEP_TAG\" '(.+?)'\s*<<<", src, re.DOTALL)
    assert m, "could not find awk block in reap-stale-egg-images.sh"
    return m.group(1)


def _run_awk(listing: str, keep: str) -> list[str]:
    """Run the script's awk block against a synthetic listing."""
    awk = shutil.which("awk")
    assert awk, "awk binary not on PATH"
    result = subprocess.run(
        [awk, "-v", f"keep={keep}", _extract_awk_program()],
        input=listing,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _row(ref: str, digest: str) -> str:
    """One `k3s ctr images list` row: REF TYPE DIGEST SIZE PLATFORMS LABELS."""
    return f"{ref}\tapplication/vnd.oci.image.manifest.v1+json\t{digest}\t1.2 GiB\tlinux/amd64\t-"


class TestReapAwkDigestGuard:
    def test_skips_stale_tag_sharing_digest_with_kept_tag(self) -> None:
        """A stale tag whose digest matches the kept tag MUST NOT be reaped.

        crictl rmi removes by image ID; a shared digest means a shared ID,
        and removing the stale tag by name would yank the current image too.
        """
        listing = "\n".join(
            [
                _row("docker.io/library/egg-gateway:v2", "sha256:aaa"),
                _row("docker.io/library/egg-gateway:v1", "sha256:aaa"),  # shared
                _row("docker.io/library/egg-orchestrator:v2", "sha256:bbb"),
                _row("docker.io/library/egg-orchestrator:v1", "sha256:bbb"),  # shared
                _row("docker.io/library/egg-sandbox:v2", "sha256:ccc"),
                _row("docker.io/library/egg-litellm:v2", "sha256:ddd"),
            ]
        )
        reaped = _run_awk(listing, keep="v2")
        assert reaped == []

    def test_reaps_stale_tag_with_distinct_digest(self) -> None:
        """A stale tag whose digest differs from every kept ref IS reaped."""
        listing = "\n".join(
            [
                _row("docker.io/library/egg-gateway:v2", "sha256:aaa"),
                _row("docker.io/library/egg-gateway:v1", "sha256:old1"),
                _row("docker.io/library/egg-orchestrator:v2", "sha256:bbb"),
                _row("docker.io/library/egg-orchestrator:v1", "sha256:old2"),
                _row("docker.io/library/egg-sandbox:v2", "sha256:ccc"),
                _row("docker.io/library/egg-sandbox:v1", "sha256:old3"),
                _row("docker.io/library/egg-litellm:v2", "sha256:ddd"),
                _row("docker.io/library/egg-litellm:v1", "sha256:old4"),
            ]
        )
        reaped = _run_awk(listing, keep="v2")
        assert sorted(reaped) == sorted(
            [
                "docker.io/library/egg-gateway:v1",
                "docker.io/library/egg-orchestrator:v1",
                "docker.io/library/egg-sandbox:v1",
                "docker.io/library/egg-litellm:v1",
            ]
        )

    def test_latest_with_different_digest_is_kept_not_reaped(self) -> None:
        """`:latest` is always kept, even if its digest differs from KEEP_TAG.

        This covers a transient state during import: `docker save` produces
        a `:latest` tag too, and on a freshly-imported v2 the `:latest`
        digest may temporarily match an older content set. The script must
        never reap `:latest` by name -- and its digest must additionally
        protect any other ref that shares it.
        """
        listing = "\n".join(
            [
                _row("docker.io/library/egg-gateway:v2", "sha256:aaa"),
                _row("docker.io/library/egg-gateway:latest", "sha256:zzz"),
                _row("docker.io/library/egg-gateway:v1", "sha256:zzz"),  # shares :latest
                _row("docker.io/library/egg-orchestrator:v2", "sha256:bbb"),
                _row("docker.io/library/egg-sandbox:v2", "sha256:ccc"),
                _row("docker.io/library/egg-litellm:v2", "sha256:ddd"),
            ]
        )
        reaped = _run_awk(listing, keep="v2")
        # :latest itself is filtered out by the `tag == "latest"` branch; v1
        # is protected by sharing :latest's digest.
        assert reaped == []

    def test_no_latest_present_does_not_cause_false_reaps(self) -> None:
        """Absence of `:latest` must not produce spurious reap candidates."""
        listing = "\n".join(
            [
                _row("docker.io/library/egg-gateway:v2", "sha256:aaa"),
                _row("docker.io/library/egg-orchestrator:v2", "sha256:bbb"),
                _row("docker.io/library/egg-sandbox:v2", "sha256:ccc"),
                _row("docker.io/library/egg-litellm:v2", "sha256:ddd"),
            ]
        )
        reaped = _run_awk(listing, keep="v2")
        assert reaped == []

    def test_ignores_non_egg_refs(self) -> None:
        """Refs outside the egg-(gateway|orchestrator|sandbox|litellm) set are untouched."""
        listing = "\n".join(
            [
                _row("docker.io/library/egg-gateway:v2", "sha256:aaa"),
                _row("docker.io/library/egg-orchestrator:v2", "sha256:bbb"),
                _row("docker.io/library/egg-sandbox:v2", "sha256:ccc"),
                _row("docker.io/library/egg-litellm:v2", "sha256:ddd"),
                _row("docker.io/library/postgres:14", "sha256:pg14"),
                _row("docker.io/library/redis:7", "sha256:redis7"),
                _row("docker.io/library/egg-helper:v1", "sha256:helper"),  # not in set
            ]
        )
        reaped = _run_awk(listing, keep="v2")
        assert reaped == []


class TestReapScriptSafetyGuard:
    """End-to-end test of the four-image safety gate via PATH shimming.

    The script gates the whole reap on all four egg-*:KEEP_TAG refs being
    visible. If any one is missing, the script must exit 0 having reaped
    nothing -- otherwise the awk loop would not record that image's
    KEEP_TAG digest and every prior tag of that image would be reaped,
    leaving no image to run the next agent pod.
    """

    def _run_script(
        self, tmp_path: Path, listing: str, keep: str
    ) -> subprocess.CompletedProcess[str]:
        """Run reap-stale-egg-images.sh with `sudo k3s` shimmed to return `listing`."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        # `sudo` shim: drop the leading `sudo` arg and exec the rest from our bindir.
        sudo = bindir / "sudo"
        sudo.write_text(f'#!/usr/bin/env bash\nexec "{bindir}/$1" "${{@:2}}"\n')
        sudo.chmod(0o755)
        # `k3s` shim: only the `ctr images list` form returns the synthetic listing.
        k3s = bindir / "k3s"
        listing_file = tmp_path / "listing.txt"
        listing_file.write_text(listing)
        k3s.write_text(
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "ctr" ] && [ "$2" = "images" ] && [ "$3" = "list" ]; then\n'
            f'  cat "{listing_file}"\n'
            'elif [ "$1" = "crictl" ] && [ "$2" = "rmi" ]; then\n'
            "  exit 0\n"  # pretend every rmi succeeds; not exercised in safety-gate tests
            "else\n"
            '  echo "unexpected k3s args: $*" >&2; exit 99\n'
            "fi\n"
        )
        k3s.chmod(0o755)
        env = {
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
        }
        return subprocess.run(
            ["bash", str(SCRIPT), keep],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_skips_reap_when_kept_tag_partially_missing(self, tmp_path: Path) -> None:
        # egg-sandbox:v2 missing — three out of four present is not enough.
        listing = "\n".join(
            [
                _row("docker.io/library/egg-gateway:v2", "sha256:aaa"),
                _row("docker.io/library/egg-gateway:v1", "sha256:old"),
                _row("docker.io/library/egg-orchestrator:v2", "sha256:bbb"),
                _row("docker.io/library/egg-sandbox:v1", "sha256:oldsand"),
                _row("docker.io/library/egg-litellm:v2", "sha256:ddd"),
            ]
        )
        result = self._run_script(tmp_path, listing, keep="v2")
        assert result.returncode == 0, result.stderr
        assert "skipping" in result.stdout
        assert "egg-sandbox:v2" in result.stdout
        # No per-ref reap line emitted ("   reaped docker.io/...").
        assert "   reaped " not in result.stdout

    def test_proceeds_with_reap_when_all_four_kept_refs_present(self, tmp_path: Path) -> None:
        listing = "\n".join(
            [
                _row("docker.io/library/egg-gateway:v2", "sha256:aaa"),
                _row("docker.io/library/egg-gateway:v1", "sha256:old1"),
                _row("docker.io/library/egg-orchestrator:v2", "sha256:bbb"),
                _row("docker.io/library/egg-sandbox:v2", "sha256:ccc"),
                _row("docker.io/library/egg-litellm:v2", "sha256:ddd"),
            ]
        )
        result = self._run_script(tmp_path, listing, keep="v2")
        assert result.returncode == 0, result.stderr
        assert "reaped docker.io/library/egg-gateway:v1" in result.stdout


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
