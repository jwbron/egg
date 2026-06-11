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
    # The script feeds KEEP_TAG / IMAGE_RE / PREFIX_ALT_RE / AUTH_REG_RE /
    # AUTH_BARE_RE to awk via the environment (NOT -v: gawk escape-processes
    # -v values, and the `\.` in "docker\.io/library/" would both warn and
    # lose its backslash). Anchor on AUTH_BARE_RE so we pick the containerd
    # reap block specifically, not the later docker-store awk.
    m = re.search(
        r"AUTH_BARE_RE=\"\$AUTH_BARE_RE\"\s+awk\s+'(.+?)'\s*<<<",
        src,
        re.DOTALL,
    )
    assert m, "could not find awk block in reap-stale-egg-images.sh"
    return m.group(1)


def _extract_images() -> list[str]:
    """Pull the IMAGES bash array out of the script.

    Same rationale as _extract_awk_program: a future edit to IMAGES (adding
    a fifth image, renaming one) flows into the test rather than letting a
    stale hardcoded list mask a regression.
    """
    src = SCRIPT.read_text()
    m = re.search(r"^IMAGES=\(([^)]+)\)", src, re.MULTILINE)
    assert m, "could not find IMAGES=(...) in reap-stale-egg-images.sh"
    return m.group(1).split()


def _run_awk(
    listing: str,
    keep: str,
    *,
    registry: str = "",
    registry_subset: tuple[str, ...] = (),
) -> list[str]:
    """Run the script's awk block against a synthetic listing.

    With `registry=""` and `registry_subset=()` this mirrors the no-registry
    case: every egg image is on the legacy docker.io/library/ prefix, and
    the registry-authority branch is the never-matching '^$' placeholder.

    With `registry` set (e.g. ``"localhost:5000"``) and `registry_subset`
    naming the registry-mode images (e.g. ``("egg-gateway", "egg-orchestrator",
    "egg-litellm")``), this mirrors hybrid mode: registry-subset images are
    authoritative as ``<registry>/<image>:<tag>`` while bare-subset images
    (the sandbox, by default) are authoritative as ``docker.io/library/...``.
    The PREFIX_ALT_RE / AUTH_REG_RE / AUTH_BARE_RE construction below mirrors
    reap-stale-egg-images.sh:88-114 — keep both halves in sync.
    """
    awk = shutil.which("awk")
    assert awk, "awk binary not on PATH"
    images = _extract_images()
    image_re = "|".join(images)
    legacy_prefix_re = r"docker\.io/library/"
    if registry:
        registry_prefix_re = re.escape(f"{registry}/")
        prefix_alt_re = f"{registry_prefix_re}|{legacy_prefix_re}"
    else:
        registry_prefix_re = ""
        prefix_alt_re = legacy_prefix_re
    reg_img_alt = "|".join(i for i in images if i in registry_subset)
    bare_img_alt = "|".join(i for i in images if i not in registry_subset)
    auth_reg_re = f"^{registry_prefix_re}({reg_img_alt}):" if reg_img_alt else "^$"
    auth_bare_re = f"^{legacy_prefix_re}({bare_img_alt}):" if bare_img_alt else "^$"
    env = {
        "KEEP_TAG": keep,
        "IMAGE_RE": image_re,
        "PREFIX_ALT_RE": prefix_alt_re,
        "AUTH_REG_RE": auth_reg_re,
        "AUTH_BARE_RE": auth_bare_re,
        "PATH": "/usr/bin:/bin",
    }
    result = subprocess.run(
        [awk, _extract_awk_program()],
        input=listing,
        capture_output=True,
        text=True,
        check=True,
        env=env,
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


class TestReapHybridMode:
    """Hybrid-mode reap: registry-subset images are authoritative as
    ``<registry>/<image>:<tag>``; bare-subset images (whatever the operator
    excludes from ``EGG_REGISTRY_IMAGES``) are authoritative as
    ``docker.io/library/<image>:<tag>``. A ref in the NON-authoritative form
    for its image (e.g. a bare ``docker.io/library/egg-gateway:<tag>`` left
    over from a pre-registry deploy, or a registry-qualified leftover for an
    excluded image) is a reap candidate by definition; the digest guard
    then spares any candidate that shares an image ID with a kept ref.

    Post-#3109 the default is all-registry-authoritative (covered by
    :class:`TestReapAllRegistryMode`); this class exercises the
    operator-opt-out config — ``EGG_REGISTRY_IMAGES = egg-gateway
    egg-orchestrator egg-litellm``, excluding egg-sandbox so it publishes via
    save+import. The path remains supported and is what an operator who
    wants to keep the sandbox image off the loopback registry would land
    on. These tests pin that split's behavior against the awk extractor's
    match/auth-ref logic so a regression in any of registry-subset
    authoritative form, bare-prefix non-authoritative form on subset images,
    mixed authority across IMAGES[], or the digest guard's interaction with
    non-authoritative refs that match `match_re` but neither `auth_reg_re`
    nor `auth_bare_re`, silently fails the test instead of silently fails
    the reap (the failure mode is no-reap = slow disk fill-up, not
    destructive — exactly the creeping regression #2999 was about).
    """

    REGISTRY = "localhost:5000"
    # Operator-opt-out subset: sandbox stays on import (bare-authoritative).
    SUBSET = ("egg-gateway", "egg-orchestrator", "egg-litellm")

    def _baseline_kept_refs(self, keep: str) -> list[str]:
        """The four authoritative kept refs under the default hybrid split."""
        return [
            _row(f"{self.REGISTRY}/egg-gateway:{keep}", "sha256:aaa"),
            _row(f"{self.REGISTRY}/egg-orchestrator:{keep}", "sha256:bbb"),
            _row(f"docker.io/library/egg-sandbox:{keep}", "sha256:ccc"),
            _row(f"{self.REGISTRY}/egg-litellm:{keep}", "sha256:ddd"),
        ]

    def test_subset_image_bare_leftover_sharing_digest_is_spared(self) -> None:
        """Registry-subset image kept authoritatively; a bare leftover with the
        SAME digest is non-authoritative but shares the image ID, so the digest
        guard must spare it — `crictl rmi` by name would yank the current image.
        """
        listing = "\n".join(
            [
                *self._baseline_kept_refs("v2"),
                # Bare leftover for a registry-subset image, same digest as kept.
                _row("docker.io/library/egg-gateway:v2", "sha256:aaa"),
            ]
        )
        reaped = _run_awk(listing, keep="v2", registry=self.REGISTRY, registry_subset=self.SUBSET)
        assert reaped == []

    def test_subset_image_bare_stale_with_distinct_digest_is_reaped(self) -> None:
        """Registry-subset image kept authoritatively; a bare leftover with a
        DIFFERENT digest is non-authoritative AND digest-distinct — exactly the
        case the new auth-aware logic exists to catch (pre-registry deploy left
        a stale bare ref behind, no longer shared with any kept ref).
        """
        listing = "\n".join(
            [
                *self._baseline_kept_refs("v2"),
                _row("docker.io/library/egg-gateway:v1", "sha256:old"),
            ]
        )
        reaped = _run_awk(listing, keep="v2", registry=self.REGISTRY, registry_subset=self.SUBSET)
        assert reaped == ["docker.io/library/egg-gateway:v1"]

    def test_bare_subset_image_registry_leftover_sharing_digest_is_spared(self) -> None:
        """The sandbox is authoritative as bare under the default subset; a
        registry-qualified leftover with the SAME digest is non-authoritative
        but shares the image ID, so the digest guard must spare it.
        """
        listing = "\n".join(
            [
                *self._baseline_kept_refs("v2"),
                _row(f"{self.REGISTRY}/egg-sandbox:v2", "sha256:ccc"),
            ]
        )
        reaped = _run_awk(listing, keep="v2", registry=self.REGISTRY, registry_subset=self.SUBSET)
        assert reaped == []

    def test_bare_subset_image_registry_leftover_distinct_digest_is_reaped(self) -> None:
        """Sandbox authoritative as bare; a registry-qualified leftover with a
        DIFFERENT digest must be reaped — this is the symmetric case to the
        registry-subset-bare-stale path.
        """
        listing = "\n".join(
            [
                *self._baseline_kept_refs("v2"),
                _row(f"{self.REGISTRY}/egg-sandbox:v1", "sha256:oldsand"),
            ]
        )
        reaped = _run_awk(listing, keep="v2", registry=self.REGISTRY, registry_subset=self.SUBSET)
        assert reaped == [f"{self.REGISTRY}/egg-sandbox:v1"]

    def test_mixed_stale_refs_across_all_images(self) -> None:
        """Combined: prior-deploy KEEP_TAG=v1 leftovers for every image, in
        each image's NON-authoritative prefix form. None share digests with
        the kept refs, so all should be reaped.
        """
        listing = "\n".join(
            [
                *self._baseline_kept_refs("v2"),
                # Non-authoritative stale refs for registry-subset images
                _row("docker.io/library/egg-gateway:v1", "sha256:old-g"),
                _row("docker.io/library/egg-orchestrator:v1", "sha256:old-o"),
                _row("docker.io/library/egg-litellm:v1", "sha256:old-l"),
                # Non-authoritative stale ref for the bare-subset image
                _row(f"{self.REGISTRY}/egg-sandbox:v1", "sha256:old-s"),
            ]
        )
        reaped = _run_awk(listing, keep="v2", registry=self.REGISTRY, registry_subset=self.SUBSET)
        assert sorted(reaped) == sorted(
            [
                "docker.io/library/egg-gateway:v1",
                "docker.io/library/egg-orchestrator:v1",
                "docker.io/library/egg-litellm:v1",
                f"{self.REGISTRY}/egg-sandbox:v1",
            ]
        )

    def test_authoritative_stale_refs_are_reaped(self) -> None:
        """Authoritative stale refs (registry-qualified for subset, bare for
        sandbox) with distinct digests are the canonical reap path — exercising
        it here under hybrid prefixes ensures the auth-aware regex doesn't
        accidentally over-protect refs that ARE in the authoritative form but
        carry a stale tag.
        """
        listing = "\n".join(
            [
                *self._baseline_kept_refs("v2"),
                _row(f"{self.REGISTRY}/egg-gateway:v1", "sha256:old-g"),
                _row(f"{self.REGISTRY}/egg-orchestrator:v1", "sha256:old-o"),
                _row("docker.io/library/egg-sandbox:v1", "sha256:old-s"),
                _row(f"{self.REGISTRY}/egg-litellm:v1", "sha256:old-l"),
            ]
        )
        reaped = _run_awk(listing, keep="v2", registry=self.REGISTRY, registry_subset=self.SUBSET)
        assert sorted(reaped) == sorted(
            [
                f"{self.REGISTRY}/egg-gateway:v1",
                f"{self.REGISTRY}/egg-orchestrator:v1",
                "docker.io/library/egg-sandbox:v1",
                f"{self.REGISTRY}/egg-litellm:v1",
            ]
        )

    def test_latest_authoritative_protects_shared_digest(self) -> None:
        """:latest on the authoritative prefix protects refs sharing its digest,
        same invariant as no-registry mode but now exercised under the hybrid
        prefix split.
        """
        listing = "\n".join(
            [
                *self._baseline_kept_refs("v2"),
                _row(f"{self.REGISTRY}/egg-gateway:latest", "sha256:zzz"),
                # Old bare ref shares :latest's digest — must be spared.
                _row("docker.io/library/egg-gateway:v1", "sha256:zzz"),
            ]
        )
        reaped = _run_awk(listing, keep="v2", registry=self.REGISTRY, registry_subset=self.SUBSET)
        assert reaped == []


class TestReapAllRegistryMode:
    """Default reap (post-#3109): every image in ``EGG_REGISTRY_IMAGES``, so
    every image is authoritative as ``<registry>/<image>:<tag>``. The migration
    path real operators hit on the first redeploy after the default flips is
    bare ``docker.io/library/egg-sandbox:<tag>`` leftovers from pre-#3109
    deploys — those are non-authoritative under the new default and must be
    reaped, except when they still share an image ID with a kept registry-
    qualified ref (digest guard, same as the other modes).

    These tests pin the awk extractor on the all-registry default so a future
    edit that re-narrows the authoritative-form regexes silently fails the
    test instead of leaving the bare-leftover refs to fill the disk.
    """

    REGISTRY = "localhost:5000"
    # Default subset for this PR: all four images registry-authoritative.
    SUBSET = ("egg-gateway", "egg-orchestrator", "egg-sandbox", "egg-litellm")

    def _baseline_kept_refs(self, keep: str) -> list[str]:
        """The four authoritative kept refs under the all-registry default."""
        return [
            _row(f"{self.REGISTRY}/egg-gateway:{keep}", "sha256:aaa"),
            _row(f"{self.REGISTRY}/egg-orchestrator:{keep}", "sha256:bbb"),
            _row(f"{self.REGISTRY}/egg-sandbox:{keep}", "sha256:ccc"),
            _row(f"{self.REGISTRY}/egg-litellm:{keep}", "sha256:ddd"),
        ]

    def test_sandbox_bare_leftover_sharing_digest_is_spared(self) -> None:
        """First redeploy after the default flip: sandbox is now authoritative
        as ``<registry>/egg-sandbox:<tag>``, but a bare leftover from the
        previous deploy shares its digest. ``crictl rmi`` by name would yank
        the current image, so the digest guard must spare it.
        """
        listing = "\n".join(
            [
                *self._baseline_kept_refs("v2"),
                # Bare leftover from pre-#3109 deploys, same digest as kept.
                _row("docker.io/library/egg-sandbox:v2", "sha256:ccc"),
            ]
        )
        reaped = _run_awk(listing, keep="v2", registry=self.REGISTRY, registry_subset=self.SUBSET)
        assert reaped == []

    def test_sandbox_bare_leftover_distinct_digest_is_reaped(self) -> None:
        """Second redeploy after the default flip: the sandbox tag has rolled
        on, so the pre-#3109 bare leftover no longer shares its digest with
        any kept ref. It is non-authoritative AND digest-distinct — reap it.
        """
        listing = "\n".join(
            [
                *self._baseline_kept_refs("v2"),
                _row("docker.io/library/egg-sandbox:v1", "sha256:oldsand"),
            ]
        )
        reaped = _run_awk(listing, keep="v2", registry=self.REGISTRY, registry_subset=self.SUBSET)
        assert reaped == ["docker.io/library/egg-sandbox:v1"]

    def test_bare_leftovers_across_all_images_distinct_digests_reaped(self) -> None:
        """The full migration shape: every image has a bare-prefix stale ref
        from a pre-registry deploy, none of which share digests with the kept
        registry-qualified refs. All four must be reaped.
        """
        listing = "\n".join(
            [
                *self._baseline_kept_refs("v2"),
                _row("docker.io/library/egg-gateway:v1", "sha256:old-g"),
                _row("docker.io/library/egg-orchestrator:v1", "sha256:old-o"),
                _row("docker.io/library/egg-sandbox:v1", "sha256:old-s"),
                _row("docker.io/library/egg-litellm:v1", "sha256:old-l"),
            ]
        )
        reaped = _run_awk(listing, keep="v2", registry=self.REGISTRY, registry_subset=self.SUBSET)
        assert sorted(reaped) == sorted(
            [
                "docker.io/library/egg-gateway:v1",
                "docker.io/library/egg-orchestrator:v1",
                "docker.io/library/egg-sandbox:v1",
                "docker.io/library/egg-litellm:v1",
            ]
        )

    def test_authoritative_stale_refs_are_reaped(self) -> None:
        """Authoritative (registry-qualified) stale refs with distinct digests
        are the canonical reap path: a previous tag's images that haven't been
        garbage-collected from containerd yet.
        """
        listing = "\n".join(
            [
                *self._baseline_kept_refs("v2"),
                _row(f"{self.REGISTRY}/egg-gateway:v1", "sha256:old-g"),
                _row(f"{self.REGISTRY}/egg-orchestrator:v1", "sha256:old-o"),
                _row(f"{self.REGISTRY}/egg-sandbox:v1", "sha256:old-s"),
                _row(f"{self.REGISTRY}/egg-litellm:v1", "sha256:old-l"),
            ]
        )
        reaped = _run_awk(listing, keep="v2", registry=self.REGISTRY, registry_subset=self.SUBSET)
        assert sorted(reaped) == sorted(
            [
                f"{self.REGISTRY}/egg-gateway:v1",
                f"{self.REGISTRY}/egg-orchestrator:v1",
                f"{self.REGISTRY}/egg-sandbox:v1",
                f"{self.REGISTRY}/egg-litellm:v1",
            ]
        )

    def test_latest_authoritative_protects_bare_leftover_sharing_digest(self) -> None:
        """``:latest`` on the authoritative (registry-qualified) prefix protects
        a bare leftover sharing its digest — the digest guard must look across
        the prefix boundary, otherwise a fresh push that leaves :latest pointing
        at content the pre-#3109 bare ref still shares would lose the bare ref's
        underlying image ID with it.
        """
        listing = "\n".join(
            [
                *self._baseline_kept_refs("v2"),
                _row(f"{self.REGISTRY}/egg-sandbox:latest", "sha256:zzz"),
                _row("docker.io/library/egg-sandbox:v1", "sha256:zzz"),
            ]
        )
        reaped = _run_awk(listing, keep="v2", registry=self.REGISTRY, registry_subset=self.SUBSET)
        assert reaped == []


class TestReapScriptSafetyGuard:
    """End-to-end test of the four-image safety gate via PATH shimming.

    The script gates the whole reap on all four egg-*:KEEP_TAG refs being
    visible IN EACH IMAGE'S AUTHORITATIVE PREFIX FORM. If any one is
    missing, the script must exit 0 having reaped nothing -- otherwise
    the awk loop would not record that image's KEEP_TAG digest and every
    prior tag of that image would be reaped, leaving no image to run the
    next agent pod.

    The per-image expected-prefix branch
    (``scripts/reap-stale-egg-images.sh:128-137``) picks registry-qualified
    for ``is_registry_image`` images and bare for the rest. Both branches
    are exercised end-to-end here: no-registry mode (every image bare-
    authoritative) and all-registry mode (every image registry-
    authoritative, the post-#3109 default).
    """

    def _run_script(
        self,
        tmp_path: Path,
        listing: str,
        keep: str,
        *,
        registry: str = "",
        registry_subset: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        """Run reap-stale-egg-images.sh with `sudo k3s` shimmed to return `listing`.

        Pass ``registry`` + ``registry_subset`` to drive the all-registry
        safety-gate path; the values are forwarded to the script as positional
        args 2+ (the same form the Makefile uses). The registry-side reap that
        runs after the containerd reap exits cleanly when ``localhost:5000``
        isn't a real registry (the ``curl`` probe at
        ``scripts/reap-stale-egg-images.sh:281`` returns non-zero and the
        script ``exit 0``s), so the safety-gate path under test isn't
        contaminated by side effects from the test environment.
        """
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
        args = ["bash", str(SCRIPT), keep]
        if registry:
            args.append(registry)
            args.extend(registry_subset)
        return subprocess.run(
            args,
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

    def test_all_registry_mode_proceeds_when_all_registry_kept_refs_present(
        self, tmp_path: Path
    ) -> None:
        """All four images authoritative as ``localhost:5000/<image>:v2`` —
        the per-image expected-prefix branch picks the registry prefix for
        every image, the safety gate passes, and a bare leftover with a
        distinct digest is reaped end-to-end.
        """
        listing = "\n".join(
            [
                _row("localhost:5000/egg-gateway:v2", "sha256:aaa"),
                _row("localhost:5000/egg-orchestrator:v2", "sha256:bbb"),
                _row("localhost:5000/egg-sandbox:v2", "sha256:ccc"),
                _row("localhost:5000/egg-litellm:v2", "sha256:ddd"),
                # Bare leftover with distinct digest — must be reaped end-to-end.
                _row("docker.io/library/egg-sandbox:v1", "sha256:oldsand"),
            ]
        )
        result = self._run_script(
            tmp_path,
            listing,
            keep="v2",
            registry="localhost:5000",
            registry_subset=(
                "egg-gateway",
                "egg-orchestrator",
                "egg-sandbox",
                "egg-litellm",
            ),
        )
        assert result.returncode == 0, result.stderr
        assert "reaped docker.io/library/egg-sandbox:v1" in result.stdout
        # Safety gate didn't fire — no containerd "skipping" line.
        assert "containerd reap: not all" not in result.stdout

    def test_all_registry_mode_skips_reap_when_kept_ref_only_at_bare_prefix(
        self, tmp_path: Path
    ) -> None:
        """Under the post-#3109 default every image's authoritative form is
        ``localhost:5000/<image>:<tag>``. If a registry-subset image's KEEP_TAG
        is visible only at the legacy bare prefix (e.g. a push failed mid-flight,
        or the sandbox import path was never re-run after the default flipped),
        the per-image expected-prefix branch sees the authoritative form as
        missing and the safety gate must fire. Otherwise the awk loop would
        not record the sandbox digest, and every prior sandbox tag would be
        reaped — exactly the next-pod-cannot-find-an-image failure the gate
        exists to prevent.
        """
        listing = "\n".join(
            [
                _row("localhost:5000/egg-gateway:v2", "sha256:aaa"),
                _row("localhost:5000/egg-orchestrator:v2", "sha256:bbb"),
                # Sandbox KEEP_TAG only at bare prefix — wrong form under the default.
                _row("docker.io/library/egg-sandbox:v2", "sha256:ccc"),
                _row("docker.io/library/egg-sandbox:v1", "sha256:oldsand"),
                _row("localhost:5000/egg-litellm:v2", "sha256:ddd"),
            ]
        )
        result = self._run_script(
            tmp_path,
            listing,
            keep="v2",
            registry="localhost:5000",
            registry_subset=(
                "egg-gateway",
                "egg-orchestrator",
                "egg-sandbox",
                "egg-litellm",
            ),
        )
        assert result.returncode == 0, result.stderr
        assert "skipping" in result.stdout
        assert "egg-sandbox:v2" in result.stdout
        # No per-ref containerd reap line emitted.
        assert "   reaped " not in result.stdout


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
