#!/usr/bin/env bash
#
# reclaim-docker-disk.sh - Bound the *docker* image + BuildKit cache store so a
# subsequent `make k3s-import` does not push the shared root filesystem over
# kubelet's image-GC high-water mark.
#
# Why this exists (and why reap-stale-egg-images.sh is not enough):
# egg keeps TWO image stores on ONE shared root filesystem --
#   * k3s containerd  (/var/lib/rancher/k3s/agent/containerd) -- a few GB
#   * docker          (/var/lib/docker)                       -- tens of GB
# reap-stale-egg-images.sh bounds the FIRST after every deploy. Nothing bounded
# the SECOND, so docker accumulated a full egg image set (incl. the ~12 GB
# egg-sandbox) for every `git describe` tag ever built, plus an unbounded
# BuildKit cache. That docker bloat alone keeps the root fs near
# imageGCHighThresholdPercent (~85%). Then `make k3s-import` does, per image,
# `docker save <img> > /var/tmp/*.tar` (a second ~12 GB copy of sandbox) AND
# `k3s ctr images import` (a third copy, in containerd) -- a transient spike
# that crosses the threshold, and kubelet image GC evicts the freshly-imported,
# not-yet-referenced egg images mid-import. check-egg-images-present.sh then
# aborts the deploy. The operator-facing symptom is a redeploy that "imported
# fine" yet reports the tag's images missing from k3s. Reclaiming docker disk
# BEFORE the import keeps that spike below the line.
#
# Two reclaims, both keyed on the about-to-be-imported KEEP_TAG:
#   1. Stale egg-*:<tag> docker images (tag != KEEP_TAG and != latest).
#   2. BuildKit cache capped to CACHE_MAX (keeps a hot working set for fast
#      incremental builds while bounding unbounded growth).
# A dangling-image prune mops up untagged layers left by rebuilds.
#
# Tag-scoped removal, UNLIKE the containerd reap -- and this difference is
# load-bearing, do not "unify" the two scripts:
#   * `crictl rmi <ref>` removes by image ID: a stale tag that shares an ID with
#     KEEP_TAG would take the current image down with it, so that script needs a
#     digest guard.
#   * `docker image rm <repo:tag>` removes by NAME: if KEEP_TAG and a stale tag
#     share an image ID, removing the stale tag merely untags it and the image
#     survives under KEEP_TAG. So no digest guard is needed here.
# We still require all KEEP_TAG refs present first, so a half-built tag never
# leaves us with nothing to import.
#
# Best-effort: never fail the build/deploy (the Makefile guards the call with
# `|| true`, and each removal below is individually tolerant). Needs NO sudo --
# the docker socket is group-accessible, unlike the root-only containerd socket.
#
set -euo pipefail

: "${1:?usage: $0 <egg-image-tag-to-keep>}"
KEEP_TAG="$1"

# Cap for the BuildKit cache. Big enough to keep a working set of layers for
# fast incremental builds, small enough that it cannot creep the root fs toward
# the GC threshold on its own. Override with EGG_DOCKER_CACHE_MAX (a docker
# byte-size string, e.g. "30GB").
CACHE_MAX="${EGG_DOCKER_CACHE_MAX:-20GB}"

# The egg image set. Keep in sync ACROSS scripts with
# check-egg-images-present.sh, the k3s-import image list, and
# reap-stale-egg-images.sh.
IMAGES=(egg-gateway egg-orchestrator egg-sandbox egg-litellm)

if ! command -v docker >/dev/null 2>&1; then
  echo "==> docker reclaim: docker not found on PATH; nothing to reclaim."
  exit 0
fi

# Safety gate: only reap stale tags once EVERY egg-*:KEEP_TAG image is actually
# built and present. An interrupted/failed build that left, say, egg-sandbox
# untagged for KEEP_TAG must not let us reap the prior egg-sandbox tag -- that
# would strand k3s-import with no sandbox image to save. Mirror of the
# containerd reap's four-image gate.
missing_keep=()
for img in "${IMAGES[@]}"; do
  if ! docker image inspect "${img}:${KEEP_TAG}" >/dev/null 2>&1; then
    missing_keep+=("${img}:${KEEP_TAG}")
  fi
done

if [ "${#missing_keep[@]}" -gt 0 ]; then
  echo "==> docker reclaim: not all egg-*:${KEEP_TAG} images present (${missing_keep[*]}); skipping stale-tag reap."
else
  removed=0
  for img in "${IMAGES[@]}"; do
    # Every tag for this repo except KEEP_TAG and :latest. `docker image ls`
    # with a repo argument lists only that repo; --format keeps it parseable.
    while IFS= read -r ref; do
      [ -z "$ref" ] && continue
      tag="${ref##*:}"
      if [ "$tag" = "$KEEP_TAG" ] || [ "$tag" = "latest" ]; then
        continue
      fi
      if docker image rm "$ref" >/dev/null 2>&1; then
        echo "   reaped $ref"
        removed=$((removed + 1))
      else
        echo "   rm failed for $ref (in use or already gone); leaving it" >&2
      fi
    done < <(docker image ls --format '{{.Repository}}:{{.Tag}}' "$img" 2>/dev/null)
  done
  echo "==> docker reclaim: removed ${removed} stale egg image tag(s) beyond '${KEEP_TAG}'/latest."
fi

# Mop up untagged layers orphaned by rebuilds (an old image ID whose tag was
# moved to a freshly-built image). `docker image prune -f` is HOST-WIDE -- it
# prunes any dangling image on this docker daemon, not just egg-* repos. That
# is safe here only because no docker containers run on this host (k3s runs the
# pods, see file header), so every dangling image is genuinely unreferenced. If
# this script is ever reused on a workstation or any host with non-egg docker
# workloads, narrow this to an egg-repo-scoped loop (e.g. iterate IMAGES and
# run `docker image ls -qf dangling=true <repo>` per repo).
if docker image prune -f >/dev/null 2>&1; then
  echo "==> docker reclaim: pruned dangling images."
fi

# Bound the BuildKit cache. --max-used-space (Buildx 0.17+, shipped with Docker
# Engine 27.x and later) supersedes the now-deprecated --keep-storage. Fall
# back to a plain dangling-cache prune on a docker old enough to lack it.
if docker builder prune -f --max-used-space "$CACHE_MAX" >/dev/null 2>&1; then
  echo "==> docker reclaim: build cache capped at ${CACHE_MAX}."
elif docker builder prune -f >/dev/null 2>&1; then
  echo "==> docker reclaim: build cache pruned (dangling; --max-used-space unsupported)."
fi
