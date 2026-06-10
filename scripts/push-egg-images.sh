#!/usr/bin/env bash
#
# push-egg-images.sh - Publish the registry-subset egg images to the local
# loopback registry and pre-pull them into k3s's containerd (issue #2999).
#
# This replaces `docker save | k3s ctr images import` for the images in the
# subset. Both legs are layer-aware: `docker push` uploads only the layers
# the registry doesn't have, and the `crictl pull` fetches only the layers
# containerd doesn't have — so a code-only rebuild moves the changed
# tens-of-MB layers instead of re-serializing full images.
#
# The pre-pull is not strictly required for the Deployments (kubelet pulls
# on rollout), but it is load-bearing for two things:
#   - images only referenced by later-spawned pods (e.g. egg-sandbox, when
#     opted into the registry subset) start instantly instead of pulling on
#     first spawn;
#   - reap-stale-egg-images.sh's safety gate, which refuses to reap unless
#     every just-deployed ref is visible in containerd.
#
# Requires sudo for crictl (containerd's socket is root-only), same as
# k3s-import.
#
set -euo pipefail

usage="usage: $0 <egg-image-tag> <registry-host:port> <image>..."
: "${1:?$usage}"
: "${2:?$usage}"
: "${3:?$usage}"
TAG="$1"
REGISTRY="$2"
shift 2
# The image list comes from the caller (EGG_REGISTRY_IMAGES in the Makefile);
# by default that EXCLUDES egg-sandbox — it bakes in private repo content and
# must not be pushed to any registry unless the operator opts in there.
IMAGES=("$@")

# Privacy guard: refuse to push anywhere but the loopback registry. The
# egg images can bake in private repo dependencies (node_modules, .venv,
# anything repositories.yaml build_commands produce), so this flow must never
# publish off-host. The loopback registry is a container on this machine
# bound to 127.0.0.1 — unreachable from the network — which keeps the
# exposure identical to the docker daemon store and k3s's containerd. Anyone
# with a genuine remote-registry use case must build their own push path with
# its own redaction story; this script intentionally has no override.
REG_HOST="${REGISTRY%%:*}"
if [ "$REG_HOST" != "localhost" ] && [ "$REG_HOST" != "127.0.0.1" ]; then
  echo "ERROR: refusing to push egg images to non-loopback registry '${REGISTRY}'." >&2
  echo "       egg images can contain private repo content; this flow only" >&2
  echo "       publishes to a local 127.0.0.1-bound registry (make registry-setup)." >&2
  exit 1
fi

if ! curl -fsS "http://${REGISTRY}/v2/" >/dev/null 2>&1; then
  echo "ERROR: local registry at http://${REGISTRY}/v2/ is not answering." >&2
  echo "       One-time setup: 'make registry-setup' (starts the egg-registry" >&2
  echo "       container and points k3s at it). To deploy without a registry," >&2
  echo "       set EGG_IMAGE_REGISTRY= (empty) to use the save+import fallback." >&2
  exit 1
fi

# Fail fast if k3s was never pointed at the registry: the pre-pull below and
# every later kubelet pull would die with "server gave HTTP response to
# HTTPS client" — after `kubectl apply` has already repointed the cluster.
if [ -d /etc/rancher/k3s ] && ! grep -qs "\"${REGISTRY}\":" /etc/rancher/k3s/registries.yaml; then
  echo "ERROR: /etc/rancher/k3s/registries.yaml has no entry for ${REGISTRY}," >&2
  echo "       so k3s's containerd cannot pull from the plain-HTTP local registry." >&2
  echo "       Run 'make registry-setup' once to write it (restarts the k3s service)." >&2
  exit 1
fi

for image in "${IMAGES[@]}"; do
  echo ">>> pushing ${REGISTRY}/${image}:${TAG}"
  docker push "${REGISTRY}/${image}:${TAG}"
  # Same digest as :TAG — manifest-only upload, no layer data moves.
  echo ">>> pushing ${REGISTRY}/${image}:latest"
  docker push "${REGISTRY}/${image}:latest"
done

for image in "${IMAGES[@]}"; do
  echo ">>> pre-pulling ${REGISTRY}/${image}:${TAG} into k3s containerd"
  sudo k3s crictl pull "${REGISTRY}/${image}:${TAG}"
done

echo "Registry-subset images (${IMAGES[*]}) for tag '${TAG}' published to ${REGISTRY} and pre-pulled into k3s."
