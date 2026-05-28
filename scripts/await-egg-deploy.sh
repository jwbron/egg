#!/usr/bin/env bash
#
# await-egg-deploy.sh - Wait for the egg-system deployments to become
# Available, failing fast with an actionable message when the cause is
# an image tag that was never imported into k3s.
#
# The orchestrator/gateway/litellm manifests reference egg-*:<EGG_IMAGE_TAG>,
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
DEPLOYMENTS=(orchestrator gateway litellm)

# Keep kubectl stderr off of stdout so the success-path jsonpath value in
# $out is strictly equal to the queried field. If a future cluster ever
# emits a stderr warning on a successful call (admission webhook
# deprecation, API-version advisory, token-near-expiry), merging it into
# $out would silently fail the [ "$out" = "True" ] equality and poll
# until timeout.
err_file=$(mktemp)
trap 'rm -f "$err_file"' EXIT

deadline=$(( $(date +%s) + TIMEOUT ))

while :; do
  # Success: every deployment reports Available=True.
  all_available=1
  for d in "${DEPLOYMENTS[@]}"; do
    rc=0
    out=$(kubectl -n "$NS" get deployment "$d" \
      -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>"$err_file") || rc=$?
    err=$(<"$err_file")
    if [ "$rc" -ne 0 ] && ! grep -q 'NotFound' <<<"$err"; then
      # Real kubectl error (auth, connection, RBAC) — surface
      # immediately rather than polling silently for the full timeout.
      # NotFound is the expected "not yet observed" state during early
      # rollout and falls through to the not-Available branch below.
      echo "ERROR: kubectl get deployment $d failed: $err" >&2
      exit 1
    fi
    [ "$out" = "True" ] || all_available=0
  done
  if [ "$all_available" -eq 1 ]; then
    echo "All egg-system deployments are Available."
    exit 0
  fi

  # Fast-fail: an egg-owned pod can't pull its image. Almost always tag
  # drift — HEAD moved since the last build+import, so `make deploy`
  # references egg-*:$TAG which was never imported into k3s.
  #
  # The scan is scoped by label to egg's own deployments
  # (orchestrator/gateway/litellm) — those images are egg-built and
  # tag-rewritten by `make deploy`, so an ImagePullBackOff on EGG_IMAGE_TAG
  # is the tag-drift signal we want to fast-fail on. egg-litellm is the
  # stock LiteLLM image plus egg's cache patches, built and tagged like the
  # rest (config/litellm/Dockerfile), so it belongs in this scan too. Any
  # genuinely third-party pod in egg-system would not carry an egg
  # component label and so is excluded.
  egg_image_pull_failed=0
  for d in "${DEPLOYMENTS[@]}"; do
    if kubectl -n "$NS" get pods \
        -l "app.kubernetes.io/component=$d" \
        -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{.state.waiting.reason}{"\n"}{end}{end}' \
        2>/dev/null | grep -qE 'ImagePullBackOff|ErrImagePull'; then
      egg_image_pull_failed=1
      break
    fi
  done
  if [ "$egg_image_pull_failed" -eq 1 ]; then
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
    # For any not-ready egg pod, dump describe + previous-instance logs into
    # THIS step's output. A crash-looping pod's startup stderr is otherwise
    # only in the k3s-debug artifact, which CI autofix cannot download — so a
    # bare `get pods` (a lone `CrashLoopBackOff` line) gives no cause to act
    # on. `--previous` is the crashed instance's stderr; a plain `logs` on a
    # CrashLoopBackOff pod shows only the not-yet-crashed current attempt.
    for d in "${DEPLOYMENTS[@]}"; do
      pods=$(kubectl -n "$NS" get pods \
        -l "app.kubernetes.io/component=$d" \
        -o jsonpath='{.items[*].metadata.name}' 2>/dev/null) || continue
      for p in $pods; do
        ready=$(kubectl -n "$NS" get pod "$p" \
          -o jsonpath='{.status.containerStatuses[*].ready}' 2>/dev/null) || true
        case "$ready" in
          *false* | "")
            echo "       ----- describe pod/$p -----" >&2
            kubectl -n "$NS" describe pod "$p" >&2 2>&1 || true
            echo "       ----- logs pod/$p (previous, crashed instance) -----" >&2
            kubectl -n "$NS" logs "$p" --previous --all-containers >&2 2>&1 || true
            echo "       ----- logs pod/$p (current) -----" >&2
            kubectl -n "$NS" logs "$p" --all-containers >&2 2>&1 || true
            ;;
        esac
      done
    done
    exit 1
  fi

  sleep 3
done
