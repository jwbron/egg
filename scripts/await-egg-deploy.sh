#!/usr/bin/env bash
#
# await-egg-deploy.sh - Wait for the egg-system deployments to become
# Available, failing fast with an actionable message when the cause is
# an image tag that was never imported into k3s.
#
# The orchestrator/gateway manifests reference egg-*:<EGG_IMAGE_TAG>,
# where EGG_IMAGE_TAG is `git describe --always` and so changes on every
# commit, pull, rebase, or checkout. `make redeploy` builds, imports, and
# deploys in one invocation so the tag is self-consistent — but a bare
# `make deploy` after HEAD has moved references a tag whose images are
# not in k3s's containerd, and the pods sit in ImagePullBackOff.
#
# Without this guard that surfaces only as a bare 120s `kubectl wait`
# timeout ("error: timed out waiting for the condition"). Here we detect
# the ImagePullBackOff within seconds and point at the fix.
#
set -euo pipefail

NS="egg-system"
: "${1:?usage: $0 <egg-image-tag> [timeout-seconds]}"
TAG="$1"
TIMEOUT="${2:-180}"
DEPLOYMENTS=(orchestrator gateway)

deadline=$(( $(date +%s) + TIMEOUT ))

while :; do
  # Success: every deployment reports Available=True.
  all_available=1
  for d in "${DEPLOYMENTS[@]}"; do
    rc=0
    out=$(kubectl -n "$NS" get deployment "$d" \
      -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>&1) || rc=$?
    if [ "$rc" -ne 0 ] && ! grep -q 'NotFound' <<<"$out"; then
      # Real kubectl error (auth, connection, RBAC) — surface
      # immediately rather than polling silently for the full timeout.
      # NotFound is the expected "not yet observed" state during early
      # rollout and falls through to the not-Available branch below.
      echo "ERROR: kubectl get deployment $d failed: $out" >&2
      exit 1
    fi
    [ "$out" = "True" ] || all_available=0
  done
  if [ "$all_available" -eq 1 ]; then
    echo "All egg-system deployments are Available."
    exit 0
  fi

  # Fast-fail: a pod can't pull its image. Almost always tag drift —
  # HEAD moved since the last build+import, so `make deploy` references
  # egg-*:$TAG which was never imported into k3s.
  if kubectl -n "$NS" get pods \
      -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{.state.waiting.reason}{"\n"}{end}{end}' \
      2>/dev/null | grep -qE 'ImagePullBackOff|ErrImagePull'; then
    echo "ERROR: egg-system pods cannot pull image tag '${TAG}' — it is not in k3s." >&2
    echo "       A commit, pull, or rebase since your last build moved EGG_IMAGE_TAG." >&2
    echo "       'make deploy' alone only deploys; run 'make redeploy' to rebuild +" >&2
    echo "       re-import + deploy on the current tag." >&2
    exit 1
  fi

  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "ERROR: timed out after ${TIMEOUT}s waiting for egg-system deployments." >&2
    echo "       Current pod state:" >&2
    kubectl -n "$NS" get pods >&2 || true
    exit 1
  fi

  sleep 3
done
