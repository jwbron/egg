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
  echo "          Fix: reclaim space in k3s's containerd -- NOT docker, a separate" >&2
  echo "          store 'docker system prune' does not touch -- then redeploy, which" >&2
  echo "          re-imports everything:" >&2
  echo "              sudo k3s crictl rmi --prune   # safe immediately before redeploy" >&2
  echo "          'df -h /' should sit well under 80% before the import. A green deploy" >&2
  echo "          now reaps older egg tags automatically (reap-stale-egg-images.sh)." >&2
  exit 1
fi

echo "All egg-system images for tag '${TAG}' are present in k3s."
