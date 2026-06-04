#!/usr/bin/env bash
#
# check-egg-images-present.sh - Fail fast, BEFORE `make deploy` mutates the
# cluster, when the egg-*:<EGG_IMAGE_TAG> images are not in k3s's containerd.
#
# EGG_IMAGE_TAG is `git describe --always --dirty`, so it tracks HEAD and
# changes on every commit, pull, rebase, or branch checkout. `make redeploy`
# builds, imports, and deploys on one self-consistent tag — but a bare
# `make deploy` after HEAD has moved references a tag whose images were never
# imported.
#
# await-egg-deploy.sh already detects that, but only AFTER `kubectl apply` has
# repointed the live deployments at the missing tag — so a failed bare deploy
# leaves the running cluster broken until the operator runs `make redeploy`.
# This pre-flight runs the same authoritative containerd check that k3s-import
# uses (sudo k3s ctr images list), so deploy aborts with the redeploy hint
# without touching the cluster.
#
# The check is branch-agnostic: it only asks whether images for the *current*
# tag are present, regardless of how HEAD arrived there. Requires sudo because
# k3s's containerd socket is root-only — same as k3s-import.
#
set -euo pipefail

: "${1:?usage: $0 <egg-image-tag>}"
TAG="$1"

# egg-built images rewritten onto the manifests by `make deploy`. Keep in sync
# with the sed rewrites in the deploy target and the k3s-import image list.
IMAGES=(egg-gateway egg-orchestrator egg-sandbox egg-litellm)

# One listing, reused for every image — k3s ctr is the slow part and needs sudo.
present=$(sudo k3s ctr images list -q)

missing=()
for img in "${IMAGES[@]}"; do
  grep -qx "docker.io/library/$img:$TAG" <<<"$present" || missing+=("$img:$TAG")
done

if [ "${#missing[@]}" -gt 0 ]; then
  echo "ERROR: egg-system images for tag '${TAG}' are not in k3s: ${missing[*]}" >&2
  echo "       Two known causes:" >&2
  echo "       1. HEAD moved (commit/pull/rebase/checkout) since your last build, so" >&2
  echo "          'make deploy' alone references a tag that was never built+imported." >&2
  echo "          Fix: 'make redeploy' rebuilds + re-imports + deploys on the current tag." >&2
  echo "       2. 'make redeploy' DID import these, but kubelet image GC evicted them" >&2
  echo "          before 'deploy' repointed the pods at the new tag -- they sit" >&2
  echo "          unreferenced until then, so under disk pressure (root fs over" >&2
  echo "          imageGCHighThresholdPercent, ~85%) they get collected mid-run." >&2
  echo "          The disk hog is the DOCKER store (/var/lib/docker, tens of GB of" >&2
  echo "          old egg tags + BuildKit cache), NOT k3s's containerd (~4 GB) --" >&2
  echo "          'docker save' + 'ctr import' briefly hold the sandbox image in" >&2
  echo "          BOTH stores plus a tar in /var/tmp on the same root fs." >&2
  echo "          Fix: reclaim docker disk, then redeploy (which re-imports):" >&2
  echo "              scripts/reclaim-docker-disk.sh \"\$(git describe --always --dirty)\"" >&2
  echo "              df -h /                       # should sit well under 80%" >&2
  echo "              make redeploy" >&2
  echo "          k3s-import now runs reclaim-docker-disk.sh automatically before" >&2
  echo "          every import; if the spike still trips GC, lower the BuildKit cache" >&2
  echo "          cap with EGG_DOCKER_CACHE_MAX (default 20GB)." >&2
  exit 1
fi

echo "All egg-system images for tag '${TAG}' are present in k3s."
