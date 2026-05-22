#!/usr/bin/env bash
#
# clear-stuck-egg-pods.sh - Delete egg orchestrator/gateway pods stuck in
# ImagePullBackOff/ErrImagePull so their ReplicaSet recreates them.
#
# `make redeploy` builds images, imports them into k3s, then deploys --
# in that order, so a redeploy's own pods are always created after their
# image exists. But `kubectl apply` is a no-op when the rendered manifest
# is unchanged (EGG_IMAGE_TAG has not moved), so a pod left in
# ImagePullBackOff by an earlier bare `make deploy` -- run when HEAD had
# moved and the tag's images were not yet imported -- survives a
# subsequent `make redeploy` untouched. It then keeps failing until its
# exponential image-pull backoff (up to ~5 min between retries) happens
# to fire.
#
# Deleting the stuck pod lets its ReplicaSet recreate it immediately;
# with the image now in containerd it pulls cleanly. This is what lets
# `make redeploy` converge a stuck cluster -- the remedy
# await-egg-deploy.sh tells the operator to run.
#
# A no-op when nothing is stuck. Safe on the bare-`make deploy` path too:
# a recreated pod simply re-sticks if the image genuinely is not
# imported, and await-egg-deploy.sh then fast-fails with the tag-drift
# message.
#
set -euo pipefail

NS="egg-system"

# Scoped to egg's own deployments, matching await-egg-deploy.sh: their
# images are egg-built and tag-rewritten by `make deploy`. Third-party
# pods (e.g. the LiteLLM proxy) pull from an external registry, so
# recreating them would not clear a registry-side pull failure.
mapfile -t stuck < <(
  kubectl -n "$NS" get pods \
    -l 'app.kubernetes.io/component in (orchestrator,gateway)' \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.containerStatuses[*]}{.state.waiting.reason}{" "}{end}{"\n"}{end}' \
    | awk '/ImagePullBackOff|ErrImagePull/ { print $1 }'
)

if [ "${#stuck[@]}" -eq 0 ]; then
  exit 0
fi

echo "Recreating egg pods stuck on image pull: ${stuck[*]}"
kubectl -n "$NS" delete pod "${stuck[@]}" --ignore-not-found
