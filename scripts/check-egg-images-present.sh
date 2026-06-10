#!/usr/bin/env bash
#
# check-egg-images-present.sh - Fail fast, BEFORE `make deploy` mutates the
# cluster, when the egg images for <EGG_IMAGE_TAG> are not where the cluster
# will look for them.
#
# EGG_IMAGE_TAG is `git describe --always --dirty`, so it tracks HEAD and
# changes on every commit, pull, rebase, or branch checkout. `make redeploy`
# builds, publishes, and deploys on one self-consistent tag — but a bare
# `make deploy` after HEAD has moved references a tag that was never
# published.
#
# await-egg-deploy.sh already detects that, but only AFTER `kubectl apply` has
# repointed the live deployments at the missing tag — so a failed bare deploy
# leaves the running cluster broken until the operator runs `make redeploy`.
#
# Publishing is split (issue #2999): the registry-subset images (args 3+,
# from EGG_REGISTRY_IMAGES — by default everything except the private-content
# egg-sandbox) are pulled by the cluster from the loopback registry, so the
# registry's HTTP API is their source of truth. The remaining images are
# save+imported, so k3s's containerd is theirs. With no registry (arg 2
# empty, e.g. CI) every image is checked in containerd — the pre-registry
# behavior, unchanged.
#
set -euo pipefail

usage="usage: $0 <egg-image-tag> [registry-host:port] [registry-image]..."
: "${1:?$usage}"
TAG="$1"
REGISTRY="${2:-}"
shift
[ "$#" -gt 0 ] && shift

# Full egg image set rewritten onto the manifests by `make deploy`. Keep in
# sync with EGG_ALL_IMAGES in the Makefile and reap-stale-egg-images.sh.
ALL_IMAGES=(egg-gateway egg-orchestrator egg-sandbox egg-litellm)

# Split ALL_IMAGES into the registry-checked subset and the containerd-checked
# remainder. Without a registry the subset is forced empty.
REGISTRY_IMAGES=()
CONTAINERD_IMAGES=()
for img in "${ALL_IMAGES[@]}"; do
  in_registry=0
  if [ -n "$REGISTRY" ]; then
    for r in "$@"; do
      [ "$r" = "$img" ] && in_registry=1 && break
    done
  fi
  if [ "$in_registry" -eq 1 ]; then
    REGISTRY_IMAGES+=("$img")
  else
    CONTAINERD_IMAGES+=("$img")
  fi
done

missing_registry=()
missing_containerd=()

if [ "${#REGISTRY_IMAGES[@]}" -gt 0 ]; then
  if ! curl -fsS "http://${REGISTRY}/v2/" >/dev/null 2>&1; then
    echo "ERROR: local registry at http://${REGISTRY}/v2/ is not answering, so the" >&2
    echo "       cluster could not pull egg images even if they were published." >&2
    echo "       One-time setup: 'make registry-setup'. If the egg-registry container" >&2
    echo "       exists but is stopped: 'docker start egg-registry'." >&2
    exit 1
  fi

  # HEAD against the manifest endpoint: 200 iff the tag exists. The Accept
  # header must enumerate the modern manifest types or the registry answers
  # 404 for images pushed with current docker/buildkit.
  accept='application/vnd.docker.distribution.manifest.v2+json'
  accept="${accept}, application/vnd.docker.distribution.manifest.list.v2+json"
  accept="${accept}, application/vnd.oci.image.manifest.v1+json"
  accept="${accept}, application/vnd.oci.image.index.v1+json"

  for img in "${REGISTRY_IMAGES[@]}"; do
    if ! curl -fsS --head -H "Accept: ${accept}" \
      "http://${REGISTRY}/v2/${img}/manifests/${TAG}" >/dev/null 2>&1; then
      missing_registry+=("$img:$TAG")
    fi
  done
fi

if [ "${#CONTAINERD_IMAGES[@]}" -gt 0 ]; then
  # One listing, reused for every image — k3s ctr is the slow part and needs
  # sudo because k3s's containerd socket is root-only, same as k3s-import.
  present=$(sudo k3s ctr images list -q)
  for img in "${CONTAINERD_IMAGES[@]}"; do
    grep -qx "docker.io/library/$img:$TAG" <<<"$present" || missing_containerd+=("$img:$TAG")
  done
fi

if [ "${#missing_registry[@]}" -gt 0 ]; then
  echo "ERROR: egg images for tag '${TAG}' are not in the local registry (${REGISTRY}): ${missing_registry[*]}" >&2
  echo "       HEAD moved (commit/pull/rebase/checkout) since your last build, so" >&2
  echo "       'make deploy' alone references a tag that was never built+pushed." >&2
  echo "       Fix: 'make redeploy' rebuilds + publishes + deploys on the current tag." >&2
fi

if [ "${#missing_containerd[@]}" -gt 0 ]; then
  echo "ERROR: egg-system images for tag '${TAG}' are not in k3s: ${missing_containerd[*]}" >&2
  echo "       Two known causes:" >&2
  echo "       1. HEAD moved (commit/pull/rebase/checkout) since your last build, so" >&2
  echo "          'make deploy' alone references a tag that was never built+imported." >&2
  echo "          Fix: 'make redeploy' rebuilds + publishes + deploys on the current tag." >&2
  echo "       2. 'make redeploy' DID import these, but kubelet image GC evicted them" >&2
  echo "          before 'deploy' repointed the pods at the new tag -- they sit" >&2
  echo "          unreferenced until then, so under disk pressure (root fs over" >&2
  echo "          imageGCHighThresholdPercent, ~85%) they get collected mid-run." >&2
  echo "          Fix: reclaim containerd space -- NOT docker, a separate store" >&2
  echo "          'docker system prune' does not touch -- then redeploy:" >&2
  echo "              sudo k3s crictl rmi --prune   # safe immediately before redeploy" >&2
  echo "          On btrfs also run 'make btrfs-reclaim' if 'df -h /' disagrees with" >&2
  echo "          reality (issue #2999). A green deploy reaps older egg tags" >&2
  echo "          automatically (reap-stale-egg-images.sh)." >&2
fi

if [ "${#missing_registry[@]}" -gt 0 ] || [ "${#missing_containerd[@]}" -gt 0 ]; then
  exit 1
fi

echo "All egg images for tag '${TAG}' are present (registry: ${REGISTRY_IMAGES[*]:-none}; containerd: ${CONTAINERD_IMAGES[*]:-none})."
